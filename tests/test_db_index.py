"""Tests for the persistent DB, insights, and suggestions layers."""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.db import DbRuntime, DbStatus, DbSyncProgress, SyncCoverage, SyncResult
from agentgrep.insights import InsightEngine, VariantEdge
from agentgrep.suggestions import SuggestionArtifact, SuggestionEngine


class CachedSearchCase(t.NamedTuple):
    """Named case for cache-backed search runtime behavior."""

    test_id: str
    cache_mode: agentgrep.CacheMode
    expected_texts: tuple[str, ...]


class DuplicateRecordCase(t.NamedTuple):
    """Named case for duplicate source record identity behavior."""

    test_id: str
    texts: tuple[str, ...]
    expected_records: int


class SyncFeatureModeCase(t.NamedTuple):
    """Named case for feature-generation behavior during DB sync."""

    test_id: str
    features_mode: t.Literal["defer", "inline"]
    expected_features: int
    expected_deferred: int


class StopAfterFirstSourceProgress:
    """Progress stub that requests early exit after one source transaction."""

    def __init__(self, control: agentgrep.SearchControl) -> None:
        self._control = control
        self.started_total: int | None = None
        self.finished_sources: list[str] = []
        self.finished_cleanly = False
        self.early_result: SyncResult | None = None

    def start(self, total_sources: int) -> None:
        """Capture the planned source count."""
        self.started_total = total_sources

    def source_started(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        result: SyncResult,
    ) -> None:
        """Accept source-start events."""
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
        """Request early exit once the first source is fully committed."""
        _ = (index, total, records_indexed, records_removed, result)
        self.finished_sources.append(source.path.name)
        self._control.request_answer_now()

    def finish(self, result: SyncResult) -> None:
        """Record an unexpected clean finish."""
        _ = result
        self.finished_cleanly = True

    def exiting_early(self, result: SyncResult) -> None:
        """Capture the partial result returned after early exit."""
        self.early_result = result


class InsightActivityProgress:
    """Progress stub that records insight engine activity labels."""

    def __init__(self) -> None:
        self.activities: list[tuple[str, str | None]] = []

    def set_activity(self, activity: str, *, detail: str | None = None) -> None:
        """Capture one current-work update."""
        self.activities.append((activity, detail))


CACHED_SEARCH_CASES: tuple[CachedSearchCase, ...] = (
    CachedSearchCase(
        test_id="require-uses-index",
        cache_mode="require",
        expected_texts=("Run ruff check before committing.",),
    ),
    CachedSearchCase(
        test_id="off-bypasses-index",
        cache_mode="off",
        expected_texts=("live scanner ruff result",),
    ),
)


DUPLICATE_RECORD_CASES: tuple[DuplicateRecordCase, ...] = (
    DuplicateRecordCase(
        test_id="identical-native-identity",
        texts=(
            "Run ruff check before committing.",
            "Run ruff check before committing.",
        ),
        expected_records=2,
    ),
)


SYNC_FEATURE_MODE_CASES: tuple[SyncFeatureModeCase, ...] = (
    SyncFeatureModeCase(
        test_id="default-defer",
        features_mode="defer",
        expected_features=0,
        expected_deferred=2,
    ),
    SyncFeatureModeCase(
        test_id="inline-features",
        features_mode="inline",
        expected_features=2,
        expected_deferred=0,
    ),
)


def _source(
    path: pathlib.Path,
    *,
    source_kind: agentgrep.SourceKind = "jsonl",
    adapter_id: str = "codex.sessions_jsonl.v1",
    agent: agentgrep.AgentName = "codex",
    store: str = "codex.sessions",
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for db tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=path,
        path_kind="session_file",
        source_kind=source_kind,
        search_root=path.parent,
        mtime_ns=path.stat().st_mtime_ns if path.exists() else 0,
    )


def _record(
    source: agentgrep.SourceHandle,
    text: str,
    *,
    kind: t.Literal["prompt", "history"] = "prompt",
    timestamp: str = "2026-06-05T12:00:00Z",
    session_id: str = "session-a",
    title: str | None = None,
    model: str | None = None,
    role: str | None = None,
) -> agentgrep.SearchRecord:
    """Build a synthetic normalized search record."""
    return agentgrep.SearchRecord(
        kind=kind,
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=title,
        model=model,
        role=role,
        timestamp=timestamp,
        session_id=session_id,
    )


def _query(
    term: str = "ruff",
    *,
    dedupe: bool = True,
    scope: agentgrep.SearchScope = "prompts",
    agents: tuple[agentgrep.AgentName, ...] = ("codex",),
) -> agentgrep.SearchQuery:
    """Build a cache-supported text search query."""
    return agentgrep.SearchQuery(
        terms=(term,),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=None,
        dedupe=dedupe,
    )


def test_db_runtime_syncs_records_and_serves_fts_results(
    tmp_path: pathlib.Path,
) -> None:
    """A synced db stores normalized records and returns SearchRecord objects."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    result = runtime.sync_records(
        (
            (
                source,
                (
                    _record(source, "Run ruff check before committing."),
                    _record(source, "Run pytest for the focused suite."),
                ),
            ),
        ),
    )

    assert result.sources_synced == 1
    assert result.records_indexed == 2
    status = runtime.status()
    assert isinstance(status, DbStatus)
    assert status.sources == 1
    assert status.records == 2
    assert [record.text for record in runtime.search_records(_query("ruff"))] == [
        "Run ruff check before committing.",
    ]


@pytest.mark.parametrize(
    "case",
    SYNC_FEATURE_MODE_CASES,
    ids=[case.test_id for case in SYNC_FEATURE_MODE_CASES],
)
def test_db_runtime_sync_feature_modes(
    case: SyncFeatureModeCase,
    tmp_path: pathlib.Path,
) -> None:
    """DB sync can defer expensive feature generation without breaking FTS."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    result = runtime.sync_records(
        (
            (
                source,
                (
                    _record(source, "Run ruff check before committing."),
                    _record(source, "Run pytest for the focused suite."),
                ),
            ),
        ),
        features_mode=case.features_mode,
    )

    assert result.records_indexed == 2
    assert result.features_deferred == case.expected_deferred
    assert runtime.status().features == case.expected_features
    assert [record.text for record in runtime.search_records(_query("ruff"))] == [
        "Run ruff check before committing.",
    ]


def test_db_runtime_sync_skips_unchanged_sources_without_reading_records(
    tmp_path: pathlib.Path,
) -> None:
    """Fresh source_state lets repeated sync avoid opening the record stream."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    def records_that_should_not_be_read() -> t.Iterator[agentgrep.SearchRecord]:
        msg = "unchanged source records should not be consumed"
        raise AssertionError(msg)
        yield _record(source, "unreachable")

    result = runtime.sync_records(((source, records_that_should_not_be_read()),))

    assert result == SyncResult(
        sources_synced=0,
        records_indexed=0,
        records_removed=0,
        sources_skipped=1,
        features_deferred=0,
    )
    assert runtime.status().records == 1


def test_db_runtime_sync_force_resyncs_unchanged_sources(
    tmp_path: pathlib.Path,
) -> None:
    """Forced sync ignores source_state when the caller wants a full refresh."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    result = runtime.sync_records(
        ((source, (_record(source, "Run pytest before committing."),)),),
        force=True,
    )

    assert result.sources_synced == 1
    assert result.sources_skipped == 0
    assert result.records_indexed == 1
    assert result.records_removed == 1
    assert [record.text for record in runtime.search_records(_query("pytest"))] == [
        "Run pytest before committing.",
    ]


def test_db_store_source_id_delete_uses_index(
    tmp_path: pathlib.Path,
) -> None:
    """Source replacement must not scan all records for each source delete."""
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    rows = runtime.store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM records_search WHERE source_id = ?",
        ("source-a",),
    ).fetchall()
    plan = " ".join(str(row["detail"]) for row in rows)

    assert "idx_records_search_source_id" in plan
    assert "SCAN records_search" not in plan


