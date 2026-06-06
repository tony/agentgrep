"""Persistent SQLite DB index for normalized agent data."""

from __future__ import annotations

import collections.abc as cabc
import concurrent.futures
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import time
import typing as t
import unicodedata

import agentgrep
from agentgrep._engine.scanning import _CACHE_EXEMPT_ADAPTERS

logger = logging.getLogger(__name__)

CacheMode = t.Literal["auto", "require", "off"]
FeatureMode = t.Literal["defer", "inline"]

SCHEMA_VERSION = 1
DEFAULT_DB_FILENAME = "agentgrep.sqlite"
#: First keyset-probe window; the study's sweet spot — one page seals
#: limit-50 searches at every measured term frequency.
_PROBE_WINDOW_FLOOR = 200
_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")
_FEATURE_PROGRESS_INTERVAL = 1024


class FeatureRefreshProgress(t.Protocol):
    """Progress sink for deterministic feature-refresh phases."""

    def set_activity(self, activity: str, *, detail: str | None = None) -> None:
        """Report the current feature-refresh activity."""


@dataclasses.dataclass(frozen=True, slots=True)
class DbStatus:
    """Summary of the persisted DB index."""

    db_path: pathlib.Path
    schema_version: int
    sources: int
    records: int
    features: int
    variant_edges: int
    omission_findings: int
    suggestions: int


@dataclasses.dataclass(frozen=True, slots=True)
class DbExplain:
    """Cache diagnostics for ``agentgrep db explain``."""

    db_path: pathlib.Path
    schema_version: int
    sources: int
    records: int
    synced_ok: int
    sync_errors: int
    last_synced_at: str | None
    answerable: str
    coverage: dict[str, tuple[str, ...]] | None = None


ANSWERABLE_QUERY_FORMS = "term AND queries (no regex, no OR)"

#: Meta key recording which agent/scope combinations completed a sync.
COVERAGE_META_KEY = "coverage_json"


@dataclasses.dataclass(frozen=True, slots=True)
class SyncResult:
    """Counters returned by a DB sync operation."""

    sources_synced: int
    records_indexed: int
    records_removed: int
    sources_skipped: int = 0
    sources_pruned: int = 0
    features_deferred: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class SyncCoverage:
    """What one sync invocation set out to cover.

    Coverage means "this agent/scope combination completed a sync",
    not "currently has records" — the auto-mode empty-result fallback
    already protects the no-records case. ``complete`` is false when
    the caller capped the source list, so capped syncs never claim
    coverage.
    """

    agents: tuple[str, ...]
    scope: str
    complete: bool


@dataclasses.dataclass(frozen=True, slots=True)
class DbRecordRow:
    """One record row with its stable DB id."""

    record_id: str
    record: agentgrep.SearchRecord


@dataclasses.dataclass(frozen=True, slots=True)
class DbSimilarityRow:
    """One record id plus its normalized text hash for similarity analysis."""

    record_id: str
    normalized_hash: str


@dataclasses.dataclass(frozen=True, slots=True)
class _FeatureInput:
    """Minimal pickleable input for deterministic feature building."""

    record_id: str
    text: str
    normalized_hash: str
    timestamp: str | None
    updated_at: str


@dataclasses.dataclass(frozen=True, slots=True)
class _FeatureValues:
    """Feature row values ready for SQLite insertion."""

    record_id: str
    normalized_hash: str
    simhash_hex: str
    minhash_json: str
    token_count: int
    quality_flags_json: str
    updated_at: str


class DbQueryUnsupported(RuntimeError):
    """Raised when a query cannot be answered from the DB index."""


type SourceRecordBatch = tuple[
    agentgrep.SourceHandle,
    cabc.Iterable[agentgrep.SearchRecord],
]


class DbSyncProgress(t.Protocol):
    """Progress reporter used by DB sync internals."""

    def start(self, total_sources: int) -> None:
        """Report the planned source count."""
        ...

    def source_started(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        result: SyncResult,
    ) -> None:
        """Report that one source transaction is starting."""
        ...

    def source_finished(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        records_indexed: int,
        records_removed: int,
        result: SyncResult,
    ) -> None:
        """Report that one source transaction has committed."""
        ...

    def finish(self, result: SyncResult) -> None:
        """Report normal sync completion."""
        ...

    def exiting_early(self, result: SyncResult) -> None:
        """Report cooperative early exit with partial counters."""
        ...


class NoopDbSyncProgress:
    """Silent DB sync progress reporter."""

    def start(self, total_sources: int) -> None:
        """Ignore the planned source count."""
        _ = total_sources

    def source_started(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        result: SyncResult,
    ) -> None:
        """Ignore source start."""
        _ = (index, total, source, result)

    def source_finished(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        records_indexed: int,
        records_removed: int,
        result: SyncResult,
    ) -> None:
        """Ignore source completion."""
        _ = (index, total, source, records_indexed, records_removed, result)

    def finish(self, result: SyncResult) -> None:
        """Ignore sync completion."""
        _ = result

    def exiting_early(self, result: SyncResult) -> None:
        """Ignore early exit."""
        _ = result


def noop_db_sync_progress() -> DbSyncProgress:
    """Return a silent DB sync progress reporter."""
    return NoopDbSyncProgress()


def default_db_path() -> pathlib.Path:
    """Return the default path for the local DB cache."""
    configured = os.environ.get("AGENTGREP_DB")
    if configured:
        return pathlib.Path(configured).expanduser()
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = pathlib.Path(cache_home).expanduser() if cache_home else pathlib.Path.home() / ".cache"
    return base / "agentgrep" / DEFAULT_DB_FILENAME


def normalize_record_text(text: str) -> str:
    """Normalize text for deterministic hashes and similarity features.

    Examples
    --------
    >>> normalize_record_text("Run RUFF check!  ")
    'run ruff check'
    >>> normalize_record_text("paths like src/agentgrep/db.py survive")
    'paths like src/agentgrep/db.py survive'
    >>> normalize_record_text("")
    ''
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = [token.strip(".,;:!?") for token in _TOKEN_RE.findall(normalized)]
    return " ".join(token for token in tokens if token)


def token_set(text: str) -> frozenset[str]:
    """Return deterministic lowercase tokens for lightweight similarity.

    Examples
    --------
    >>> sorted(token_set("Run ruff check, run ruff check"))
    ['check', 'ruff', 'run']
    >>> token_set("")
    frozenset()
    """
    return frozenset(normalize_record_text(text).split())


def text_hash(text: str) -> str:
    """Return a stable SHA-256 hex digest for ``text``.

    Examples
    --------
    >>> text_hash("ruff")[:12]
    'acadbba99747'
    >>> len(text_hash(""))
    64
    """
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def source_id_for(source: agentgrep.SourceHandle) -> str:
    """Return a stable source id derived from adapter identity and path."""
    identity = "\0".join(
        (
            source.agent,
            source.store,
            source.adapter_id,
            str(source.path),
        ),
    )
    return text_hash(identity)


def record_id_for(source_id: str, record: agentgrep.SearchRecord) -> str:
    """Return a stable record id derived from native identity and text."""
    return record_id_for_normalized(
        source_id,
        record,
        normalized_hash=text_hash(normalize_record_text(record.text)),
    )


def record_id_for_normalized(
    source_id: str,
    record: agentgrep.SearchRecord,
    *,
    normalized_hash: str,
) -> str:
    """Return a stable record id when the normalized text hash is already known."""
    native = "\0".join(
        (
            record.session_id or "",
            record.conversation_id or "",
            record.timestamp or "",
            record.role or "",
            record.title or "",
        ),
    )
    return text_hash("\0".join((source_id, native, normalized_hash, record.text)))


def _now_iso() -> str:
    """Return a UTC timestamp suitable for SQLite rows."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def _json_dumps(value: object) -> str:
    """Serialize JSON metadata with stable ordering."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads_mapping(value: str) -> dict[str, object]:
    """Deserialize a metadata mapping, tolerating legacy or corrupt rows."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_fingerprint(source: agentgrep.SourceHandle) -> str:
    """Return a cheap source fingerprint for cache freshness checks.

    WAL-mode SQLite stores commit into a ``-wal`` sidecar while the main
    database file's size and mtime stay unchanged until a checkpoint, so
    sqlite sources fold the sidecar's stat into the fingerprint — the
    same invariant the engine's source-scan cache keys rely on.
    """
    try:
        stat = source.path.stat()
    except OSError:
        size = 0
        mtime_ns = source.mtime_ns
    else:
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns
    wal_part = ""
    if source.source_kind == "sqlite":
        wal_path = source.path.with_name(source.path.name + "-wal")
        try:
            wal_stat = wal_path.stat()
        except OSError:
            wal_part = "\0wal:none"
        else:
            wal_part = f"\0wal:{wal_stat.st_size}\0{wal_stat.st_mtime_ns}"
    return text_hash(f"{source.path}\0{size}\0{mtime_ns}{wal_part}")


