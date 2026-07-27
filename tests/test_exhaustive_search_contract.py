"""Engine contracts for exhaustive search effort."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep.cli.render as cli_render
from agentgrep import (
    SearchArgs,
    discover_sources_for_search,
    parse_args,
    source_matches_scope,
)
from agentgrep._engine.planning import (
    build_logical_search_plan,
    build_physical_search_plan,
)
from agentgrep.discovery import descriptor_admits_store_roles
from agentgrep.records import (
    CONVERSATION_STORE_ROLES,
    PROMPT_HISTORY_STORE_ROLES,
    AgentName,
    BackendSelection,
    SearchEffort,
    SearchQuery,
    SearchRecord,
    SearchScope,
    SourceHandle,
)
from agentgrep.results import RunCoverage, SearchResult, build_search_summary
from agentgrep.store_catalog import CATALOG
from agentgrep.stores import StoreRole


def _source(
    name: str,
    *,
    search_root: pathlib.Path | None = None,
) -> SourceHandle:
    """Build one synthetic transcript source."""
    return SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path(name),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=search_root,
        mtime_ns=0,
    )


def _app_state_source(
    agent: AgentName,
    store: str,
    adapter_id: str,
) -> SourceHandle:
    """Build one synthetic conversation-bearing app-state source."""
    return SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(f"{agent}.sqlite"),
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=0,
    )


def _record(source: SourceHandle, text: str, timestamp: str) -> SearchRecord:
    """Build one matching prompt with an explicit global-order timestamp."""
    return SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        timestamp=timestamp,
        session_id=source.path.stem,
    )


def _query(
    *,
    scope: SearchScope = "prompts",
    limit: int | None = None,
    effort: SearchEffort | None = None,
) -> SearchQuery:
    """Build one single-agent request over the flags these cases share."""
    return SearchQuery(
        terms=("match",),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        effort=effort,
    )


@pytest.mark.parametrize("scope", ["conversations", "all"])
def test_explicit_prompt_effort_rejects_broad_scope(scope: SearchScope) -> None:
    """Reject a query whose requested records require forbidden stores."""
    query = _query(scope=scope, effort="prompt")

    with pytest.raises(ValueError, match="prompt effort requires prompt scope"):
        build_logical_search_plan(query)


def test_engine_rejects_invalid_runtime_effort() -> None:
    """Fail closed when a direct Python caller supplies an invalid effort."""
    query = _query(effort=t.cast("SearchEffort", "invalid"))

    with pytest.raises(
        ValueError,
        match="effort must be 'prompt', 'targeted', or 'exhaustive'",
    ):
        build_logical_search_plan(query)


def test_public_source_scope_rejects_invalid_runtime_effort() -> None:
    """Fail closed when direct source filtering receives an invalid effort."""
    transcript = _source("transcript.jsonl")

    with pytest.raises(
        ValueError,
        match="effort must be 'prompt', 'targeted', or 'exhaustive'",
    ):
        source_matches_scope(
            transcript,
            "prompts",
            effort=t.cast("SearchEffort", "invalid"),
        )


@pytest.mark.parametrize(
    ("scope", "expected_effort"),
    [
        ("prompts", "prompt"),
        ("conversations", "exhaustive"),
        ("all", "exhaustive"),
    ],
)
def test_omitted_effort_derives_from_scope(
    scope: SearchScope,
    expected_effort: SearchEffort,
) -> None:
    """Keep legacy broad scopes exhaustive when effort is omitted."""
    assert build_logical_search_plan(_query(scope=scope)).request.effort == expected_effort


def test_prompt_effort_rejects_transcripts_before_scope_shortcuts() -> None:
    """Keep caller-supplied source lists behind the same I/O boundary."""
    transcript = _source("transcript.jsonl")

    assert not source_matches_scope(transcript, "all", effort="prompt")
    assert source_matches_scope(transcript, "prompts", effort="exhaustive")


@pytest.mark.parametrize(
    ("agent", "store", "adapter_id"),
    [
        ("codex", "codex.state_db", "codex.state_sqlite.v1"),
        ("pi", "pi.context_mode_db", "pi.context_mode_sqlite.v1"),
    ],
)
@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("prompts", False),
        ("conversations", True),
        ("all", True),
    ],
)
def test_exhaustive_scope_keeps_app_state_out_of_deep_prompts(
    agent: AgentName,
    store: str,
    adapter_id: str,
    scope: SearchScope,
    expected: bool,
) -> None:
    """Reserve conversation-bearing app state for broad explicit scopes."""
    source = _app_state_source(agent, store, adapter_id)
    if scope == "prompts":
        store_roles = PROMPT_HISTORY_STORE_ROLES | CONVERSATION_STORE_ROLES
    elif scope == "conversations":
        store_roles = CONVERSATION_STORE_ROLES
    else:
        store_roles = None

    assert (
        descriptor_admits_store_roles(
            CATALOG.by_id(store),
            store_roles,
            allow_conversation_content_role_fallback=scope != "prompts",
        )
        is expected
    )
    assert source_matches_scope(source, scope, effort="exhaustive") is expected


@pytest.mark.parametrize(
    "store_roles",
    [
        frozenset({StoreRole.PRIMARY_CHAT}),
        CONVERSATION_STORE_ROLES,
        PROMPT_HISTORY_STORE_ROLES | CONVERSATION_STORE_ROLES,
    ],
)
def test_descriptor_role_filter_preserves_conversation_fallback(
    store_roles: frozenset[StoreRole],
) -> None:
    """Preserve public subset, exact-set, and superset role semantics."""
    for store in ("codex.state_db", "pi.context_mode_db"):
        assert descriptor_admits_store_roles(CATALOG.by_id(store), store_roles)


def test_direct_app_state_role_survives_disabled_fallback() -> None:
    """Disable only the role mismatch exception, not direct role selection."""
    assert descriptor_admits_store_roles(
        CATALOG.by_id("codex.state_db"),
        frozenset({StoreRole.APP_STATE}),
        allow_conversation_content_role_fallback=False,
    )


@pytest.mark.parametrize(
    ("scope", "effort", "expected"),
    [
        ("prompts", "exhaustive", False),
        ("conversations", "exhaustive", True),
        ("all", "exhaustive", True),
    ],
)
def test_search_discovery_controls_conversation_app_state_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: SearchScope,
    effort: SearchEffort,
    expected: bool,
) -> None:
    """Thread search scope intent through discovery before filesystem reads."""
    codex_root = tmp_path / ".codex"
    codex_root.mkdir()
    (codex_root / "state_5.sqlite").touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_root))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(codex_root))
    query = _query(scope=scope, effort=effort)

    sources = discover_sources_for_search(
        tmp_path,
        query,
        BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    assert any(source.store == "codex.state_db" for source in sources) is expected


def test_source_scope_retains_prompt_history_agents_keyword() -> None:
    """Preserve the pre-effort public fallback contract for Python callers."""
    transcript = _source("transcript.jsonl")

    assert source_matches_scope(transcript, "prompts")
    assert not source_matches_scope(
        transcript,
        "prompts",
        prompt_history_agents=frozenset({"codex"}),
    )
    with pytest.raises(ValueError, match="cannot combine effort"):
        source_matches_scope(
            transcript,
            "prompts",
            effort="exhaustive",
            prompt_history_agents=frozenset(),
        )


def test_exhaustive_limit_plans_drained_sources() -> None:
    """Disable source-local count bounds until global order is known."""
    source = _source("bounded.jsonl", search_root=pathlib.Path())
    query = _query(limit=1, effort="exhaustive")

    plan = build_physical_search_plan(
        query,
        [source],
        BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].limit_behavior == "drain_source"


@pytest.mark.parametrize(
    ("extra_args", "expected_order", "expected_text"),
    [
        ([], "relevance", "needle"),
        (["--no-rank"], "newest", "prefix needle suffix"),
    ],
)
def test_exhaustive_search_maps_rank_flag_to_engine_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
    expected_order: str,
    expected_text: str,
) -> None:
    """Pass both ranked and unranked result windows to the engine."""
    source = _source("ranking.jsonl")
    engine_order = [
        _record(source, "prefix needle suffix", "2026-02-01T00:00:00Z"),
        _record(source, "needle", "2026-01-01T00:00:00Z"),
    ]
    observed_requests: list[tuple[int | None, str]] = []

    def run_search_result(
        _home: pathlib.Path,
        query: SearchQuery,
        **_kwargs: object,
    ) -> SearchResult:
        observed_requests.append((query.limit, query.order))
        records = [engine_order[1] if query.order == "relevance" else engine_order[0]]
        summary = build_search_summary(
            query,
            effort=t.cast("SearchEffort", query.effort),
            coverage=RunCoverage(
                sources_discovered=1,
                sources_eligible=1,
                sources_planned=1,
                sources_attempted=1,
                sources_completed=1,
                sources_bounded=0,
                sources_skipped=0,
                sources_unsupported=0,
                sources_failed=0,
                sources_cancelled=0,
                records_seen=2,
                matches_seen=2,
            ),
            match_count=len(records),
            elapsed_seconds=0.0,
            result_limit_reached=True,
        )
        return SearchResult(tuple(records), summary)

    monkeypatch.setattr(cli_render, "run_search_result", run_search_result)
    parsed = parse_args(
        [
            "search",
            "--exhaustive",
            "--limit",
            "1",
            "--no-progress",
            *extra_args,
            "needle",
        ],
    )
    assert isinstance(parsed, SearchArgs)

    assert cli_render.run_search_command(parsed) == 0

    output = capsys.readouterr().out
    assert observed_requests == [(1, expected_order)]
    assert expected_text in output