@pytest.mark.parametrize(
    "case",
    DUPLICATE_RECORD_CASES,
    ids=[case.test_id for case in DUPLICATE_RECORD_CASES],
)
def test_db_runtime_preserves_duplicate_source_records(
    case: DuplicateRecordCase,
    tmp_path: pathlib.Path,
) -> None:
    """A source can contain repeated native records without breaking sync."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    result = runtime.sync_records(
        (
            (
                source,
                tuple(_record(source, text) for text in case.texts),
            ),
        ),
    )
    rows = runtime.store.iter_record_rows()

    assert result.records_indexed == case.expected_records
    assert runtime.status().records == case.expected_records
    assert len({row.record_id for row in rows}) == case.expected_records
    found = runtime.search_records(_query("ruff", dedupe=False))
    assert [record.text for record in found] == list(case.texts)


def test_db_runtime_sync_can_exit_early_between_sources(
    tmp_path: pathlib.Path,
) -> None:
    """The DB sync control stops before the next source without partial writes."""
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path.write_text("pytest", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    control = agentgrep.SearchControl()
    progress = StopAfterFirstSourceProgress(control)

    result = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run typecheck before committing."),)),
        ),
        control=control,
        progress=progress,
    )

    assert result == SyncResult(
        sources_synced=1,
        records_indexed=1,
        records_removed=0,
        features_deferred=1,
    )
    assert progress.started_total == 2
    assert progress.finished_sources == ["first.jsonl"]
    assert progress.finished_cleanly is False
    assert progress.early_result == result
    assert runtime.status().sources == 1
    # The haystack surface includes record paths, and pytest tmp dirs
    # contain "pytest", so probe with a term absent from every path.
    assert [record.text for record in runtime.search_records(_query("typecheck"))] == []


@pytest.mark.parametrize(
    "case",
    CACHED_SEARCH_CASES,
    ids=[case.test_id for case in CACHED_SEARCH_CASES],
)
def test_run_search_query_respects_cache_mode(
    case: CachedSearchCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search runtime cache mode selects indexed data or the live scanner explicitly."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    db = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = db.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )
    runtime = agentgrep.SearchRuntime(
        source_scan_cache=None,
        db=db,
        cache_mode=case.cache_mode,
    )

    def discover_sources_for_search(
        _home: pathlib.Path,
        _query: agentgrep.SearchQuery,
        _backends: agentgrep.BackendSelection,
        *,
        version_detail: agentgrep.DiscoveryVersionDetail,
    ) -> list[agentgrep.SourceHandle]:
        _ = version_detail
        return [source]

    def iter_source_records(_source: agentgrep.SourceHandle) -> tuple[agentgrep.SearchRecord, ...]:
        return (_record(source, "live scanner ruff result"),)

    monkeypatch.setattr(
        "agentgrep._engine.orchestration.discover_sources_for_search",
        discover_sources_for_search,
    )
    monkeypatch.setattr(
        "agentgrep._engine.scanning.iter_source_records",
        iter_source_records,
    )

    records = agentgrep.run_search_query(tmp_path, _query("ruff"), runtime=runtime)

    assert [record.text for record in records] == list(case.expected_texts)


def test_insight_engine_records_duplicate_variant_edges(
    tmp_path: pathlib.Path,
) -> None:
    """Similarity insights persist deterministic variant edges with confidence."""
    first_path = tmp_path / "one.jsonl"
    second_path = tmp_path / "two.jsonl"
    first_path.write_text("{}", encoding="utf-8")
    second_path.write_text("{}", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing.", session_id="one"),)),
            (second, (_record(second, "run ruff check before committing", session_id="two"),)),
        ),
    )

    result = InsightEngine(runtime.store).run_similarity()
    edges = InsightEngine(runtime.store).list_variant_edges()

    assert result.variant_edges == 1
    assert runtime.status().features == 2
    assert len(edges) == 1
    assert isinstance(edges[0], VariantEdge)
    assert edges[0].variant_type == "exact_duplicate"
    assert edges[0].confidence == pytest.approx(1.0)


def test_insight_engine_counts_and_limits_variant_edges(
    tmp_path: pathlib.Path,
) -> None:
    """Variant edge listing can be paged without materializing every row."""
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("{}", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                source,
                tuple(
                    _record(
                        source,
                        "Run ruff check before committing.",
                        session_id=f"duplicate-{index}",
                    )
                    for index in range(4)
                ),
            ),
        ),
    )
    _ = InsightEngine(runtime.store).run_similarity()
    engine = InsightEngine(runtime.store)

    edges = engine.list_variant_edges(limit=2)

    assert engine.count_variant_edges() == 6
    assert len(edges) == 2


def test_variant_edge_listing_uses_confidence_order_index(
    tmp_path: pathlib.Path,
) -> None:
    """The default variant-edge page can stop at the requested limit."""
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    rows = runtime.store.connection.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT edge_id, run_id, left_record_id, right_record_id,
               variant_type, confidence, explanation
        FROM variant_edges
        ORDER BY confidence DESC, edge_id
        LIMIT 1
        """,
    ).fetchall()
    plan = "\n".join(str(row["detail"]) for row in rows)

    assert "idx_variant_edges_confidence_edge_id" in plan
    assert "USE TEMP B-TREE" not in plan


def test_insight_engine_reports_similarity_activity(
    tmp_path: pathlib.Path,
) -> None:
    """Similarity analysis reports the current backend phase."""
    first_path = tmp_path / "one.jsonl"
    second_path = tmp_path / "two.jsonl"
    first_path.write_text("{}", encoding="utf-8")
    second_path.write_text("{}", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing.", session_id="one"),)),
            (second, (_record(second, "run ruff check before committing", session_id="two"),)),
        ),
    )
    progress = InsightActivityProgress()

    _ = InsightEngine(runtime.store).run_similarity(progress=progress)

    labels = [activity for activity, _detail in progress.activities]
    first_seen_labels = tuple(dict.fromkeys(labels))
    assert first_seen_labels == (
        "checking feature cache",
        "building feature signatures",
        "writing feature cache",
        "loading similarity rows",
        "grouping duplicate prompts",
        "writing similarity artifacts",
    )


def test_feature_refresh_reports_incremental_build_and_write_progress(
    tmp_path: pathlib.Path,
) -> None:
    """Feature refresh reports row-level progress inside long phases."""
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("{}", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                source,
                (
                    _record(source, "Run ruff check before committing.", session_id="one"),
                    _record(source, "Run ty check before committing.", session_id="two"),
                    _record(source, "Build docs before committing.", session_id="three"),
                ),
            ),
        ),
    )
    progress = InsightActivityProgress()

    refreshed = runtime.store.refresh_missing_features(workers=1, progress=progress)

    assert refreshed == 3
    build_details = [
        detail
        for activity, detail in progress.activities
        if activity == "building feature signatures"
    ]
    write_details = [
        detail for activity, detail in progress.activities if activity == "writing feature cache"
    ]
    assert build_details[-1] == "3/3 rows built (100.0%, 1w)"
    assert write_details[-1] == "3/3 rows written (100.0%)"


def test_similarity_analysis_reports_artifact_write_progress(
    tmp_path: pathlib.Path,
) -> None:
    """Similarity analysis reports cluster and edge write counters."""
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("{}", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                source,
                tuple(
                    _record(
                        source,
                        "Run ruff check before committing.",
                        session_id=f"duplicate-{index}",
                    )
                    for index in range(4)
                ),
            ),
        ),
    )
    progress = InsightActivityProgress()

    result = InsightEngine(runtime.store).run_similarity(progress=progress)

    write_details = [
        detail
        for activity, detail in progress.activities
        if activity == "writing similarity artifacts"
    ]
    assert result.variant_edges == 6
    assert write_details[-1] == "1/1 clusters, 6/6 edges"