def _quote_fts_term(term: str) -> str:
    """Quote one user term for an FTS5 MATCH expression.

    Examples
    --------
    >>> _quote_fts_term("ruff")
    '"ruff"'
    >>> _quote_fts_term('say "hi" now')
    '"say ""hi"" now"'
    """
    return '"' + term.replace('"', '""') + '"'


def _fts_indexable(term: str) -> bool:
    """Return whether the trigram index can serve ``term`` losslessly.

    The trigram tokenizer indexes nothing shorter than three
    characters; shorter terms take the exact table scan instead. Case
    folding happens in Python on both the indexed haystack and the
    query term, so the index never re-folds disagreeably.
    """
    return len(term) >= 3


def _record_in_cached_scope(
    record: agentgrep.SearchRecord,
    scope: agentgrep.SearchScope,
    prompt_history_agents: frozenset[str],
) -> bool:
    """Return whether a cached record belongs to the requested scope.

    Composes the live pipeline's two scope filters: the per-record
    :func:`agentgrep.record_matches_scope` check and the planner's
    :func:`agentgrep.source_matches_scope` store selection. The cached
    table holds records from every synced store, so the planner's
    source-level decisions must be re-applied per record — most
    visibly for prompts scope, where an agent with a dedicated
    prompt-history store never serves user turns from its chat stores.

    Parameters
    ----------
    record : agentgrep.SearchRecord
        Cached record reconstructed from the records table.
    scope : agentgrep.SearchScope
        Requested search scope; ``"all"`` is handled by the caller.
    prompt_history_agents : frozenset[str]
        Agents holding a synced prompt-history-role source.

    Returns
    -------
    bool
        Whether the record is in scope.
    """
    return _cached_scope_admits(
        record.kind,
        record.store,
        record.adapter_id,
        record.agent,
        scope,
        prompt_history_agents,
    )


def _cached_scope_admits(
    kind: str,
    store: str,
    adapter_id: str,
    agent: str,
    scope: agentgrep.SearchScope,
    prompt_history_agents: frozenset[str],
) -> bool:
    """Scalar form of the cached scope filter for lean probe rows."""
    role = agentgrep.store_role_for_record(store, adapter_id)
    if scope == "conversations":
        return role in agentgrep.CONVERSATION_STORE_ROLES
    if kind != "prompt":
        return False
    if role in agentgrep.CONVERSATION_STORE_ROLES:
        return agent not in prompt_history_agents
    return True


@dataclasses.dataclass(slots=True)
class _SqlStatementStats:
    """Aggregated telemetry for one named SQL statement shape."""

    count: int = 0
    seconds: float = 0.0
    rows: int = 0
    plan: str | None = None


def _sql_explain_enabled() -> bool:
    """Return whether EXPLAIN QUERY PLAN capture is requested.

    Controlled by the ``AGENTGREP_SQL_EXPLAIN`` environment variable;
    any non-empty value enables capture.
    """
    return bool(os.environ.get("AGENTGREP_SQL_EXPLAIN"))


