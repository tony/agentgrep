"""Persistent SQLite DB index for normalized agent data."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import typing as t
import unicodedata

import agentgrep
from agentgrep._engine.scanning import _CACHE_EXEMPT_ADAPTERS

CacheMode = t.Literal["auto", "require", "off"]

SCHEMA_VERSION = 1
DEFAULT_DB_FILENAME = "agentgrep.sqlite"
_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")


@dataclasses.dataclass(frozen=True, slots=True)
class DbStatus:
    """Summary of the persisted DB index."""

    db_path: pathlib.Path
    schema_version: int
    sources: int
    records: int


@dataclasses.dataclass(frozen=True, slots=True)
class SyncResult:
    """Counters returned by a DB sync operation."""

    sources_synced: int
    records_indexed: int
    records_removed: int
    sources_skipped: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class DbRecordRow:
    """One record row with its stable DB id."""

    record_id: str
    record: agentgrep.SearchRecord


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
    """Normalize text for deterministic hashes and similarity features."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = [token.strip(".,;:!?") for token in _TOKEN_RE.findall(normalized)]
    return " ".join(token for token in tokens if token)


def token_set(text: str) -> frozenset[str]:
    """Return deterministic lowercase tokens for lightweight similarity."""
    return frozenset(normalize_record_text(text).split())


def text_hash(text: str) -> str:
    """Return a stable SHA-256 hex digest for ``text``."""
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
    """Quote one user term for an FTS5 MATCH expression."""
    return '"' + term.replace('"', '""') + '"'