def _sync_prompts(
    tmp_path: pathlib.Path,
    texts: tuple[str, ...],
) -> DbRuntime:
    """Sync one synthetic prompt per text into a fresh DB runtime."""
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("{}", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                source,
                tuple(
                    _record(source, text, session_id=f"session-{index}")
                    for index, text in enumerate(texts)
                ),
            ),
        ),
    )
    return runtime


def _near_duplicate_edges(runtime: DbRuntime) -> list[tuple[frozenset[str], float]]:
    """Return near-duplicate edges as ``({left_text, right_text}, confidence)``."""
    rows = runtime.store.connection.execute(
        """
        SELECT dl.text AS left_text, dr.text AS right_text, v.confidence AS confidence
        FROM variant_edges v
        JOIN records_search rl ON rl.record_id = v.left_record_id
        JOIN record_details dl ON dl.rowid = rl.rowid
        JOIN records_search rr ON rr.record_id = v.right_record_id
        JOIN record_details dr ON dr.rowid = rr.rowid
        WHERE v.variant_type = 'near_duplicate'
        """,
    ).fetchall()
    return [
        (frozenset({str(row["left_text"]), str(row["right_text"])}), float(row["confidence"]))
        for row in rows
    ]


def _near_duplicate_cluster_member_texts(runtime: DbRuntime) -> list[frozenset[str]]:
    """Return the record texts grouped in each near-duplicate cluster."""
    rows = runtime.store.connection.execute(
        """
        SELECT c.cluster_id AS cluster_id, d.text AS text
        FROM clusters c
        JOIN cluster_members m ON m.cluster_id = c.cluster_id
        JOIN records_search r ON r.record_id = m.record_id
        JOIN record_details d ON d.rowid = r.rowid
        WHERE c.kind = 'near_duplicate_prompt'
        ORDER BY c.cluster_id, d.text
        """,
    ).fetchall()
    by_cluster: dict[str, set[str]] = {}
    for row in rows:
        by_cluster.setdefault(str(row["cluster_id"]), set()).add(str(row["text"]))
    return [frozenset(texts) for texts in by_cluster.values()]


def test_insight_engine_links_near_duplicate_prompts(tmp_path: pathlib.Path) -> None:
    """Slightly different prompts become one near-duplicate edge and cluster."""
    left = "run ruff check across the repo"
    right = "run ruff check across the whole repo"
    runtime = _sync_prompts(tmp_path, (left, right))

    result = InsightEngine(runtime.store).run_similarity()
    edges = _near_duplicate_edges(runtime)
    clusters = _near_duplicate_cluster_member_texts(runtime)

    assert result.variant_edges == 1
    assert result.clusters == 1
    assert len(edges) == 1
    pair, confidence = edges[0]
    assert pair == frozenset({left, right})
    # Confidence is the exact token-set Jaccard: 6 shared / 7 union tokens.
    assert confidence == pytest.approx(6 / 7)
    assert clusters == [frozenset({left, right})]


def test_insight_engine_keeps_exact_and_near_duplicates_separate(
    tmp_path: pathlib.Path,
) -> None:
    """Exact duplicates stay exact-only and are not re-reported as near."""
    exact = "run ruff check across the repo"
    variant = "run ruff check across the whole repo"
    runtime = _sync_prompts(tmp_path, (exact, exact, variant))

    result = InsightEngine(runtime.store).run_similarity()
    exact_edges = runtime.store.connection.execute(
        "SELECT confidence FROM variant_edges WHERE variant_type = 'exact_duplicate'",
    ).fetchall()
    near_edges = _near_duplicate_edges(runtime)

    assert len(exact_edges) == 1
    assert float(exact_edges[0]["confidence"]) == pytest.approx(1.0)
    # The two identical prompts collapse to one representative, so their
    # pair is never emitted as a near-duplicate edge.
    assert frozenset({exact}) not in {pair for pair, _confidence in near_edges}
    # The representative still links to the distinct near-duplicate variant.
    assert near_edges == [(frozenset({exact, variant}), pytest.approx(6 / 7))]
    assert result.variant_edges == 2


def test_insight_engine_ignores_unrelated_prompts_for_near_duplicates(
    tmp_path: pathlib.Path,
) -> None:
    """Prompts below the Jaccard threshold produce no near-duplicate edge."""
    runtime = _sync_prompts(
        tmp_path,
        (
            "run ruff check across the repo",
            "deploy the kubernetes cluster to staging tonight",
        ),
    )

    result = InsightEngine(runtime.store).run_similarity()

    assert result.variant_edges == 0
    assert result.clusters == 0
    assert _near_duplicate_edges(runtime) == []


def test_insight_engine_clusters_transitive_near_duplicates(
    tmp_path: pathlib.Path,
) -> None:
    """A~B and B~C above threshold land in one cluster via union-find."""
    a = "run ruff check across the repo before every commit"
    b = "run ruff check across the repo"
    c = "run ruff check across the repo inside docker containers"
    runtime = _sync_prompts(tmp_path, (a, b, c))

    result = InsightEngine(runtime.store).run_similarity()
    edges = _near_duplicate_edges(runtime)
    clusters = _near_duplicate_cluster_member_texts(runtime)

    assert result.clusters == 1
    assert clusters == [frozenset({a, b, c})]
    # A~C (Jaccard 0.5) is below threshold, so only the two chain edges exist.
    assert {pair for pair, _confidence in edges} == {
        frozenset({a, b}),
        frozenset({b, c}),
    }
    assert frozenset({a, c}) not in {pair for pair, _confidence in edges}
    cluster_confidence = runtime.store.connection.execute(
        "SELECT confidence FROM clusters WHERE kind = 'near_duplicate_prompt'",
    ).fetchone()
    # Cluster confidence is the weakest-link (minimum) pairwise Jaccard.
    assert float(cluster_confidence["confidence"]) == pytest.approx(6 / 9)


def test_near_duplicate_similarity_run_is_deterministic(
    tmp_path: pathlib.Path,
) -> None:
    """Re-running similarity yields identical near-duplicate ids and counts."""
    runtime = _sync_prompts(
        tmp_path,
        (
            "run ruff check across the repo before every commit",
            "run ruff check across the repo",
            "run ruff check across the repo inside docker containers",
        ),
    )

    def snapshot() -> tuple[frozenset[str], frozenset[str]]:
        edge_ids = frozenset(
            str(row["edge_id"])
            for row in runtime.store.connection.execute(
                "SELECT edge_id FROM variant_edges WHERE variant_type = 'near_duplicate'",
            )
        )
        cluster_ids = frozenset(
            str(row["cluster_id"])
            for row in runtime.store.connection.execute(
                "SELECT cluster_id FROM clusters WHERE kind = 'near_duplicate_prompt'",
            )
        )
        return edge_ids, cluster_ids

    first_result = InsightEngine(runtime.store).run_similarity()
    first_snapshot = snapshot()
    second_result = InsightEngine(runtime.store).run_similarity()
    second_snapshot = snapshot()

    assert first_snapshot == second_snapshot
    assert first_result.run_id == second_result.run_id
    assert first_result.variant_edges == second_result.variant_edges == 2
    assert first_result.clusters == second_result.clusters == 1