class DbStore:
    """SQLite-backed store for DB, insight, and suggestion artifacts."""

    def __init__(self, db_path: pathlib.Path, *, readonly: bool = False) -> None:
        self.db_path = db_path
        self._sql_stats: dict[str, _SqlStatementStats] = {}
        if readonly:
            self.connection = agentgrep.open_readonly_sqlite(self.db_path)
            self.connection.row_factory = sqlite3.Row
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    @classmethod
    def open(cls, db_path: pathlib.Path | str | None = None) -> DbStore:
        """Open a DB store at ``db_path`` or the default cache path."""
        resolved = default_db_path() if db_path is None else pathlib.Path(db_path)
        return cls(resolved.expanduser())

    @classmethod
    def open_readonly(cls, db_path: pathlib.Path | str | None = None) -> DbStore:
        """Open the store read-only, without schema writes or WAL pragmas.

        Status surfaces must not mutate the cache: the regular open
        path runs schema migration and records the schema version,
        which writes on every call. The read-only URI mode also works
        on read-only filesystems and never creates the file.
        """
        resolved = default_db_path() if db_path is None else pathlib.Path(db_path)
        return cls(resolved.expanduser(), readonly=True)

    def close(self) -> None:
        """Close the SQLite connection."""
        self.connection.close()

    def _track(
        self,
        stmt_name: str,
        sql: str,
        elapsed: float,
        rows: int,
    ) -> _SqlStatementStats:
        """Accumulate one statement execution into the telemetry stats.

        The statement text carries placeholders only; bound parameters
        are never logged or recorded (they can hold search terms).
        """
        stats = self._sql_stats.get(stmt_name)
        if stats is None:
            stats = _SqlStatementStats()
            self._sql_stats[stmt_name] = stats
        stats.count += 1
        stats.seconds += elapsed
        stats.rows += rows
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "sql statement executed: %s",
                " ".join(sql.split()),
                extra={
                    "agentgrep_sql_statement": stmt_name,
                    "agentgrep_sql_seconds": elapsed,
                    "agentgrep_sql_rows": rows,
                },
            )
        return stats

    def _capture_plan(
        self,
        stmt_name: str,
        stats: _SqlStatementStats,
        sql: str,
        params: tuple[object, ...],
    ) -> None:
        """Capture EXPLAIN QUERY PLAN once per statement shape, if enabled.

        Plan rows carry table, index, and strategy names only — no
        bound parameters — so the joined detail text is privacy-safe.
        """
        if stats.plan is not None or not _sql_explain_enabled():
            return
        plan_rows = self.connection.execute(
            f"EXPLAIN QUERY PLAN {sql}",
            params,
        ).fetchall()
        stats.plan = "; ".join(str(row["detail"]) for row in plan_rows)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "sql plan captured: %s",
                stats.plan,
                extra={"agentgrep_sql_statement": stmt_name},
            )

    def _query(
        self,
        stmt_name: str,
        sql: str,
        params: cabc.Sequence[object] = (),
    ) -> list[sqlite3.Row]:
        """Run one SELECT through the telemetry choke point."""
        bound = tuple(params)
        start = time.perf_counter()
        rows = self.connection.execute(sql, bound).fetchall()
        stats = self._track(stmt_name, sql, time.perf_counter() - start, len(rows))
        self._capture_plan(stmt_name, stats, sql, bound)
        return rows

    def _execute(
        self,
        stmt_name: str,
        sql: str,
        params: cabc.Sequence[object] = (),
    ) -> sqlite3.Cursor:
        """Run one write statement through the telemetry choke point."""
        start = time.perf_counter()
        cursor = self.connection.execute(sql, tuple(params))
        rows = cursor.rowcount if cursor.rowcount > 0 else 0
        _ = self._track(stmt_name, sql, time.perf_counter() - start, rows)
        return cursor

    def _executescript(self, stmt_name: str, script: str) -> None:
        """Run one SQL script through the telemetry choke point."""
        start = time.perf_counter()
        _ = self.connection.executescript(script)
        _ = self._track(stmt_name, script, time.perf_counter() - start, 0)

    def _flush_sql_samples(self) -> None:
        """Emit one aggregate profile sample per executed statement shape.

        One sample per statement name, never per execution: sync loops
        run two statements per record, and per-execution samples would
        swamp the profile. A high ``agentgrep_sql_count`` on a single
        sample is the n+1 signal.
        """
        if not self._sql_stats:
            return
        stats_by_name = self._sql_stats
        self._sql_stats = {}
        for stmt_name, stats in sorted(stats_by_name.items()):
            if stats.plan is not None:
                agentgrep._record_engine_profile_sample(
                    "db.sql.statement",
                    stats.seconds,
                    agentgrep_sql_statement=stmt_name,
                    agentgrep_sql_count=stats.count,
                    agentgrep_sql_rows=stats.rows,
                    agentgrep_sql_plan=stats.plan,
                )
                continue
            agentgrep._record_engine_profile_sample(
                "db.sql.statement",
                stats.seconds,
                agentgrep_sql_statement=stmt_name,
                agentgrep_sql_count=stats.count,
                agentgrep_sql_rows=stats.rows,
            )

    def _configure(self) -> None:
        """Configure connection-local SQLite settings.

        ``page_size`` only takes effect on databases created by this
        connection — existing caches keep their page size until the
        next rebuild. 8 KiB pages measurably shorten overflow chains
        for the ~3 KiB detail rows; the mmap budget rationale lives on
        :data:`agentgrep.SQLITE_MMAP_BYTES`.
        """
        _ = self._execute("pragma.page_size", "PRAGMA page_size=8192")
        _ = self._execute("pragma.journal_mode", "PRAGMA journal_mode=WAL")
        _ = self._execute("pragma.foreign_keys", "PRAGMA foreign_keys=ON")
        _ = self._execute(
            "pragma.mmap_size",
            f"PRAGMA mmap_size={agentgrep.SQLITE_MMAP_BYTES}",
        )

    def _stored_schema_version(self) -> int | None:
        """Return the schema version recorded in ``meta``, if any."""
        try:
            rows = self._query(
                "meta.schema_version.get",
                "SELECT value FROM meta WHERE key = 'schema_version'",
            )
            row = rows[0] if rows else None
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        try:
            return int(str(row["value"]))
        except ValueError:
            return None

    def _migrate(self) -> None:
        """Create or upgrade the SQLite schema.

        The database is a derived cache, so a schema-version mismatch
        drops and recreates every table; the next sync repopulates it.
        """
        with self.connection:
            stored = self._stored_schema_version()
            if stored is not None and stored != SCHEMA_VERSION:
                self._executescript(
                    "schema.drop",
                    """
                    DROP TABLE IF EXISTS suggestion_evidence;
                    DROP TABLE IF EXISTS suggestions;
                    DROP TABLE IF EXISTS omission_findings;
                    DROP TABLE IF EXISTS variant_edges;
                    DROP TABLE IF EXISTS cluster_members;
                    DROP TABLE IF EXISTS clusters;
                    DROP TABLE IF EXISTS insight_runs;
                    DROP TABLE IF EXISTS record_features;
                    DROP TABLE IF EXISTS record_text_fts;
                    DROP TABLE IF EXISTS source_state;
                    DROP TABLE IF EXISTS record_details;
                    DROP TABLE IF EXISTS records_search;
                    DROP TABLE IF EXISTS records;
                    DROP TABLE IF EXISTS sources;
                    DROP TABLE IF EXISTS meta;
                    """,
                )
            self._executescript(
                "schema.create",
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    store TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    path_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    search_root TEXT,
                    coverage TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    version_detection_json TEXT NOT NULL,
                    last_seen_generation INTEGER NOT NULL,
                    tombstoned_at TEXT,
                    UNIQUE(agent, store, adapter_id, path)
                );

                CREATE TABLE IF NOT EXISTS source_state (
                    source_id TEXT PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,
                    sync_status TEXT NOT NULL,
                    synced_mtime_ns INTEGER NOT NULL,
                    synced_fingerprint TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS records_search (
                    rowid INTEGER PRIMARY KEY,
                    record_id TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    store TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    timestamp TEXT,
                    session_id TEXT,
                    conversation_id TEXT,
                    text_hash TEXT NOT NULL,
                    normalized_text_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS record_details (
                    rowid INTEGER PRIMARY KEY
                        REFERENCES records_search(rowid) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    title TEXT,
                    role TEXT,
                    model TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS record_text_fts
                USING fts5(haystack, tokenize='trigram');

                CREATE INDEX IF NOT EXISTS idx_records_search_source_id
                ON records_search(source_id);
                CREATE TABLE IF NOT EXISTS record_features (
                    record_id TEXT PRIMARY KEY
                        REFERENCES records_search(record_id) ON DELETE CASCADE,
                    normalized_hash TEXT NOT NULL,
                    simhash_hex TEXT NOT NULL,
                    minhash_json TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    quality_flags_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS insight_runs (
                    run_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    counters_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clusters (
                    cluster_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES insight_runs(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    centroid_record_id TEXT,
                    confidence REAL NOT NULL,
                    evidence_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cluster_members (
                    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
                    record_id TEXT NOT NULL REFERENCES records_search(record_id) ON DELETE CASCADE,
                    score REAL NOT NULL,
                    signals_json TEXT NOT NULL,
                    PRIMARY KEY(cluster_id, record_id)
                );

                CREATE TABLE IF NOT EXISTS variant_edges (
                    edge_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES insight_runs(run_id) ON DELETE CASCADE,
                    left_record_id TEXT NOT NULL
                        REFERENCES records_search(record_id) ON DELETE CASCADE,
                    right_record_id TEXT NOT NULL
                        REFERENCES records_search(record_id) ON DELETE CASCADE,
                    variant_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    signals_json TEXT NOT NULL,
                    explanation TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS omission_findings (
                    finding_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES insight_runs(run_id) ON DELETE CASCADE,
                    target_path TEXT NOT NULL,
                    cluster_id TEXT,
                    representative_record_id TEXT NOT NULL
                        REFERENCES records_search(record_id) ON DELETE CASCADE,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    rationale TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS suggestions (
                    suggestion_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES insight_runs(run_id) ON DELETE CASCADE,
                    target_path TEXT NOT NULL,
                    surface_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    reload_note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS suggestion_evidence (
                    suggestion_id TEXT NOT NULL
                        REFERENCES suggestions(suggestion_id) ON DELETE CASCADE,
                    record_id TEXT NOT NULL REFERENCES records_search(record_id) ON DELETE CASCADE,
                    evidence_role TEXT NOT NULL,
                    score REAL NOT NULL,
                    signals_json TEXT NOT NULL,
                    PRIMARY KEY(suggestion_id, record_id, evidence_role)
                );

                CREATE INDEX IF NOT EXISTS idx_variant_edges_confidence_edge_id
                ON variant_edges(confidence DESC, edge_id);

                CREATE INDEX IF NOT EXISTS idx_suggestions_confidence_suggestion_id
                ON suggestions(confidence DESC, suggestion_id);
                """,
            )
            _ = self._execute(
                "meta.schema_version.set",
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def status(self) -> DbStatus:
        """Return db row counts."""
        try:
            return DbStatus(
                db_path=self.db_path,
                schema_version=SCHEMA_VERSION,
                sources=self._count("sources"),
                records=self._count("records_search"),
                features=self._count("record_features"),
                variant_edges=self._count("variant_edges"),
                omission_findings=self._count("omission_findings"),
                suggestions=self._count("suggestions"),
            )
        finally:
            self._flush_sql_samples()

    def explain(self) -> DbExplain:
        """Return cache diagnostics: counts, sync state, answerable forms."""
        try:
            return self._explain()
        finally:
            self._flush_sql_samples()

    def _explain(self) -> DbExplain:
        """Compute ``explain`` ahead of the telemetry flush."""
        ok_rows = self._query(
            "source_state.count_ok",
            "SELECT COUNT(*) AS count FROM source_state WHERE sync_status = 'ok'",
        )
        ok_row = ok_rows[0] if ok_rows else None
        error_rows = self._query(
            "source_state.count_errors",
            """
            SELECT COUNT(*) AS count FROM source_state
            WHERE sync_status != 'ok' OR last_error IS NOT NULL
            """,
        )
        error_row = error_rows[0] if error_rows else None
        last_rows = self._query(
            "source_state.last_synced",
            "SELECT MAX(updated_at) AS last FROM source_state",
        )
        last_row = last_rows[0] if last_rows else None
        last_synced = last_row["last"] if last_row is not None else None
        return DbExplain(
            db_path=self.db_path,
            schema_version=SCHEMA_VERSION,
            sources=self._count("sources"),
            records=self._count("records_search"),
            synced_ok=int(ok_row["count"]) if ok_row is not None else 0,
            sync_errors=int(error_row["count"]) if error_row is not None else 0,
            last_synced_at=str(last_synced) if last_synced is not None else None,
            answerable=ANSWERABLE_QUERY_FORMS,
            coverage=self.coverage(),
        )

    def get_meta(self, key: str) -> str | None:
        """Return one meta value or ``None`` when the key is absent."""
        rows = self._query(
            "meta.get",
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        )
        return str(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: str) -> None:
        """Insert or replace one meta value."""
        with self.connection:
            _ = self._execute(
                "meta.set",
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, value),
            )

    def coverage(self) -> dict[str, tuple[str, ...]] | None:
        """Return the synced agent/scope coverage map.

        ``None`` means no completed sync has recorded coverage yet —
        distinct from an empty map — so callers can tell an old cache
        apart from one that covered nothing.
        """
        raw = self.get_meta(COVERAGE_META_KEY)
        if raw is None:
            return None
        payload = t.cast("dict[str, list[str]]", json.loads(raw))
        return {agent: tuple(scopes) for agent, scopes in sorted(payload.items())}

    def merge_coverage(self, coverage: SyncCoverage) -> None:
        """Merge one completed sync's agent/scope coverage into meta.

        Merging (rather than replacing) keeps a narrowed re-sync from
        erasing the coverage earlier full syncs established for other
        agents.
        """
        existing = self.coverage() or {}
        merged: dict[str, list[str]] = {agent: list(scopes) for agent, scopes in existing.items()}
        for agent in coverage.agents:
            scopes = set(merged.get(agent, []))
            scopes.add(coverage.scope)
            merged[agent] = sorted(scopes)
        self.set_meta(COVERAGE_META_KEY, _json_dumps(merged))

    def covers(self, agents: cabc.Iterable[str], scope: str) -> bool:
        """Return whether every agent has completed a sync for ``scope``.

        A sync with scope ``"all"`` covers every query scope; a scoped
        sync covers only itself.
        """
        coverage = self.coverage()
        if coverage is None:
            return False
        for agent in agents:
            scopes = coverage.get(agent)
            if scopes is None or (scope not in scopes and "all" not in scopes):
                return False
        return True

    def _count(self, table: str) -> int:
        """Return row count for a known table."""
        rows = self._query(f"count.{table}", f"SELECT COUNT(*) AS count FROM {table}")
        return int(rows[0]["count"]) if rows else 0

    def replace_source_records(
        self,
        source: agentgrep.SourceHandle,
        records: cabc.Iterable[agentgrep.SearchRecord],
        *,
        features_mode: FeatureMode = "defer",
    ) -> tuple[int, int, int]:
        """Replace every indexed record for ``source``.

        Returns
        -------
        tuple[int, int, int]
            ``(records_indexed, records_removed, features_deferred)``.
        """
        source_id = source_id_for(source)
        now = _now_iso()
        fingerprint = _source_fingerprint(source)
        record_list = list(records)
        with self.connection:
            self._upsert_source(source, source_id=source_id, fingerprint=fingerprint, now=now)
            removed = self._remove_source_records(source_id)
            indexed = 0
            seen_record_ids: dict[str, int] = {}
            features_deferred = 0
            for record in record_list:
                raw_hash = text_hash(record.text)
                normalized_text = normalize_record_text(record.text)
                normalized_hash = text_hash(normalized_text)
                base_record_id = record_id_for_normalized(
                    source_id,
                    record,
                    normalized_hash=normalized_hash,
                )
                duplicate_index = seen_record_ids.get(base_record_id, 0)
                seen_record_ids[base_record_id] = duplicate_index + 1
                record_id = (
                    base_record_id
                    if duplicate_index == 0
                    else text_hash(f"{base_record_id}\0duplicate\0{duplicate_index}")
                )
                self._insert_record(
                    source_id,
                    record,
                    record_id=record_id,
                    now=now,
                    raw_hash=raw_hash,
                    normalized_text=normalized_text,
                    normalized_hash=normalized_hash,
                    features_mode=features_mode,
                )
                indexed += 1
                if features_mode == "defer":
                    features_deferred += 1
            _ = self._execute(
                "source_state.upsert",
                """
                INSERT OR REPLACE INTO source_state(
                    source_id, sync_status, synced_mtime_ns,
                    synced_fingerprint, last_error, updated_at
                )
                VALUES(?, 'ok', ?, ?, NULL, ?)
                """,
                (source_id, source.mtime_ns, fingerprint, now),
            )
        return indexed, removed, features_deferred

    def source_is_current(self, source: agentgrep.SourceHandle) -> bool:
        """Return whether ``source`` has an up-to-date successful sync state.

        Adapters whose record text depends on files outside
        ``source.path`` are never current: the fingerprint stats only
        the primary file, so a changed sibling file — Claude history
        resolves ``paste-cache/<contentHash>.txt`` references — would
        otherwise leave stale expansions in the index. Mirrors the
        engine's source-scan cache exemption.
        """
        if source.adapter_id in _CACHE_EXEMPT_ADAPTERS:
            return False
        source_id = source_id_for(source)
        fingerprint = _source_fingerprint(source)
        state_rows = self._query(
            "source_state.get",
            """
            SELECT sync_status, synced_mtime_ns, synced_fingerprint
            FROM source_state
            WHERE source_id = ?
            """,
            (source_id,),
        )
        if not state_rows:
            return False
        row = state_rows[0]
        return (
            str(row["sync_status"]) == "ok"
            and int(row["synced_mtime_ns"]) == source.mtime_ns
            and str(row["synced_fingerprint"]) == fingerprint
        )

    def _upsert_source(
        self,
        source: agentgrep.SourceHandle,
        *,
        source_id: str,
        fingerprint: str,
        now: str,
    ) -> None:
        """Insert or update one source ledger row."""
        version_detection = (
            dataclasses.asdict(source.version_detection)
            if source.version_detection is not None
            else None
        )
        _ = self._execute(
            "sources.upsert",
            """
            INSERT INTO sources(
                source_id, agent, store, adapter_id, path, path_kind, source_kind,
                search_root, coverage, mtime_ns, fingerprint,
                version_detection_json, last_seen_generation, tombstoned_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
            ON CONFLICT(source_id) DO UPDATE SET
                mtime_ns=excluded.mtime_ns,
                fingerprint=excluded.fingerprint,
                version_detection_json=excluded.version_detection_json,
                last_seen_generation=sources.last_seen_generation + 1,
                tombstoned_at=NULL
            """,
            (
                source_id,
                source.agent,
                source.store,
                source.adapter_id,
                str(source.path),
                source.path_kind,
                source.source_kind,
                str(source.search_root) if source.search_root is not None else None,
                source.coverage.name,
                source.mtime_ns,
                fingerprint,
                _json_dumps(version_detection),
            ),
        )
        _ = now

    def _remove_source_records(self, source_id: str) -> int:
        """Delete indexed records for one source and return the removed count.

        The FTS table stores its own haystack (content-full), so its
        rows go with a plain DELETE; the details rows cascade from
        records_search via the foreign key.
        """
        rows = self._query(
            "records_search.select_for_delete",
            "SELECT rowid FROM records_search WHERE source_id = ?",
            (source_id,),
        )
        if rows:
            rowids = ",".join(str(int(row["rowid"])) for row in rows)
            _ = self._execute(
                "fts.delete_by_rowid",
                f"DELETE FROM record_text_fts WHERE rowid IN ({rowids})",
            )
        _ = self._execute(
            "records_search.delete_by_source",
            "DELETE FROM records_search WHERE source_id = ?",
            (source_id,),
        )
        return len(rows)

    def source_ids(self) -> frozenset[str]:
        """Return every source id in the ledger."""
        rows = self._query("sources.ids", "SELECT source_id FROM sources")
        return frozenset(str(row["source_id"]) for row in rows)

    def remove_source(self, source_id: str) -> int:
        """Delete one source's ledger row and records; return removed count.

        Records and their FTS rows go through the external-content
        delete path first - cascade-deleting records rows would leave
        stale FTS token mappings behind.
        """
        with self.connection:
            removed = self._remove_source_records(source_id)
            _ = self._execute(
                "sources.delete",
                "DELETE FROM sources WHERE source_id = ?",
                (source_id,),
            )
        return removed

    def _insert_record(
        self,
        source_id: str,
        record: agentgrep.SearchRecord,
        *,
        record_id: str,
        now: str,
        raw_hash: str,
        normalized_text: str,
        normalized_hash: str,
        features_mode: FeatureMode,
    ) -> str:
        """Insert one normalized record across the search, details, FTS, and feature surfaces."""
        haystack = agentgrep.build_record_match_surface(record, "haystack").casefold()
        cursor = self._execute(
            "records_search.insert",
            """
            INSERT INTO records_search(
                record_id, source_id, kind, agent, store, adapter_id, path,
                timestamp, session_id, conversation_id,
                text_hash, normalized_text_hash, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                source_id,
                record.kind,
                record.agent,
                record.store,
                record.adapter_id,
                str(record.path),
                record.timestamp,
                record.session_id,
                record.conversation_id,
                raw_hash,
                normalized_hash,
                now,
            ),
        )
        lastrowid = cursor.lastrowid
        if lastrowid is None:
            msg = "SQLite did not return a record rowid"
            raise RuntimeError(msg)
        rowid = int(lastrowid)
        _ = self._execute(
            "record_details.insert",
            """
            INSERT INTO record_details(rowid, text, title, role, model, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                rowid,
                record.text,
                record.title,
                record.role,
                record.model,
                _json_dumps(record.metadata),
            ),
        )
        _ = self._execute(
            "fts.insert",
            "INSERT INTO record_text_fts(rowid, haystack) VALUES(?, ?)",
            (rowid, haystack),
        )
        if features_mode == "defer":
            return record_id
        self._insert_feature_values(
            _feature_values_for_input(
                _FeatureInput(
                    record_id=record_id,
                    text=record.text,
                    normalized_hash=normalized_hash,
                    timestamp=record.timestamp,
                    updated_at=now,
                ),
                normalized_text=normalized_text,
            ),
        )
        return record_id

    def _scope_catalog(self) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
        """Classify the synced source ledger for scope predicates.

        Returns the agents holding a prompt-history-role source and the
        conversation-role ``(store, adapter_id)`` pairs present in the
        ledger — both small, bounded sets. Mirrors
        :func:`agentgrep.prompt_history_agents_for_sources` and the
        planner's :func:`agentgrep.source_matches_scope` store
        selection so the gates can run inside SQL.
        """
        rows = self._query(
            "sources.distinct_adapters",
            "SELECT DISTINCT agent, store, adapter_id FROM sources",
        )
        prompt_history_agents: set[str] = set()
        conversation_pairs: set[tuple[str, str]] = set()
        for row in rows:
            store = str(row["store"])
            adapter_id = str(row["adapter_id"])
            role = agentgrep.store_role_for_record(store, adapter_id)
            if role in agentgrep.PROMPT_HISTORY_STORE_ROLES:
                prompt_history_agents.add(str(row["agent"]))
            if role in agentgrep.CONVERSATION_STORE_ROLES:
                conversation_pairs.add((store, adapter_id))
        return frozenset(prompt_history_agents), frozenset(conversation_pairs)

    def _insert_feature_values(self, values: _FeatureValues) -> None:
        """Insert one precomputed feature row."""
        self.connection.execute(
            """
            INSERT INTO record_features(
                record_id, normalized_hash, simhash_hex, minhash_json,
                token_count, quality_flags_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values.record_id,
                values.normalized_hash,
                values.simhash_hex,
                values.minhash_json,
                values.token_count,
                values.quality_flags_json,
                values.updated_at,
            ),
        )

    def refresh_missing_features(
        self,
        *,
        limit: int | None = None,
        workers: int | None = None,
        progress: FeatureRefreshProgress | None = None,
    ) -> int:
        """Build deterministic feature rows missing from deferred syncs."""
        if progress is not None:
            progress.set_activity(
                "checking feature cache",
                detail="querying records missing deterministic features",
            )
        sql = """
            SELECT r.record_id, d.text, r.normalized_text_hash, r.timestamp
            FROM records_search r
            JOIN record_details d ON d.rowid = r.rowid
            LEFT JOIN record_features f ON f.record_id = r.record_id
            WHERE f.record_id IS NULL
            ORDER BY r.rowid
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        if not rows:
            if progress is not None:
                progress.set_activity(
                    "checking feature cache",
                    detail="feature cache already complete",
                )
            return 0
        now = _now_iso()
        inputs = tuple(
            _FeatureInput(
                record_id=str(row["record_id"]),
                text=str(row["text"]),
                normalized_hash=str(row["normalized_text_hash"]),
                timestamp=t.cast("str | None", row["timestamp"]),
                updated_at=now,
            )
            for row in rows
        )
        values = _build_feature_values_batch(inputs, workers=workers, progress=progress)
        if progress is not None:
            progress.set_activity(
                "writing feature cache",
                detail=_format_feature_write_progress(0, len(values)),
            )
        with self.connection:
            for done, item in enumerate(values, start=1):
                self._insert_feature_values(item)
                if progress is not None and _should_report_feature_progress(done, len(values)):
                    progress.set_activity(
                        "writing feature cache",
                        detail=_format_feature_write_progress(done, len(values)),
                    )
        return len(values)

    def search_records(self, query: agentgrep.SearchQuery) -> list[agentgrep.SearchRecord]:
        """Return SearchRecord objects matching ``query`` from SQLite/FTS."""
        try:
            return self._search_records(query)
        finally:
            self._flush_sql_samples()

    def _search_records(self, query: agentgrep.SearchQuery) -> list[agentgrep.SearchRecord]:
        """Serve ``search_records`` ahead of the telemetry flush."""
        if query.regex or query.any_term or query.compiled is not None:
            msg = "query requires live scanner"
            raise DbQueryUnsupported(msg)
        if not query.agents:
            # Live parity: an empty agent selection discovers zero
            # sources. Returning early also avoids generating the
            # nonstandard ``IN ()`` form some SQLite builds reject.
            return []
        params: list[object] = []
        where = ["r.agent IN ({})".format(",".join("?" for _ in query.agents))]
        params.extend(query.agents)
        prompt_history_agents: frozenset[str] = frozenset()
        if query.scope != "all":
            # Push the planner's store-role gates into SQL. Without
            # them the probe pages wade through scope-rejected rows -
            # profiled at 130 of the 200 newest hot-term rows - and pay
            # continuation pages for nothing. The Python scope filter
            # below stays as the authoritative re-check.
            prompt_history_agents, conversation_pairs = self._scope_catalog()
            pair_sql = ",".join("(?, ?)" for _ in conversation_pairs)
            pair_params = [part for pair in sorted(conversation_pairs) for part in pair]
            if query.scope == "prompts":
                # Live prompts-scope results are always kind='prompt',
                # and an agent with a dedicated prompt-history store
                # never serves user turns from its chat stores.
                where.append("r.kind = 'prompt'")
                if conversation_pairs and prompt_history_agents:
                    agent_sql = ",".join("?" for _ in prompt_history_agents)
                    where.append(
                        f"NOT ((r.store, r.adapter_id) IN (VALUES {pair_sql})"
                        f" AND r.agent IN ({agent_sql}))"
                    )
                    params.extend(pair_params)
                    params.extend(sorted(prompt_history_agents))
            elif conversation_pairs:
                where.append(f"(r.store, r.adapter_id) IN (VALUES {pair_sql})")
                params.extend(pair_params)
            else:
                # No conversation-role sources synced: nothing can be
                # in conversations scope.
                where.append("0")
        # The indexed haystack and the query terms are both Python-
        # casefolded, and trigram MATCH is exact for substrings (the
        # study probe showed MATCH == instr on every term), so the FTS
        # candidates equal the haystack-surface matches; matches_record
        # stays the authoritative oracle for surface/case semantics.
        if query.terms and all(_fts_indexable(term) for term in query.terms):
            match_expr = " AND ".join(_quote_fts_term(term.casefold()) for term in query.terms)
            stmt_name = "records.probe_fts"
            from_where = (
                "FROM record_text_fts f "
                "JOIN records_search r ON r.rowid = f.rowid "
                f"WHERE f.record_text_fts MATCH ? AND {' AND '.join(where)}"
            )
            base_params: tuple[object, ...] = (match_expr, *params)
        else:
            scan_where = list(where)
            scan_params: list[object] = list(params)
            for term in query.terms:
                scan_where.append("instr(f.haystack, ?) > 0")
                scan_params.append(term.casefold())
            stmt_name = "records.probe_scan"
            from_where = (
                "FROM record_text_fts f "
                "JOIN records_search r ON r.rowid = f.rowid "
                f"WHERE {' AND '.join(scan_where)}"
            )
            base_params = tuple(scan_params)
        if query.limit is None:
            return self._search_all(
                query, stmt_name, from_where, base_params, prompt_history_agents
            )
        return self._search_limited(
            query, stmt_name, from_where, base_params, prompt_history_agents
        )

    _PROBE_COLUMNS = (
        "r.rowid, r.kind, r.agent, r.store, r.adapter_id, "
        "COALESCE(r.timestamp,'') AS sort_ts, r.agent AS sort_agent, "
        "r.path AS sort_path"
    )
    _PROBE_ORDER = "ORDER BY sort_ts DESC, r.agent DESC, r.path DESC, r.rowid DESC"

    def _search_limited(
        self,
        query: agentgrep.SearchQuery,
        stmt_name: str,
        from_where: str,
        base_params: tuple[object, ...],
        prompt_history_agents: frozenset[str],
    ) -> list[agentgrep.SearchRecord]:
        """Serve a limited search through the keyset probe.

        Probe pages arrive in the deterministic total order
        ``(COALESCE(timestamp,''), agent, path, rowid) DESC`` — the live
        sort key plus rowid as the unique tiebreaker — so once the
        cursor passes a row, nothing below it can outrank an accepted
        survivor. The window is sealed only when ``limit`` records
        survive scope, dedup, AND the ``matches_record`` oracle; an
        under-filled window continues from the row-value cursor.
        """
        limit = query.limit
        assert limit is not None
        results: list[agentgrep.SearchRecord] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        window = self._initial_probe_window(limit)
        cursor: tuple[str, str, str, int] | None = None
        page_index = 0
        while len(results) < limit:
            page_index += 1
            page_started = time.perf_counter()
            cursor_sql = ""
            page_params: tuple[object, ...] = base_params
            if cursor is not None:
                cursor_sql = (
                    " AND (COALESCE(r.timestamp,''), r.agent, r.path, r.rowid) < (?, ?, ?, ?)"
                )
                page_params = (*base_params, *cursor)
            page = self._query(
                stmt_name,
                f"SELECT {self._PROBE_COLUMNS} {from_where}{cursor_sql} "
                f"{self._PROBE_ORDER} LIMIT ?",
                (*page_params, window),
            )
            if not page:
                break
            admitted = [
                row
                for row in page
                if query.scope == "all"
                or _cached_scope_admits(
                    str(row["kind"]),
                    str(row["store"]),
                    str(row["adapter_id"]),
                    str(row["agent"]),
                    query.scope,
                    prompt_history_agents,
                )
            ]
            by_rowid = self._hydrate([int(row["rowid"]) for row in admitted])
            for row in admitted:
                record = by_rowid.get(int(row["rowid"]))
                if record is None or not agentgrep.matches_record(record, query):
                    continue
                if query.dedupe:
                    key = agentgrep.record_dedupe_key(record)
                    if key in seen:
                        continue
                    seen.add(key)
                results.append(record)
                if len(results) >= limit:
                    break
            # One aggregate sample per probe page: the funnel from
            # fetched rows to oracle-surviving results explains page
            # continuations (e.g. text-surface queries rejecting
            # haystack-only hits) without per-record samples.
            agentgrep._record_engine_profile_sample(
                "db.probe.page",
                time.perf_counter() - page_started,
                agentgrep_probe_page=page_index,
                agentgrep_probe_window=window,
                agentgrep_probe_rows=len(page),
                agentgrep_probe_admitted=len(admitted),
                agentgrep_probe_kept=len(results),
            )
            if len(page) < window:
                break
            last = page[-1]
            cursor = (
                str(last["sort_ts"]),
                str(last["sort_agent"]),
                str(last["sort_path"]),
                int(last["rowid"]),
            )
            window *= 4
        return results

    @staticmethod
    def _initial_probe_window(limit: int) -> int:
        """Return the first probe window for a limited search."""
        return max(4 * limit, _PROBE_WINDOW_FLOOR)

    def _search_all(
        self,
        query: agentgrep.SearchQuery,
        stmt_name: str,
        from_where: str,
        base_params: tuple[object, ...],
        prompt_history_agents: frozenset[str],
    ) -> list[agentgrep.SearchRecord]:
        """Serve an unlimited search: lean fetch, hydrate survivors, finish."""
        rows = self._query(
            stmt_name,
            f"SELECT {self._PROBE_COLUMNS} {from_where}",
            base_params,
        )
        admitted = [
            row
            for row in rows
            if query.scope == "all"
            or _cached_scope_admits(
                str(row["kind"]),
                str(row["store"]),
                str(row["adapter_id"]),
                str(row["agent"]),
                query.scope,
                prompt_history_agents,
            )
        ]
        # Same deterministic total order as the probe path: the live
        # sort tuple plus rowid DESC as the unique tiebreaker.
        admitted.sort(
            key=lambda row: (
                str(row["sort_ts"]),
                str(row["sort_agent"]),
                str(row["sort_path"]),
                int(row["rowid"]),
            ),
            reverse=True,
        )
        by_rowid = self._hydrate([int(row["rowid"]) for row in admitted])
        seen: set[tuple[str, str, str, str, str]] = set()
        results: list[agentgrep.SearchRecord] = []
        for row in admitted:
            record = by_rowid.get(int(row["rowid"]))
            if record is None or not agentgrep.matches_record(record, query):
                continue
            if query.dedupe:
                key = agentgrep.record_dedupe_key(record)
                if key in seen:
                    continue
                seen.add(key)
            results.append(record)
        return results

    def _hydrate(self, rowids: list[int]) -> dict[int, agentgrep.SearchRecord]:
        """Fetch full records for ``rowids``, keyed by rowid."""
        out: dict[int, agentgrep.SearchRecord] = {}
        for start in range(0, len(rowids), 500):
            chunk = rowids[start : start + 500]
            id_list = ",".join(str(rowid) for rowid in chunk)
            rows = self._query(
                "records.hydrate",
                "SELECT r.*, d.text, d.title, d.role, d.model, d.metadata_json "
                "FROM records_search r JOIN record_details d ON d.rowid = r.rowid "
                f"WHERE r.rowid IN ({id_list})",
            )
            for row in rows:
                out[int(row["rowid"])] = self._row_to_record(row)
        return out

    def iter_record_rows(self) -> tuple[DbRecordRow, ...]:
        """Return every indexed record with its db id."""
        rows = self._query(
            "records.all",
            "SELECT r.*, d.text, d.title, d.role, d.model, d.metadata_json "
            "FROM records_search r JOIN record_details d ON d.rowid = r.rowid "
            "ORDER BY r.rowid",
        )
        return tuple(
            DbRecordRow(
                record_id=str(row["record_id"]),
                record=self._row_to_record(row),
            )
            for row in rows
        )

    def iter_similarity_rows(self) -> tuple[DbSimilarityRow, ...]:
        """Return record ids with precomputed normalized hashes for similarity."""
        rows = self.connection.execute(
            """
            SELECT record_id, normalized_text_hash
            FROM records_search
            ORDER BY rowid
            """,
        ).fetchall()
        return tuple(
            DbSimilarityRow(
                record_id=str(row["record_id"]),
                normalized_hash=str(row["normalized_text_hash"]),
            )
            for row in rows
        )

    def get_record_row(self, record_id: str) -> DbRecordRow | None:
        """Return one indexed record row by id."""
        rows = self._query(
            "records.get",
            "SELECT r.*, d.text, d.title, d.role, d.model, d.metadata_json "
            "FROM records_search r JOIN record_details d ON d.rowid = r.rowid "
            "WHERE r.record_id = ?",
            (record_id,),
        )
        if not rows:
            return None
        return DbRecordRow(record_id=record_id, record=self._row_to_record(rows[0]))

    def _row_to_record(self, row: sqlite3.Row) -> agentgrep.SearchRecord:
        """Convert one SQLite row into the public SearchRecord dataclass."""
        return agentgrep.SearchRecord(
            kind=t.cast("t.Literal['prompt', 'history']", str(row["kind"])),
            agent=t.cast("agentgrep.AgentName", str(row["agent"])),
            store=str(row["store"]),
            adapter_id=str(row["adapter_id"]),
            path=pathlib.Path(str(row["path"])),
            text=str(row["text"]),
            title=t.cast("str | None", row["title"]),
            role=t.cast("str | None", row["role"]),
            timestamp=t.cast("str | None", row["timestamp"]),
            model=t.cast("str | None", row["model"]),
            session_id=t.cast("str | None", row["session_id"]),
            conversation_id=t.cast("str | None", row["conversation_id"]),
            metadata=_json_loads_mapping(str(row["metadata_json"])),
        )


class DbRuntime:
    """Headless DB runtime used by CLI, MCP, and tests."""

    def __init__(self, store: DbStore) -> None:
        self.store = store

    @classmethod
    def open(cls, db_path: pathlib.Path | str | None = None) -> DbRuntime:
        """Open a DB runtime at ``db_path`` or the default path."""
        return cls(DbStore.open(db_path))

    @classmethod
    def open_readonly(cls, db_path: pathlib.Path | str | None = None) -> DbRuntime:
        """Open a read-only DB runtime for status surfaces."""
        return cls(DbStore.open_readonly(db_path))

    def close(self) -> None:
        """Close the underlying store connection."""
        self.store.close()

    def __enter__(self) -> DbRuntime:
        """Return the runtime for use as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the underlying store connection on context exit."""
        self.close()

    def status(self) -> DbStatus:
        """Return DB status counters."""
        return self.store.status()

    def explain(self) -> DbExplain:
        """Return cache diagnostics."""
        return self.store.explain()

    def sync_records(
        self,
        batches: cabc.Iterable[SourceRecordBatch],
        *,
        control: agentgrep.SearchControl | None = None,
        progress: DbSyncProgress | None = None,
        features_mode: FeatureMode = "defer",
        force: bool = False,
        coverage: SyncCoverage | None = None,
        prune_missing: bool = False,
    ) -> SyncResult:
        """Sync explicit source/record batches into the DB.

        ``coverage`` is merged into the coverage map only when it is
        marked complete AND the loop visits every batch — early exits
        and interruptions record nothing, so coverage always reflects
        the last sync that actually finished. ``prune_missing``
        deletes ledger rows (and their records) for sources absent
        from the batch set, and likewise applies only to loops that
        run to the end; callers must pass it only for uncapped,
        full-scope syncs so a narrowed run cannot prune other agents.
        """
        try:
            return self._sync_records(
                batches,
                control=control,
                progress=progress,
                features_mode=features_mode,
                force=force,
                coverage=coverage,
                prune_missing=prune_missing,
            )
        finally:
            self.store._flush_sql_samples()

    def _sync_records(
        self,
        batches: cabc.Iterable[SourceRecordBatch],
        *,
        control: agentgrep.SearchControl | None = None,
        progress: DbSyncProgress | None = None,
        features_mode: FeatureMode = "defer",
        force: bool = False,
        coverage: SyncCoverage | None = None,
        prune_missing: bool = False,
    ) -> SyncResult:
        """Run the sync loop ahead of the telemetry flush."""
        result = SyncResult(
            sources_synced=0,
            records_indexed=0,
            records_removed=0,
        )
        seen_source_ids: set[str] = set()
        if progress is None:
            for source, records in batches:
                if control is not None and control.answer_now_requested():
                    return result
                seen_source_ids.add(source_id_for(source))
                result, _indexed, _removed = self._sync_one_source(
                    source,
                    records,
                    result=result,
                    features_mode=features_mode,
                    force=force,
                )
            result = self._finish_complete_sync(
                result,
                coverage=coverage,
                prune_missing=prune_missing,
                seen_source_ids=seen_source_ids,
            )
            return result

        batch_list = tuple(batches)
        total = len(batch_list)
        progress.start(total)
        for index, (source, records) in enumerate(batch_list, start=1):
            if control is not None and control.answer_now_requested():
                progress.exiting_early(result)
                return result
            seen_source_ids.add(source_id_for(source))
            progress.source_started(index, total, source, result)
            result, indexed, removed = self._sync_one_source(
                source,
                records,
                result=result,
                features_mode=features_mode,
                force=force,
            )
            progress.source_finished(index, total, source, indexed, removed, result)
        result = self._finish_complete_sync(
            result,
            coverage=coverage,
            prune_missing=prune_missing,
            seen_source_ids=seen_source_ids,
        )
        progress.finish(result)
        return result

    def _finish_complete_sync(
        self,
        result: SyncResult,
        *,
        coverage: SyncCoverage | None,
        prune_missing: bool,
        seen_source_ids: set[str],
    ) -> SyncResult:
        """Apply end-of-loop effects for a sync that visited every batch."""
        if prune_missing:
            pruned = 0
            removed = 0
            for source_id in sorted(self.store.source_ids() - seen_source_ids):
                removed += self.store.remove_source(source_id)
                pruned += 1
            if pruned:
                result = dataclasses.replace(
                    result,
                    records_removed=result.records_removed + removed,
                    sources_pruned=result.sources_pruned + pruned,
                )
        if coverage is not None and coverage.complete:
            self.store.merge_coverage(coverage)
        return result

    def covers_query(self, query: agentgrep.SearchQuery) -> bool:
        """Return whether coverage spans the query's agents and scope."""
        return self.store.covers(query.agents, query.scope)

    def _sync_one_source(
        self,
        source: agentgrep.SourceHandle,
        records: cabc.Iterable[agentgrep.SearchRecord],
        *,
        result: SyncResult,
        features_mode: FeatureMode,
        force: bool,
    ) -> tuple[SyncResult, int, int]:
        """Sync one source and return updated counters plus source deltas."""
        if not force and self.store.source_is_current(source):
            return (
                SyncResult(
                    sources_synced=result.sources_synced,
                    records_indexed=result.records_indexed,
                    records_removed=result.records_removed,
                    sources_skipped=result.sources_skipped + 1,
                    features_deferred=result.features_deferred,
                ),
                0,
                0,
            )
        indexed, removed, deferred = self.store.replace_source_records(
            source,
            records,
            features_mode=features_mode,
        )
        return (
            SyncResult(
                sources_synced=result.sources_synced + 1,
                records_indexed=result.records_indexed + indexed,
                records_removed=result.records_removed + removed,
                sources_skipped=result.sources_skipped,
                features_deferred=result.features_deferred + deferred,
            ),
            indexed,
            removed,
        )

    def sync_sources(
        self,
        sources: cabc.Iterable[agentgrep.SourceHandle],
        *,
        control: agentgrep.SearchControl | None = None,
        progress: DbSyncProgress | None = None,
        features_mode: FeatureMode = "defer",
        force: bool = False,
        coverage: SyncCoverage | None = None,
        prune_missing: bool = False,
    ) -> SyncResult:
        """Read records from existing adapters and sync them into the DB."""
        return self.sync_records(
            ((source, agentgrep.iter_source_records(source)) for source in sources),
            control=control,
            progress=progress,
            features_mode=features_mode,
            force=force,
            coverage=coverage,
            prune_missing=prune_missing,
        )

    def search_records(self, query: agentgrep.SearchQuery) -> list[agentgrep.SearchRecord]:
        """Search the DB index."""
        return self.store.search_records(query)


def simhash_hex(text: str) -> str:
    """Return a deterministic 64-bit SimHash as fixed-width hex.

    Examples
    --------
    >>> simhash_hex("Run ruff check.")
    'a33bc7c107168285'
    >>> simhash_hex("")
    '0000000000000000'
    """
    return _simhash_hex_from_tokens(token_set(text))


def _simhash_hex_from_tokens(tokens: cabc.Iterable[str]) -> str:
    """Return a deterministic 64-bit SimHash for pre-tokenized text."""
    weights = [0] * 64
    for token in tokens:
        digest = int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16)
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    value = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            value |= 1 << bit
    return f"{value:016x}"


def minhash_signature(text: str, *, size: int = 16) -> list[str]:
    """Return a small deterministic MinHash-style signature.

    Examples
    --------
    >>> minhash_signature("ruff check", size=4)[0]
    '7cd7bdec93a8d7d1'
    >>> minhash_signature("", size=4)
    []
    """
    return _minhash_signature_from_tokens(token_set(text), size=size)


def _minhash_signature_from_tokens(tokens: cabc.Iterable[str], *, size: int = 16) -> list[str]:
    """Return a small deterministic MinHash-style signature for tokens."""
    sorted_tokens = sorted(tokens)
    if not sorted_tokens:
        return []
    signature: list[str] = []
    for index in range(size):
        minimum = min(
            hashlib.blake2b(
                f"{index}\0{token}".encode(),
                digest_size=8,
            ).hexdigest()
            for token in sorted_tokens
        )
        signature.append(minimum)
    return signature


def _quality_flags_for_text(text: str, *, timestamp: str | None) -> dict[str, object]:
    """Return lightweight quality/noise flags from scalar record fields."""
    stripped = text.strip()
    return {
        "empty": not stripped,
        "short": len(stripped) < 12,
        "has_timestamp": timestamp is not None,
    }


def quality_flags(record: agentgrep.SearchRecord) -> dict[str, object]:
    """Return lightweight quality/noise flags for a record."""
    return _quality_flags_for_text(record.text, timestamp=record.timestamp)


def _feature_values_for_input(
    item: _FeatureInput,
    *,
    normalized_text: str | None = None,
) -> _FeatureValues:
    """Build feature values for one record without touching SQLite."""
    effective_normalized_text = (
        normalize_record_text(item.text) if normalized_text is None else normalized_text
    )
    tokens = frozenset(effective_normalized_text.split())
    return _FeatureValues(
        record_id=item.record_id,
        normalized_hash=item.normalized_hash,
        simhash_hex=_simhash_hex_from_tokens(tokens),
        minhash_json=_json_dumps(_minhash_signature_from_tokens(tokens)),
        token_count=len(tokens),
        quality_flags_json=_json_dumps(
            _quality_flags_for_text(item.text, timestamp=item.timestamp),
        ),
        updated_at=item.updated_at,
    )


def _build_feature_values_batch(
    inputs: tuple[_FeatureInput, ...],
    *,
    workers: int | None,
    progress: FeatureRefreshProgress | None = None,
) -> tuple[_FeatureValues, ...]:
    """Build feature rows, using processes only for sufficiently large batches."""
    if not inputs:
        return ()
    worker_count = workers
    if worker_count is None:
        worker_count = min(4, os.cpu_count() or 1)
    if worker_count <= 1 or len(inputs) < 512:
        return _build_feature_values_inline(inputs, progress=progress)
    if progress is not None:
        progress.set_activity(
            "building feature signatures",
            detail=_format_feature_build_progress(0, len(inputs), workers=worker_count),
        )
    values: list[_FeatureValues] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as executor:
        for done, value in enumerate(
            executor.map(_feature_values_for_input, inputs, chunksize=128),
            start=1,
        ):
            values.append(value)
            if progress is not None and _should_report_feature_progress(done, len(inputs)):
                progress.set_activity(
                    "building feature signatures",
                    detail=_format_feature_build_progress(done, len(inputs), workers=worker_count),
                )
    return tuple(values)


def _build_feature_values_inline(
    inputs: tuple[_FeatureInput, ...],
    *,
    progress: FeatureRefreshProgress | None,
) -> tuple[_FeatureValues, ...]:
    """Build feature rows in-process while reporting bounded progress."""
    if progress is not None:
        progress.set_activity(
            "building feature signatures",
            detail=_format_feature_build_progress(0, len(inputs), workers=1),
        )
    values: list[_FeatureValues] = []
    for done, item in enumerate(inputs, start=1):
        values.append(_feature_values_for_input(item))
        if progress is not None and _should_report_feature_progress(done, len(inputs)):
            progress.set_activity(
                "building feature signatures",
                detail=_format_feature_build_progress(done, len(inputs), workers=1),
            )
    return tuple(values)


def _should_report_feature_progress(done: int, total: int) -> bool:
    """Return whether feature progress should emit for this count."""
    if done == total:
        return True
    interval = min(_FEATURE_PROGRESS_INTERVAL, max(1, total // 20))
    return done % interval == 0


def _format_feature_build_progress(done: int, total: int, *, workers: int) -> str:
    """Return a compact feature-build progress detail."""
    percent = 100.0 if total == 0 else done / total * 100
    return f"{done:,}/{total:,} rows built ({percent:.1f}%, {workers:,}w)"


def _format_feature_write_progress(done: int, total: int) -> str:
    """Return a compact feature-cache write progress detail."""
    percent = 100.0 if total == 0 else done / total * 100
    return f"{done:,}/{total:,} rows written ({percent:.1f}%)"