class DbStore:
    """SQLite-backed store for the persistent DB index."""

    def __init__(self, db_path: pathlib.Path) -> None:
        self.db_path = db_path
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

    def close(self) -> None:
        """Close the SQLite connection."""
        self.connection.close()

    def _configure(self) -> None:
        """Configure connection-local SQLite settings."""
        _ = self.connection.execute("PRAGMA journal_mode=WAL")
        _ = self.connection.execute("PRAGMA foreign_keys=ON")

    def _migrate(self) -> None:
        """Create or upgrade the SQLite schema."""
        with self.connection:
            self.connection.executescript(
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

                CREATE TABLE IF NOT EXISTS records (
                    rowid INTEGER PRIMARY KEY,
                    record_id TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    store TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT,
                    role TEXT,
                    timestamp TEXT,
                    model TEXT,
                    session_id TEXT,
                    conversation_id TEXT,
                    text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    normalized_text_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS record_text_fts
                USING fts5(title, text, content='records', content_rowid='rowid');

                CREATE INDEX IF NOT EXISTS idx_records_source_id
                ON records(source_id);
                """
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def status(self) -> DbStatus:
        """Return db row counts."""
        return DbStatus(
            db_path=self.db_path,
            schema_version=SCHEMA_VERSION,
            sources=self._count("sources"),
            records=self._count("records"),
        )

    def _count(self, table: str) -> int:
        """Return row count for a known table."""
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"]) if row is not None else 0

    def replace_source_records(
        self,
        source: agentgrep.SourceHandle,
        records: cabc.Iterable[agentgrep.SearchRecord],
    ) -> tuple[int, int]:
        """Replace every indexed record for ``source``.

        Returns
        -------
        tuple[int, int]
            ``(records_indexed, records_removed)``.
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
                    normalized_hash=normalized_hash,
                )
                indexed += 1
            self.connection.execute(
                """
                INSERT OR REPLACE INTO source_state(
                    source_id, sync_status, synced_mtime_ns,
                    synced_fingerprint, last_error, updated_at
                )
                VALUES(?, 'ok', ?, ?, NULL, ?)
                """,
                (source_id, source.mtime_ns, fingerprint, now),
            )
        return indexed, removed

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
        row = self.connection.execute(
            """
            SELECT sync_status, synced_mtime_ns, synced_fingerprint
            FROM source_state
            WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()
        if row is None:
            return False
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
        self.connection.execute(
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

        External-content FTS5 ``'delete'`` commands must receive the
        originally indexed column values; deleting with placeholder
        values leaves stale token mappings behind and corrupts later
        ``MATCH`` queries against reused rowids.
        """
        rows = self.connection.execute(
            "SELECT rowid, title, text FROM records WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "INSERT INTO record_text_fts(record_text_fts, rowid, title, text) "
                "VALUES('delete', ?, ?, ?)",
                (int(row["rowid"]), row["title"] or "", row["text"]),
            )
        self.connection.execute("DELETE FROM records WHERE source_id = ?", (source_id,))
        return len(rows)

    def _insert_record(
        self,
        source_id: str,
        record: agentgrep.SearchRecord,
        *,
        record_id: str,
        now: str,
        raw_hash: str,
        normalized_hash: str,
    ) -> str:
        """Insert one normalized record and its FTS row."""
        cursor = self.connection.execute(
            """
            INSERT INTO records(
                record_id, source_id, kind, agent, store, adapter_id, path,
                title, role, timestamp, model, session_id, conversation_id,
                text, metadata_json, text_hash, normalized_text_hash, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                source_id,
                record.kind,
                record.agent,
                record.store,
                record.adapter_id,
                str(record.path),
                record.title,
                record.role,
                record.timestamp,
                record.model,
                record.session_id,
                record.conversation_id,
                record.text,
                _json_dumps(record.metadata),
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
        self.connection.execute(
            "INSERT INTO record_text_fts(rowid, title, text) VALUES(?, ?, ?)",
            (rowid, record.title or "", record.text),
        )
        return record_id

    def search_records(self, query: agentgrep.SearchQuery) -> list[agentgrep.SearchRecord]:
        """Return SearchRecord objects matching ``query`` from SQLite/FTS."""
        if query.regex or query.any_term or query.compiled is not None:
            msg = "query requires live scanner"
            raise DbQueryUnsupported(msg)
        params: list[object] = []
        where = ["r.agent IN ({})".format(",".join("?" for _ in query.agents))]
        params.extend(query.agents)
        if query.scope == "prompts":
            where.append("r.kind = 'prompt'")
        elif query.scope == "conversations":
            where.append("r.kind = 'history'")
        if query.terms:
            match_expr = " AND ".join(_quote_fts_term(term) for term in query.terms)
            sql = (
                "SELECT r.* FROM record_text_fts f "
                "JOIN records r ON r.rowid = f.rowid "
                f"WHERE f.record_text_fts MATCH ? AND {' AND '.join(where)}"
            )
            rows = self.connection.execute(sql, (match_expr, *params)).fetchall()
        else:
            rows = self.connection.execute(
                f"SELECT r.* FROM records r WHERE {' AND '.join(where)}",
                params,
            ).fetchall()
        records = [self._row_to_record(row) for row in rows]
        filtered = [record for record in records if agentgrep.matches_record(record, query)]
        filtered.sort(key=agentgrep.search_record_sort_key, reverse=True)
        return filtered[: query.limit] if query.limit is not None else filtered

    def iter_record_rows(self) -> tuple[DbRecordRow, ...]:
        """Return every indexed record with its db id."""
        rows = self.connection.execute("SELECT * FROM records ORDER BY rowid").fetchall()
        return tuple(
            DbRecordRow(
                record_id=str(row["record_id"]),
                record=self._row_to_record(row),
            )
            for row in rows
        )

    def get_record_row(self, record_id: str) -> DbRecordRow | None:
        """Return one indexed record row by id."""
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return DbRecordRow(record_id=record_id, record=self._row_to_record(row))

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

    def status(self) -> DbStatus:
        """Return DB status counters."""
        return self.store.status()

    def sync_records(
        self,
        batches: cabc.Iterable[SourceRecordBatch],
        *,
        control: agentgrep.SearchControl | None = None,
        progress: DbSyncProgress | None = None,
        force: bool = False,
    ) -> SyncResult:
        """Sync explicit source/record batches into the DB."""
        result = SyncResult(
            sources_synced=0,
            records_indexed=0,
            records_removed=0,
        )
        if progress is None:
            for source, records in batches:
                if control is not None and control.answer_now_requested():
                    return result
                result, _indexed, _removed = self._sync_one_source(
                    source,
                    records,
                    result=result,
                    force=force,
                )
            return result

        batch_list = tuple(batches)
        total = len(batch_list)
        progress.start(total)
        for index, (source, records) in enumerate(batch_list, start=1):
            if control is not None and control.answer_now_requested():
                progress.exiting_early(result)
                return result
            progress.source_started(index, total, source, result)
            result, indexed, removed = self._sync_one_source(
                source,
                records,
                result=result,
                force=force,
            )
            progress.source_finished(index, total, source, indexed, removed, result)
        progress.finish(result)
        return result

    def _sync_one_source(
        self,
        source: agentgrep.SourceHandle,
        records: cabc.Iterable[agentgrep.SearchRecord],
        *,
        result: SyncResult,
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
                ),
                0,
                0,
            )
        indexed, removed = self.store.replace_source_records(source, records)
        return (
            SyncResult(
                sources_synced=result.sources_synced + 1,
                records_indexed=result.records_indexed + indexed,
                records_removed=result.records_removed + removed,
                sources_skipped=result.sources_skipped,
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
        force: bool = False,
    ) -> SyncResult:
        """Read records from existing adapters and sync them into the DB."""
        return self.sync_records(
            ((source, agentgrep.iter_source_records(source)) for source in sources),
            control=control,
            progress=progress,
            force=force,
        )

    def search_records(self, query: agentgrep.SearchQuery) -> list[agentgrep.SearchRecord]:
        """Search the DB index."""
        return self.store.search_records(query)