def test_suggestion_engine_renders_review_only_instruction_suggestion(
    tmp_path: pathlib.Path,
) -> None:
    """Omission suggestions are persisted artifacts and do not edit the target file."""
    source_path = tmp_path / "source.jsonl"
    target_path = tmp_path / "AGENTS.md"
    source_path.write_text("{}", encoding="utf-8")
    target_path.write_text("Run pytest before committing.\n", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                source,
                (
                    _record(
                        source,
                        "Run ruff check before committing.",
                        session_id="instruction-source",
                    ),
                ),
            ),
        ),
    )
    insights = InsightEngine(runtime.store)
    _ = insights.run_omissions(target_path=target_path, target_text=target_path.read_text())

    suggestions = SuggestionEngine(runtime.store).create_from_omissions(target_path=target_path)

    assert len(suggestions) == 1
    assert isinstance(suggestions[0], SuggestionArtifact)
    assert "Run ruff check before committing." in suggestions[0].body
    assert "reload" in suggestions[0].reload_note.casefold()
    assert target_path.read_text(encoding="utf-8") == "Run pytest before committing.\n"


def test_resync_keeps_fts_index_consistent(tmp_path: pathlib.Path) -> None:
    """Re-syncing a source removes old FTS rows with their stored values.

    External-content FTS5 deletes that pass placeholder values leave the
    old tokens mapped to a rowid SQLite later reuses, so the raw index
    keeps answering for text that no longer exists. The result-level
    post-filter masks that from ``search_records``, so this asserts on
    the index itself.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("{}", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    _ = runtime.sync_records(
        ((source, (_record(source, "Run pytest for the focused suite."),)),),
        force=True,
    )

    stale = runtime.store.connection.execute(
        "SELECT rowid FROM record_text_fts WHERE record_text_fts MATCH ?",
        ('"ruff"',),
    ).fetchall()
    assert stale == []
    fresh = runtime.store.connection.execute(
        "SELECT rowid FROM record_text_fts WHERE record_text_fts MATCH ?",
        ('"pytest"',),
    ).fetchall()
    assert len(fresh) == 1
    assert [record.text for record in runtime.search_records(_query("pytest"))] == [
        "Run pytest for the focused suite.",
    ]


class WalFreshnessCase(t.NamedTuple):
    """Named case for WAL-sidecar freshness behavior during DB sync."""

    test_id: str
    source_kind: agentgrep.SourceKind
    touch_wal: bool
    expected_synced: int
    expected_skipped: int


WAL_FRESHNESS_CASES: tuple[WalFreshnessCase, ...] = (
    WalFreshnessCase(
        test_id="sqlite-wal-write-resyncs",
        source_kind="sqlite",
        touch_wal=True,
        expected_synced=1,
        expected_skipped=0,
    ),
    WalFreshnessCase(
        test_id="sqlite-unchanged-skips",
        source_kind="sqlite",
        touch_wal=False,
        expected_synced=0,
        expected_skipped=1,
    ),
    WalFreshnessCase(
        test_id="jsonl-ignores-wal-sidecar",
        source_kind="jsonl",
        touch_wal=True,
        expected_synced=0,
        expected_skipped=1,
    ),
)


@pytest.mark.parametrize(
    "case",
    WAL_FRESHNESS_CASES,
    ids=[case.test_id for case in WAL_FRESHNESS_CASES],
)
def test_sync_freshness_tracks_wal_sidecars(
    case: WalFreshnessCase,
    tmp_path: pathlib.Path,
) -> None:
    """WAL-only writes invalidate freshness for sqlite sources only."""
    source_path = tmp_path / "store.db"
    source_path.write_text("primary", encoding="utf-8")
    source = _source(source_path, source_kind=case.source_kind)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )
    if case.touch_wal:
        (tmp_path / "store.db-wal").write_text("wal frame", encoding="utf-8")

    result = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    assert result.sources_synced == case.expected_synced
    assert result.sources_skipped == case.expected_skipped


def test_cross_file_adapters_always_resync(tmp_path: pathlib.Path) -> None:
    """Adapters that expand sibling files never satisfy the freshness skip."""
    source_path = tmp_path / "history.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path, adapter_id="claude.history_jsonl.v1")
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    result = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    assert result.sources_synced == 1
    assert result.sources_skipped == 0


class SubstringParityCase(t.NamedTuple):
    """Named case for cached-search substring parity with the live engine."""

    test_id: str
    term: str
    expected_texts: tuple[str, ...]


SUBSTRING_PARITY_CASES: tuple[SubstringParityCase, ...] = (
    SubstringParityCase(
        test_id="token-prefix",
        term="ruf",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="mid-token",
        term="uff",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="token-and-substring",
        term="ruff",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="uppercase-query",
        term="RUFF",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="short-term-scan",
        term="ty",
        expected_texts=("run ty check",),
    ),
    SubstringParityCase(
        test_id="non-ascii-term-scan",
        term="café",
        expected_texts=("visit the café notes",),
    ),
    SubstringParityCase(
        test_id="genuine-zero",
        term="zsh-plugin",
        expected_texts=(),
    ),
)


@pytest.mark.parametrize(
    "case",
    SUBSTRING_PARITY_CASES,
    ids=[case.test_id for case in SUBSTRING_PARITY_CASES],
)
def test_cached_search_matches_live_substring_semantics(
    case: SubstringParityCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached term search returns exactly what live substring matching would."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    texts = (
        "Run ruff check.",
        "the scruffy dog",
        "run ty check",
        "visit the café notes",
        "unrelated pytest line",
    )
    _ = runtime.sync_records(
        (
            (
                source,
                tuple(
                    _record(source, text, session_id=f"session-{index}")
                    for index, text in enumerate(texts)
                ),
            ),
        ),
    )

    found = sorted(record.text for record in runtime.search_records(_query(case.term)))

    assert found == sorted(case.expected_texts)


def test_schema_version_mismatch_rebuilds_cache(tmp_path: pathlib.Path) -> None:
    """A cache written by a different schema version is rebuilt on open."""
    db_path = tmp_path / "agentgrep.sqlite"
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(db_path)
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
        features_mode="inline",
    )
    _ = InsightEngine(runtime.store).run_similarity()
    fresh_tables = {
        str(row["name"])
        for row in runtime.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    with runtime.store.connection:
        _ = runtime.store.connection.execute(
            "UPDATE meta SET value = '999' WHERE key = 'schema_version'",
        )
    runtime.store.close()

    reopened = DbRuntime.open(db_path)

    status = reopened.status()
    assert status.records == 0
    assert status.sources == 0
    assert status.features == 0
    assert status.variant_edges == 0
    assert status.omission_findings == 0
    assert status.suggestions == 0
    rebuilt_tables = {
        str(row["name"])
        for row in reopened.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert rebuilt_tables == fresh_tables
    # Tables without a cascade path to records keep rows if the drop
    # list misses them; count them directly since status() does not.
    for table in ("insight_runs", "clusters"):
        row = reopened.store.connection.execute(
            f"SELECT COUNT(*) AS count FROM {table}",
        ).fetchone()
        assert row is not None
        assert int(row["count"]) == 0, table


class CacheDedupeCase(t.NamedTuple):
    """Named case for per-session dedup on the cached search path."""

    test_id: str
    dedupe: bool
    expected_count: int


CACHE_DEDUPE_CASES: tuple[CacheDedupeCase, ...] = (
    CacheDedupeCase(test_id="dedupe-collapses-session", dedupe=True, expected_count=1),
    CacheDedupeCase(test_id="no-dedupe-keeps-both", dedupe=False, expected_count=2),
)


@pytest.mark.parametrize(
    "case",
    CACHE_DEDUPE_CASES,
    ids=[case.test_id for case in CACHE_DEDUPE_CASES],
)
def test_cached_search_applies_per_session_dedup(
    case: CacheDedupeCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached records honor the per-session dedup decision like live scans."""
    first_path = tmp_path / "one.jsonl"
    second_path = tmp_path / "two.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run ruff check before committing."),)),
        ),
    )
    search_runtime = agentgrep.SearchRuntime(cache_mode="require", db=runtime)

    handled, records = agentgrep._db_search_result(
        _query("ruff", dedupe=case.dedupe),
        search_runtime,
    )

    assert handled is True
    assert len(records) == case.expected_count


class CacheLimitDedupeCase(t.NamedTuple):
    """Named case for limit interaction with per-session dedup."""

    test_id: str
    dedupe: bool
    limit: int
    expected_texts: tuple[str, ...]


CACHE_LIMIT_DEDUPE_CASES: tuple[CacheLimitDedupeCase, ...] = (
    CacheLimitDedupeCase(
        test_id="limit-counts-unique-records",
        dedupe=True,
        limit=2,
        expected_texts=(
            "Run ruff check before committing.",
            "Run pytest before committing.",
        ),
    ),
    CacheLimitDedupeCase(
        test_id="no-dedupe-limit-keeps-duplicates",
        dedupe=False,
        limit=2,
        expected_texts=(
            "Run ruff check before committing.",
            "Run ruff check before committing.",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    CACHE_LIMIT_DEDUPE_CASES,
    ids=[case.test_id for case in CACHE_LIMIT_DEDUPE_CASES],
)
def test_cached_search_limit_counts_unique_records(
    case: CacheLimitDedupeCase,
    tmp_path: pathlib.Path,
) -> None:
    """A result cap counts unique records, like the live driver."""
    first_path = tmp_path / "one.jsonl"
    second_path = tmp_path / "two.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                first,
                (
                    _record(first, "Run ruff check before committing."),
                    _record(
                        first,
                        "Run pytest before committing.",
                        timestamp="2026-06-04T12:00:00Z",
                        session_id="session-b",
                    ),
                ),
            ),
            (second, (_record(second, "Run ruff check before committing."),)),
        ),
    )
    query = dataclasses.replace(
        _query("committing", dedupe=case.dedupe),
        limit=case.limit,
    )

    found = [record.text for record in runtime.search_records(query)]

    assert found == list(case.expected_texts)


class HaystackParityCase(t.NamedTuple):
    """Named case for cached haystack-field parity with the live engine."""

    test_id: str
    term: str
    match_surface: agentgrep.SearchMatchSurface
    expected_texts: tuple[str, ...]


HAYSTACK_PARITY_CASES: tuple[HaystackParityCase, ...] = (
    HaystackParityCase(
        test_id="model-only-term",
        term="opus",
        match_surface="haystack",
        expected_texts=("model record",),
    ),
    HaystackParityCase(
        test_id="role-only-term",
        term="assistant",
        match_surface="haystack",
        expected_texts=("role record",),
    ),
    HaystackParityCase(
        test_id="path-only-term",
        term="projalpha",
        match_surface="haystack",
        expected_texts=("path record",),
    ),
    HaystackParityCase(
        test_id="casefold-expanding-text",
        term="ass",
        match_surface="haystack",
        # "ass" matches the folded "strasse" expansion AND the role
        # record's "assistant" haystack — both are live-parity hits.
        expected_texts=("Besuch der Straße notieren", "role record"),
    ),
    HaystackParityCase(
        test_id="text-surface-excludes-title-match",
        term="release",
        match_surface="text",
        expected_texts=("release steps live here",),
    ),
)


@pytest.mark.parametrize(
    "case",
    HAYSTACK_PARITY_CASES,
    ids=[case.test_id for case in HAYSTACK_PARITY_CASES],
)
def test_cached_search_covers_live_haystack_fields(
    case: HaystackParityCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached candidates cover every field the live matcher searches."""
    plain_path = tmp_path / "plain" / "session.jsonl"
    proj_path = tmp_path / "projalpha" / "session.jsonl"
    plain_path.parent.mkdir()
    proj_path.parent.mkdir()
    plain_path.write_text("{}", encoding="utf-8")
    proj_path.write_text("{}", encoding="utf-8")
    plain = _source(plain_path)
    proj = _source(proj_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                plain,
                (
                    _record(plain, "model record", model="claude-opus-4", session_id="s1"),
                    _record(plain, "role record", role="assistant", session_id="s2"),
                    _record(plain, "Besuch der Straße notieren", session_id="s3"),
                    _record(plain, "title only", title="release notes", session_id="s4"),
                    _record(plain, "release steps live here", session_id="s5"),
                ),
            ),
            (proj, (_record(proj, "path record", session_id="s6"),)),
        ),
    )
    query = dataclasses.replace(_query(case.term), match_surface=case.match_surface)

    found = sorted(record.text for record in runtime.search_records(query))

    assert found == sorted(case.expected_texts)


class CacheDecisionSpanCase(t.NamedTuple):
    """Named case for the search.cache.decision telemetry span."""

    test_id: str
    cache_mode: agentgrep.CacheMode
    term: str
    regex: bool
    expected_spans: int
    expected_handled: bool | None
    expected_reason: str | None
    with_coverage: bool = True


CACHE_DECISION_SPAN_CASES: tuple[CacheDecisionSpanCase, ...] = (
    CacheDecisionSpanCase(
        test_id="served-from-cache",
        cache_mode="require",
        term="ruff",
        regex=False,
        expected_spans=1,
        expected_handled=True,
        expected_reason=None,
    ),
    CacheDecisionSpanCase(
        test_id="auto-empty-falls-back",
        cache_mode="auto",
        term="zsh-plugin-nowhere",
        regex=False,
        expected_spans=1,
        expected_handled=False,
        expected_reason="empty",
    ),
    CacheDecisionSpanCase(
        test_id="auto-unsupported-falls-back",
        cache_mode="auto",
        term="ruff.*check",
        regex=True,
        expected_spans=1,
        expected_handled=False,
        expected_reason="unsupported",
    ),
    CacheDecisionSpanCase(
        test_id="auto-partial-coverage-falls-back",
        cache_mode="auto",
        term="ruff",
        regex=False,
        expected_spans=1,
        expected_handled=False,
        expected_reason="partial-coverage",
        with_coverage=False,
    ),
    CacheDecisionSpanCase(
        test_id="off-emits-nothing",
        cache_mode="off",
        term="ruff",
        regex=False,
        expected_spans=0,
        expected_handled=None,
        expected_reason=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    CACHE_DECISION_SPAN_CASES,
    ids=[case.test_id for case in CACHE_DECISION_SPAN_CASES],
)
def test_cache_decision_span_reports_aggregate_outcome(
    case: CacheDecisionSpanCase,
    tmp_path: pathlib.Path,
) -> None:
    """One privacy-safe span per consulted query, never per record."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
        coverage=(
            SyncCoverage(agents=("codex",), scope="all", complete=True)
            if case.with_coverage
            else None
        ),
    )
    search_runtime = agentgrep.SearchRuntime(
        db=None if case.cache_mode == "off" else runtime,
        cache_mode=case.cache_mode,
    )
    query = dataclasses.replace(_query(case.term), regex=case.regex)
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = agentgrep._db_search_result(query, search_runtime)

    samples = [
        sample for sample in profiler.snapshot().samples if sample.name == "search.cache.decision"
    ]
    assert len(samples) == case.expected_spans
    if case.expected_spans:
        attributes = samples[0].attributes
        assert attributes["agentgrep_cache_mode"] == case.cache_mode
        assert attributes["agentgrep_cache_handled"] == case.expected_handled
        if case.expected_reason is None:
            assert "agentgrep_cache_fallback_reason" not in attributes
        else:
            assert attributes["agentgrep_cache_fallback_reason"] == case.expected_reason


class ScopeParityCase(t.NamedTuple):
    """Named case for cached scope filters mirroring live semantics."""

    test_id: str
    scope: agentgrep.SearchScope
    agents: tuple[agentgrep.AgentName, ...]
    expected_texts: tuple[str, ...]


SCOPE_PARITY_CASES: tuple[ScopeParityCase, ...] = (
    ScopeParityCase(
        test_id="conversations-include-user-turns",
        scope="conversations",
        agents=("codex",),
        expected_texts=(
            "alpaca assistant turn",
            "alpaca chat user turn",
        ),
    ),
    ScopeParityCase(
        test_id="prompts-use-prompt-store-for-covered-agent",
        scope="prompts",
        agents=("codex",),
        expected_texts=("alpaca prompt store entry",),
    ),
    ScopeParityCase(
        test_id="prompts-fall-back-to-chat-without-prompt-store",
        scope="prompts",
        agents=("pi",),
        expected_texts=("alpaca pi user turn",),
    ),
    ScopeParityCase(
        test_id="all-scope-returns-everything",
        scope="all",
        agents=("codex", "pi"),
        expected_texts=(
            "alpaca assistant turn",
            "alpaca chat user turn",
            "alpaca pi user turn",
            "alpaca prompt store entry",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    SCOPE_PARITY_CASES,
    ids=[case.test_id for case in SCOPE_PARITY_CASES],
)
def test_cached_scope_filters_mirror_live_semantics(
    case: ScopeParityCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached scope filtering matches the live planner and record filters.

    Live conversations scope admits records by store role — chat
    adapters emit user turns as kind='prompt' — and live prompts scope
    serves an agent from its dedicated prompt-history store when one
    exists, falling back to chat-store user turns only when it does
    not.
    """
    chat_path = tmp_path / "codex-session.jsonl"
    chat_path.write_text("chat", encoding="utf-8")
    chat_source = _source(chat_path)
    prompt_path = tmp_path / "codex-history.jsonl"
    prompt_path.write_text("history", encoding="utf-8")
    prompt_source = _source(
        prompt_path,
        adapter_id="codex.history_jsonl.v1",
        store="codex.history",
    )
    pi_path = tmp_path / "pi-session.jsonl"
    pi_path.write_text("pi", encoding="utf-8")
    pi_source = _source(
        pi_path,
        adapter_id="pi.sessions_jsonl.v1",
        agent="pi",
        store="pi.sessions",
    )
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                chat_source,
                (
                    _record(chat_source, "alpaca chat user turn", session_id="chat-a"),
                    _record(
                        chat_source,
                        "alpaca assistant turn",
                        kind="history",
                        session_id="chat-a",
                    ),
                ),
            ),
            (
                prompt_source,
                (_record(prompt_source, "alpaca prompt store entry", session_id="hist-a"),),
            ),
            (
                pi_source,
                (_record(pi_source, "alpaca pi user turn", session_id="pi-a"),),
            ),
        ),
    )

    found = runtime.search_records(
        _query("alpaca", scope=case.scope, agents=case.agents),
    )
    runtime.close()

    assert sorted(record.text for record in found) == sorted(case.expected_texts)


class CoverageGateCase(t.NamedTuple):
    """Named case for the auto-mode coverage gate."""

    test_id: str
    sync_coverage: SyncCoverage | None
    cache_mode: agentgrep.CacheMode
    query_agents: tuple[agentgrep.AgentName, ...]
    query_scope: agentgrep.SearchScope
    expected_handled: bool


COVERAGE_GATE_CASES: tuple[CoverageGateCase, ...] = (
    CoverageGateCase(
        test_id="full-sync-serves-auto",
        sync_coverage=SyncCoverage(agents=("codex",), scope="all", complete=True),
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=True,
    ),
    CoverageGateCase(
        test_id="agent-subset-falls-back",
        sync_coverage=SyncCoverage(agents=("codex",), scope="all", complete=True),
        cache_mode="auto",
        query_agents=("codex", "claude"),
        query_scope="prompts",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="scoped-sync-covers-only-its-scope",
        sync_coverage=SyncCoverage(agents=("codex",), scope="prompts", complete=True),
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="conversations",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="capped-sync-claims-nothing",
        sync_coverage=SyncCoverage(agents=("codex",), scope="all", complete=False),
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="no-coverage-falls-back",
        sync_coverage=None,
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="require-serves-without-coverage",
        sync_coverage=None,
        cache_mode="require",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    COVERAGE_GATE_CASES,
    ids=[case.test_id for case in COVERAGE_GATE_CASES],
)
def test_auto_cache_hits_require_sync_coverage(
    case: CoverageGateCase,
    tmp_path: pathlib.Path,
) -> None:
    """Auto mode serves cache hits only when coverage spans the query.

    Coverage means the agent/scope combination completed a sync;
    require mode keeps serving regardless because the caller demanded
    the cache.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
        coverage=case.sync_coverage,
    )
    search_runtime = agentgrep.SearchRuntime(db=runtime, cache_mode=case.cache_mode)

    handled, records = agentgrep._db_search_result(
        _query("ruff", scope=case.query_scope, agents=case.query_agents),
        search_runtime,
    )
    runtime.close()

    assert handled is case.expected_handled
    assert bool(records) is case.expected_handled


def test_interrupted_sync_records_no_coverage(tmp_path: pathlib.Path) -> None:
    """An early-exited sync loop never claims coverage."""
    first_path = tmp_path / "first.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path = tmp_path / "second.jsonl"
    second_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    control = agentgrep.SearchControl()
    progress = StopAfterFirstSourceProgress(control)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    _ = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run ruff again."),)),
        ),
        control=control,
        progress=t.cast("DbSyncProgress", progress),
        coverage=SyncCoverage(agents=("codex",), scope="all", complete=True),
    )

    assert runtime.store.coverage() is None
    runtime.close()


def test_merge_coverage_keeps_other_agents(tmp_path: pathlib.Path) -> None:
    """A narrowed re-sync merges into coverage instead of replacing it."""
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    runtime.store.merge_coverage(
        SyncCoverage(agents=("codex", "claude"), scope="all", complete=True),
    )
    runtime.store.merge_coverage(
        SyncCoverage(agents=("codex",), scope="prompts", complete=True),
    )

    coverage = runtime.store.coverage()
    runtime.close()

    assert coverage == {
        "claude": ("all",),
        "codex": ("all", "prompts"),
    }


class PruneCase(t.NamedTuple):
    """Named case for vanished-source pruning during sync."""

    test_id: str
    prune_missing: bool
    expected_pruned: int
    expected_texts: tuple[str, ...]


PRUNE_CASES: tuple[PruneCase, ...] = (
    PruneCase(
        test_id="full-sync-prunes-vanished-source",
        prune_missing=True,
        expected_pruned=1,
        expected_texts=("Run ruff check before committing.",),
    ),
    PruneCase(
        test_id="narrowed-sync-keeps-vanished-source",
        prune_missing=False,
        expected_pruned=0,
        expected_texts=(
            "Run ruff check before committing.",
            "Run ruff from the vanished file.",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    PRUNE_CASES,
    ids=[case.test_id for case in PRUNE_CASES],
)
def test_resync_prunes_sources_missing_from_discovery(
    case: PruneCase,
    tmp_path: pathlib.Path,
) -> None:
    """A pruning resync drops ledger rows for vanished sources.

    A previously indexed file that is deleted or rotated never appears
    in discovery again, so without pruning its records answer cached
    searches forever. Freshness-skipped sources stay: they are part of
    the resync's batch set.
    """
    kept_path = tmp_path / "kept.jsonl"
    kept_path.write_text("ruff", encoding="utf-8")
    vanished_path = tmp_path / "vanished.jsonl"
    vanished_path.write_text("ruff", encoding="utf-8")
    kept = _source(kept_path)
    vanished = _source(vanished_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (kept, (_record(kept, "Run ruff check before committing."),)),
            (vanished, (_record(vanished, "Run ruff from the vanished file."),)),
        ),
    )
    vanished_path.unlink()

    result = runtime.sync_records(
        ((kept, (_record(kept, "unread"),)),),
        prune_missing=case.prune_missing,
    )

    assert result.sources_pruned == case.expected_pruned
    assert result.sources_skipped == 1
    found = runtime.search_records(_query("ruff"))
    assert sorted(record.text for record in found) == sorted(case.expected_texts)
    assert runtime.status().sources == 2 - case.expected_pruned
    runtime.close()


def test_early_exited_sync_never_prunes(tmp_path: pathlib.Path) -> None:
    """An early-exited loop must not prune sources it never visited."""
    first_path = tmp_path / "first.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    vanished_path = tmp_path / "vanished.jsonl"
    vanished_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    vanished = _source(vanished_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((vanished, (_record(vanished, "Run ruff from the vanished file."),)),),
    )
    vanished_path.unlink()
    control = agentgrep.SearchControl()
    progress = StopAfterFirstSourceProgress(control)

    second_path = tmp_path / "second.jsonl"
    second_path.write_text("ruff", encoding="utf-8")
    second = _source(second_path)
    result = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run ruff again."),)),
        ),
        control=control,
        progress=t.cast("DbSyncProgress", progress),
        prune_missing=True,
    )

    assert result.sources_pruned == 0
    assert runtime.status().sources == 2
    runtime.close()


def test_cached_search_with_empty_agent_selection_returns_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """An empty agent selection matches live discovery: zero records.

    Guards the public library surface — CLI and MCP always normalize
    to a non-empty selection, but ``SearchQuery(agents=())`` is a
    structurally valid input and ``IN ()`` is nonstandard SQL.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    found = runtime.search_records(_query("ruff", agents=()))
    runtime.close()

    assert found == []


class SqlTelemetrySearchCase(t.NamedTuple):
    """Named case for SQL statement samples emitted by cached search."""

    test_id: str
    term: str
    expected_statement: str


SQL_TELEMETRY_SEARCH_CASES: tuple[SqlTelemetrySearchCase, ...] = (
    SqlTelemetrySearchCase(
        test_id="fts-path-statement",
        term="alpaca",
        expected_statement="records.probe_fts",
    ),
    SqlTelemetrySearchCase(
        test_id="scan-path-statement",
        term="zq",
        expected_statement="records.probe_scan",
    ),
)


def _sql_samples(
    profiler_samples: t.Any,
) -> dict[str, t.Any]:
    """Index db.sql.statement samples by statement name."""
    return {
        str(sample.attributes["agentgrep_sql_statement"]): sample
        for sample in profiler_samples
        if sample.name == "db.sql.statement"
    }


@pytest.mark.parametrize(
    "case",
    SQL_TELEMETRY_SEARCH_CASES,
    ids=[case.test_id for case in SQL_TELEMETRY_SEARCH_CASES],
)
def test_search_emits_aggregated_sql_statement_samples(
    case: SqlTelemetrySearchCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached search emits one db.sql.statement sample per statement shape."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "alpaca zq sentinel-free text"),)),),
    )
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = runtime.search_records(_query(case.term))
    runtime.close()

    samples = _sql_samples(profiler.snapshot().samples)
    assert case.expected_statement in samples
    statement = samples[case.expected_statement]
    assert statement.attributes["agentgrep_sql_count"] == 1
    assert statement.attributes["agentgrep_sql_rows"] == 1


def test_sync_aggregates_sql_statement_samples(tmp_path: pathlib.Path) -> None:
    """A multi-record sync emits one aggregated sample per statement shape.

    Per-record statements (records.insert, fts.insert) must show up as
    one sample with a count — the n+1 signal — never as one sample per
    execution.
    """
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    records = tuple(
        _record(source, f"alpaca record {index}", session_id=f"session-{index}")
        for index in range(3)
    )
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = runtime.sync_records(((source, records),))
    runtime.close()

    all_sql = [
        sample for sample in profiler.snapshot().samples if sample.name == "db.sql.statement"
    ]
    samples = _sql_samples(all_sql)
    assert samples["records_search.insert"].attributes["agentgrep_sql_count"] == 3
    assert samples["record_details.insert"].attributes["agentgrep_sql_count"] == 3
    assert samples["fts.insert"].attributes["agentgrep_sql_count"] == 3
    statement_names = [str(sample.attributes["agentgrep_sql_statement"]) for sample in all_sql]
    assert len(statement_names) == len(set(statement_names))


def test_sql_telemetry_is_silent_without_active_profiler(
    tmp_path: pathlib.Path,
) -> None:
    """Without an active profiler, ops run clean and leave no stats behind."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "alpaca quiet"),)),),
    )
    _ = runtime.search_records(_query("alpaca"))
    assert runtime.store._sql_stats == {}

    late_profiler = EngineProfiler()
    with use_engine_profiler(late_profiler):
        pass
    runtime.close()

    assert late_profiler.snapshot().samples == ()


def test_sql_telemetry_never_captures_bound_parameters(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Statement telemetry carries placeholders only — never search terms."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    sentinel = "zanzibar7sentinel"
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, f"prompt mentioning {sentinel} once"),)),),
    )
    profiler = EngineProfiler()

    with (
        caplog.at_level(logging.DEBUG, logger="agentgrep.db"),
        use_engine_profiler(profiler),
    ):
        found = runtime.search_records(_query(sentinel))
    runtime.close()

    assert len(found) == 1
    sql_records = [
        record for record in caplog.records if hasattr(record, "agentgrep_sql_statement")
    ]
    assert sql_records
    for record in sql_records:
        assert sentinel not in record.getMessage()
        assert sentinel not in str(record.agentgrep_sql_statement)
    for sample in profiler.snapshot().samples:
        for value in sample.attributes.values():
            assert sentinel not in str(value)


class SqlPlanCaptureCase(t.NamedTuple):
    """Named case for opt-in EXPLAIN QUERY PLAN capture."""

    test_id: str
    explain_env: str | None
    term: str
    expected_statement: str
    expects_plan: bool
    plan_mentions: str | None


SQL_PLAN_CAPTURE_CASES: tuple[SqlPlanCaptureCase, ...] = (
    SqlPlanCaptureCase(
        test_id="fts-plan-captured",
        explain_env="1",
        term="alpaca",
        expected_statement="records.probe_fts",
        expects_plan=True,
        plan_mentions="VIRTUAL TABLE",
    ),
    SqlPlanCaptureCase(
        test_id="scan-plan-captured",
        explain_env="1",
        term="zq",
        expected_statement="records.probe_scan",
        expects_plan=True,
        plan_mentions="SCAN",
    ),
    SqlPlanCaptureCase(
        test_id="off-by-default",
        explain_env=None,
        term="alpaca",
        expected_statement="records.probe_fts",
        expects_plan=False,
        plan_mentions=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    SQL_PLAN_CAPTURE_CASES,
    ids=[case.test_id for case in SQL_PLAN_CAPTURE_CASES],
)
def test_sql_plan_capture_honors_explain_lever(
    case: SqlPlanCaptureCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENTGREP_SQL_EXPLAIN attaches query plans to statement samples."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    if case.explain_env is None:
        monkeypatch.delenv("AGENTGREP_SQL_EXPLAIN", raising=False)
    else:
        monkeypatch.setenv("AGENTGREP_SQL_EXPLAIN", case.explain_env)
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "alpaca zq plan capture text"),)),),
    )
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = runtime.search_records(_query(case.term))
    runtime.close()

    samples = _sql_samples(profiler.snapshot().samples)
    statement = samples[case.expected_statement]
    if case.expects_plan:
        plan = str(statement.attributes["agentgrep_sql_plan"])
        assert case.plan_mentions is not None
        assert case.plan_mentions in plan
    else:
        assert "agentgrep_sql_plan" not in statement.attributes


def test_sql_plan_captured_once_per_statement_shape(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated executions of one shape run EXPLAIN only once."""
    monkeypatch.setenv("AGENTGREP_SQL_EXPLAIN", "1")
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    store = runtime.store

    first = store._query("meta.get", "SELECT value FROM meta WHERE key = ?", ("a",))
    stats = store._sql_stats["meta.get"]
    plan_after_first = stats.plan
    second = store._query("meta.get", "SELECT value FROM meta WHERE key = ?", ("b",))
    runtime.close()

    assert first == [] and second == []
    assert plan_after_first is not None
    assert stats.plan is plan_after_first
    assert stats.count == 2


def test_split_read_model_round_trips_every_field(tmp_path: pathlib.Path) -> None:
    """Hydrated records are field-identical to what was synced.

    The search/details split must not lose title, role, model, or
    metadata on the way through the two tables.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text="alpaca full fidelity",
        title="a title",
        role="user",
        timestamp="2026-06-07T01:00:00Z",
        model="gpt-test",
        session_id="session-a",
        conversation_id="conv-a",
        metadata={"k": "v"},
    )
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(((source, (record,)),))

    found = runtime.search_records(_query("alpaca"))
    runtime.close()

    assert len(found) == 1
    got = found[0]
    assert (got.text, got.title, got.role, got.model) == (
        record.text,
        record.title,
        record.role,
        record.model,
    )
    assert (got.timestamp, got.session_id, got.conversation_id) == (
        record.timestamp,
        record.session_id,
        record.conversation_id,
    )
    assert got.metadata == {"k": "v"}


def _adversarial_corpus(
    source: agentgrep.SourceHandle,
) -> tuple[agentgrep.SearchRecord, ...]:
    """Build a corpus exercising probe edges.

    Shape: 24 NULL-timestamp records on one shared path (equal sort
    tuples, rowid tiebreak) in dedup groups of 3; 24 timestamped records
    with duplicate texts straddling group boundaries; 12 newest records
    where the term appears ONLY in the title (haystack hit, text-surface
    oracle reject); 6 case-variant records.
    """
    null_ts = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=f"alpaca null-ts group-{index // 3}",
            timestamp=None,
            session_id="session-null",
        )
        for index in range(24)
    ]
    timed = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=f"alpaca timed group-{index // 4}",
            timestamp=f"2026-06-0{1 + index % 5}T0{index % 10}:00:00Z",
            session_id="session-timed",
        )
        for index in range(24)
    ]
    title_only = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=f"plain body {index}",
            title=f"alpaca only in title {index}",
            timestamp=f"2026-06-07T1{index % 10}:00:00Z",
            session_id=f"session-title-{index}",
        )
        for index in range(12)
    ]
    case_variants = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=f"ALPACA upper case {index}" if index % 2 else f"alpaca lower {index}",
            timestamp=f"2026-06-06T0{index}:00:00Z",
            session_id=f"session-case-{index}",
        )
        for index in range(6)
    ]
    return (*null_ts, *timed, *title_only, *case_variants)


class ProbeParityCase(t.NamedTuple):
    """Named case for keyset-probe parity against the unlimited reference."""

    test_id: str
    term: str
    limit: int
    window_floor: int
    dedupe: bool = True
    case_sensitive: bool = False
    match_surface: agentgrep.SearchMatchSurface = "haystack"
    expect_multiple_pages: bool = False


PROBE_PARITY_CASES: tuple[ProbeParityCase, ...] = (
    ProbeParityCase(
        test_id="nulls-and-ties-paginate",
        term="null-ts",
        limit=5,
        window_floor=4,
        expect_multiple_pages=True,
    ),
    ProbeParityCase(
        test_id="dedup-groups-straddle-pages",
        term="timed",
        limit=5,
        window_floor=4,
        expect_multiple_pages=True,
    ),
    ProbeParityCase(
        test_id="dedupe-false-counts-duplicates",
        term="timed",
        limit=10,
        window_floor=4,
        dedupe=False,
        expect_multiple_pages=True,
    ),
    ProbeParityCase(
        test_id="case-sensitive-wades-past-rejections",
        term="ALPACA",
        limit=2,
        window_floor=4,
        case_sensitive=True,
        expect_multiple_pages=True,
    ),
    ProbeParityCase(
        test_id="text-surface-oracle-refills",
        term="alpaca",
        limit=6,
        window_floor=4,
        match_surface="text",
        expect_multiple_pages=True,
    ),
    ProbeParityCase(
        test_id="default-window-seals-one-page",
        term="alpaca",
        limit=5,
        window_floor=200,
    ),
    ProbeParityCase(
        test_id="limit-beyond-corpus-exhausts",
        term="alpaca",
        limit=500,
        window_floor=8,
        expect_multiple_pages=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    PROBE_PARITY_CASES,
    ids=[case.test_id for case in PROBE_PARITY_CASES],
)
def test_keyset_probe_matches_unlimited_reference(
    case: ProbeParityCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe results equal the unlimited reference sliced to the limit.

    The reference path (limit=None) is itself pinned by the existing
    substring/scope/dedup parity suites; the probe must reproduce its
    prefix under pagination, dedup-group splits, NULL-timestamp tie
    runs, oracle rejections, and exhaustion.
    """
    import agentgrep.db as agentgrep_db
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(((source, _adversarial_corpus(source)),))

    def build_query(limit: int | None) -> agentgrep.SearchQuery:
        return agentgrep.SearchQuery(
            terms=(case.term,),
            scope="prompts",
            any_term=False,
            regex=False,
            case_sensitive=case.case_sensitive,
            agents=("codex",),
            limit=limit,
            dedupe=case.dedupe,
            match_surface=case.match_surface,
        )

    reference = runtime.search_records(build_query(None))
    if case.window_floor < agentgrep_db._PROBE_WINDOW_FLOOR:
        monkeypatch.setattr(
            agentgrep_db.DbStore,
            "_initial_probe_window",
            staticmethod(lambda limit: case.window_floor),
        )
    profiler = EngineProfiler()
    with use_engine_profiler(profiler):
        probed = runtime.search_records(build_query(case.limit))
    runtime.close()

    expected = reference[: case.limit]
    assert [record.text for record in probed] == [record.text for record in expected]
    probe_samples = [
        sample
        for sample in profiler.snapshot().samples
        if sample.name == "db.sql.statement"
        and str(sample.attributes["agentgrep_sql_statement"]).startswith("records.probe")
    ]
    assert len(probe_samples) == 1
    pages = int(t.cast("int", probe_samples[0].attributes["agentgrep_sql_count"]))
    if case.expect_multiple_pages:
        assert pages > 1
    else:
        assert pages == 1


def test_connections_memory_map_the_cache(tmp_path: pathlib.Path) -> None:
    """Both open paths memory-map the database file.

    Measured lever: mmap shares the OS page cache across the
    per-consult connections the MCP server opens, unlike a
    per-connection cache_size that re-warms on every consult.
    """
    db_path = tmp_path / "agentgrep.sqlite"
    runtime = DbRuntime.open(db_path)
    rw_mmap = runtime.store.connection.execute("PRAGMA mmap_size").fetchone()[0]
    runtime.close()

    readonly = DbRuntime.open_readonly(db_path)
    ro_mmap = readonly.store.connection.execute("PRAGMA mmap_size").fetchone()[0]
    readonly.close()

    assert rw_mmap > 0
    assert ro_mmap > 0


def test_new_caches_use_8k_pages(tmp_path: pathlib.Path) -> None:
    """Caches created by agentgrep use 8 KiB pages.

    Measured lever: 8 KiB pages shorten overflow chains for the ~3 KiB
    detail rows, halving hydration time. Existing caches keep their
    page size until the next rebuild.
    """
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    page_size = runtime.store.connection.execute("PRAGMA page_size").fetchone()[0]
    runtime.close()

    assert page_size == 8192
