"""End-to-end tests for the query language wired into the search engine.

Covers commit 5 of the query-language project — verifies that
:func:`agentgrep.iter_search_events` consults
:attr:`agentgrep.SearchQuery.compiled.source_predicate` to prune
sources before any file is read, and that
:func:`agentgrep.matches_record` consults
:attr:`agentgrep.SearchQuery.compiled.record_predicate` after the
existing text match.

A call-tracking monkeypatch on :func:`agentgrep.iter_source_records`
proves that pruned sources never enter the file-read loop — that's
the architectural payoff of the source/record split.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
import agentgrep._engine.find as _rm_find
import agentgrep._engine.planning as _rm_planning
import agentgrep._engine.scanning as _rm_scanning
from agentgrep import events as ag_events
from agentgrep._engine import orchestration
from agentgrep.query import compile_query, default_registry, parse_query


def _make_source(
    *,
    agent: agentgrep.AgentName,
    path: str,
    store: str = "sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
) -> agentgrep.SourceHandle:
    """Build a synthetic SourceHandle for engine tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )


def _make_record(
    *,
    agent: agentgrep.AgentName,
    text: str,
    path: str = "/tmp/codex/sessions/abc.jsonl",
    timestamp: str | None = None,
    model: str | None = None,
) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for engine tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=agent,
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path(path),
        text=text,
        title=None,
        role="user",
        timestamp=timestamp,
        model=model,
        session_id=None,
        conversation_id=None,
        metadata={},
    )


def _compile_query(query_text: str) -> agentgrep.CompiledQuery:
    """Parse + compile, returning the CompiledQuery for SearchQuery.compiled."""
    ast = parse_query(query_text, default_registry())
    return compile_query(ast, default_registry())


def test_source_predicate_prunes_codex_sources_without_reading_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune claude sources without reading their records.

    Asserts via call-tracking on :func:`agentgrep.iter_source_records`
    that pruned sources never enter the read loop.
    """
    codex_source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    claude_source = _make_source(agent="claude", path="/tmp/claude.jsonl")
    codex_record = _make_record(agent="codex", text="bliss in codex")
    claude_record = _make_record(agent="claude", text="bliss in claude")

    sources_read: list[pathlib.Path] = []

    def _stub_iter(
        source: agentgrep.SourceHandle,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        sources_read.append(source.path)
        if source.agent == "codex":
            yield codex_record
        else:
            yield claude_record

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: [codex_source, claude_source],
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(_rm_scanning, "iter_source_records", _stub_iter)

    compiled = _compile_query("-agent:claude bliss")
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )

    emitted = [
        event.record
        for event in agentgrep.iter_search_events(pathlib.Path.home(), query)
        if isinstance(event, ag_events.RecordEmitted)
    ]

    assert sources_read == [codex_source.path]
    assert [r.agent for r in emitted] == ["codex"]


def test_record_predicate_filters_after_source_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent:codex model:claude` reads codex sources, filters records by model."""
    codex_source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    claude_source = _make_source(agent="claude", path="/tmp/claude.jsonl")
    matching = _make_record(
        agent="codex",
        text="bliss content",
        model="claude-3-sonnet",
    )
    non_matching = _make_record(
        agent="codex",
        text="bliss content",
        model="gpt-4",
    )

    def _stub_iter(
        source: agentgrep.SourceHandle,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        yield from (matching, non_matching)

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: [codex_source, claude_source],
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(_rm_scanning, "iter_source_records", _stub_iter)

    compiled = _compile_query("agent:codex model:claude bliss")
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )

    emitted = [
        event.record
        for event in agentgrep.iter_search_events(pathlib.Path.home(), query)
        if isinstance(event, ag_events.RecordEmitted)
    ]

    assert len(emitted) == 1
    assert emitted[0].model == "claude-3-sonnet"


class CompiledTermCase(t.NamedTuple):
    """One compiled boolean query evaluated against one record."""

    test_id: str
    query_text: str
    record_text: str
    expected: bool


COMPILED_TERM_CASES: tuple[CompiledTermCase, ...] = (
    CompiledTermCase(
        test_id="or-single-branch-matches",
        query_text="agent:codex (bliss OR absent)",
        record_text="only bliss here",
        expected=True,
    ),
    CompiledTermCase(
        test_id="or-other-branch-matches",
        query_text="agent:codex (bliss OR serenity)",
        record_text="pure serenity",
        expected=True,
    ),
    CompiledTermCase(
        test_id="or-no-branch-misses",
        query_text="agent:codex (bliss OR absent)",
        record_text="nothing relevant",
        expected=False,
    ),
    CompiledTermCase(
        test_id="negated-term-matches-without-it",
        query_text="agent:codex tmux -bliss",
        record_text="tmux only",
        expected=True,
    ),
    CompiledTermCase(
        test_id="negated-term-rejects-with-it",
        query_text="agent:codex tmux -bliss",
        record_text="tmux bliss",
        expected=False,
    ),
    CompiledTermCase(
        test_id="and-terms-still-required",
        query_text="agent:codex bliss tmux",
        record_text="bliss without the other",
        expected=False,
    ),
    CompiledTermCase(
        test_id="and-terms-all-present",
        query_text="agent:codex bliss tmux",
        record_text="bliss and tmux together",
        expected=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    COMPILED_TERM_CASES,
    ids=[c.test_id for c in COMPILED_TERM_CASES],
)
def test_compiled_predicate_owns_text_term_semantics(case: CompiledTermCase) -> None:
    """Boolean text terms follow the query's AND/OR/NOT structure.

    Regression guard: the record matcher pre-required every collected
    text term before the compiled predicate ran, so OR branches were
    conjoined and negated terms could never match anything.
    """
    compiled = _compile_query(case.query_text)
    query = agentgrep.SearchQuery(
        terms=compiled.text_terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )
    record = _make_record(agent="codex", text=case.record_text)

    assert agentgrep.matches_record(record, query) is case.expected


def test_search_emits_every_or_branch_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OR query returns records matching either branch end-to-end."""
    source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    records = [
        _make_record(agent="codex", text="only bliss here"),
        _make_record(agent="codex", text="pure serenity"),
        _make_record(agent="codex", text="nothing relevant"),
    ]

    def _stub_iter(
        source: agentgrep.SourceHandle,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        yield from records

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: [source],
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(_rm_scanning, "iter_source_records", _stub_iter)

    compiled = _compile_query("agent:codex (bliss OR serenity)")
    query = agentgrep.SearchQuery(
        terms=compiled.text_terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )

    emitted = [
        event.record.text
        for event in agentgrep.iter_search_events(pathlib.Path.home(), query)
        if isinstance(event, ag_events.RecordEmitted)
    ]

    assert sorted(emitted) == ["only bliss here", "pure serenity"]


def test_text_matches_finds_needle_in_model_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text term in a combined query matches against model and path fields.

    ``_text_matches`` must check the same fields as
    ``build_search_haystack``: text, title, role, model, and path.
    A record with the term only in ``model`` should survive the
    record predicate.
    """
    source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    # "sonnet" appears only in model, not in text/title/role
    record = _make_record(
        agent="codex",
        text="nothing relevant here",
        model="claude-sonnet",
    )

    def _stub_iter(
        source: agentgrep.SourceHandle,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        yield record

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: [source],
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(_rm_scanning, "iter_source_records", _stub_iter)

    compiled = _compile_query("agent:codex sonnet")
    query = agentgrep.SearchQuery(
        terms=("sonnet",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )

    emitted = [
        event.record
        for event in agentgrep.iter_search_events(pathlib.Path.home(), query)
        if isinstance(event, ag_events.RecordEmitted)
    ]
    assert len(emitted) == 1, "_text_matches should find 'sonnet' in record.model"


class EngineRoundtripCase(t.NamedTuple):
    """Parametrized case for end-to-end query → records via the engine."""

    test_id: str
    query: str
    records: tuple[agentgrep.SearchRecord, ...]
    expected_agents: tuple[str, ...]


def _build_engine_records() -> tuple[agentgrep.SearchRecord, ...]:
    """Build a small heterogeneous record set used by the roundtrip tests."""
    return (
        _make_record(
            agent="codex",
            text="bliss in codex",
            timestamp="2026-03-15T10:00:00Z",
            path="/tmp/codex/sessions/a.jsonl",
        ),
        _make_record(
            agent="claude",
            text="bliss in claude",
            timestamp="2026-04-15T10:00:00Z",
            path="/tmp/claude/sessions/b.jsonl",
        ),
        _make_record(
            agent="cursor-cli",
            text="bliss in cursor",
            timestamp="2025-12-15T10:00:00Z",
            path="/tmp/cursor/sessions/c.jsonl",
        ),
    )


ENGINE_ROUNDTRIP_CASES: tuple[EngineRoundtripCase, ...] = (
    EngineRoundtripCase(
        test_id="single-agent-filter",
        query="agent:codex bliss",
        records=_build_engine_records(),
        expected_agents=("codex",),
    ),
    EngineRoundtripCase(
        test_id="or-of-two-agents",
        query="(agent:codex OR agent:claude) bliss",
        records=_build_engine_records(),
        expected_agents=("codex", "claude"),
    ),
    EngineRoundtripCase(
        test_id="negated-agent",
        query="-agent:cursor-cli bliss",
        records=_build_engine_records(),
        expected_agents=("codex", "claude"),
    ),
    EngineRoundtripCase(
        test_id="timestamp-range-filters-records",
        query="timestamp:>2026-01-01 bliss",
        records=_build_engine_records(),
        expected_agents=("codex", "claude"),
    ),
)


@pytest.mark.parametrize(
    "case",
    ENGINE_ROUNDTRIP_CASES,
    ids=[c.test_id for c in ENGINE_ROUNDTRIP_CASES],
)
def test_engine_routes_query_through_predicates(
    case: EngineRoundtripCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: query string → engine → expected agent set."""
    sources = [_make_source(agent=record.agent, path=str(record.path)) for record in case.records]

    records_by_source = {
        source.path: record for source, record in zip(sources, case.records, strict=False)
    }

    def _stub_iter(
        source: agentgrep.SourceHandle,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        record = records_by_source.get(source.path)
        if record is not None:
            yield record

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(_rm_scanning, "iter_source_records", _stub_iter)

    compiled = _compile_query(case.query)
    query = agentgrep.SearchQuery(
        terms=compiled.text_terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )

    emitted_agents = [
        event.record.agent
        for event in agentgrep.iter_search_events(pathlib.Path.home(), query)
        if isinstance(event, ag_events.RecordEmitted)
    ]
    assert tuple(sorted(emitted_agents)) == tuple(sorted(case.expected_agents))


class FindPipelineCase(t.NamedTuple):
    """Parametrized case for `iter_find_events` source pruning."""

    test_id: str
    query: str
    expected_agents: tuple[str, ...]


FIND_PIPELINE_CASES: tuple[FindPipelineCase, ...] = (
    FindPipelineCase(
        test_id="single-agent-find",
        query="agent:codex",
        expected_agents=("codex",),
    ),
    FindPipelineCase(
        test_id="or-of-two-agents-find",
        query="agent:codex OR agent:claude",
        expected_agents=("claude", "codex"),
    ),
    FindPipelineCase(
        test_id="negated-agent-find",
        query="-agent:cursor-cli",
        expected_agents=("claude", "codex"),
    ),
    FindPipelineCase(
        test_id="path-substring-find",
        query="path:codex",
        expected_agents=("codex",),
    ),
)


@pytest.mark.parametrize(
    "case",
    FIND_PIPELINE_CASES,
    ids=[c.test_id for c in FIND_PIPELINE_CASES],
)
def test_find_pipeline_consumes_compiled_query(
    case: FindPipelineCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iter_find_events prunes sources via CompiledQuery.source_predicate."""
    sources = [
        _make_source(agent="codex", path="/tmp/codex/a.jsonl"),
        _make_source(agent="claude", path="/tmp/claude/b.jsonl"),
        _make_source(agent="cursor-cli", path="/tmp/cursor/c.jsonl"),
    ]
    monkeypatch.setattr(
        _rm_find,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )

    compiled = _compile_query(case.query)
    emitted = [
        event.record
        for event in agentgrep.iter_find_events(
            pathlib.Path.home(),
            agentgrep.AGENT_CHOICES,
            pattern=None,
            limit=None,
            compiled=compiled,
        )
        if isinstance(event, ag_events.FindRecordEmitted)
    ]
    assert tuple(sorted(r.agent for r in emitted)) == case.expected_agents


def test_find_pipeline_matches_home_relative_path_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``find 'path:~/.codex agent:codex'`` emits the matching Codex source."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sources = [
        _make_source(
            agent="codex",
            path=str(home / ".codex" / "history.jsonl"),
            store="codex.history",
            adapter_id="codex.history_jsonl.v1",
        ),
        _make_source(
            agent="claude",
            path=str(home / ".claude" / "history.jsonl"),
            store="claude.history",
            adapter_id="claude.history_jsonl.v1",
        ),
    ]
    monkeypatch.setattr(
        _rm_find,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )

    compiled = _compile_query("path:~/.codex agent:codex")
    emitted = [
        event.record
        for event in agentgrep.iter_find_events(
            pathlib.Path.home(),
            agentgrep.AGENT_CHOICES,
            pattern=None,
            limit=None,
            compiled=compiled,
        )
        if isinstance(event, ag_events.FindRecordEmitted)
    ]

    assert [(record.agent, record.path) for record in emitted] == [
        ("codex", home / ".codex" / "history.jsonl"),
    ]


def test_find_pipeline_compiled_none_keeps_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``compiled=None``, iter_find_events takes the existing path."""
    sources = [_make_source(agent="codex", path="/tmp/codex.jsonl")]
    monkeypatch.setattr(
        _rm_find,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )

    emitted = list(
        agentgrep.iter_find_events(
            pathlib.Path.home(),
            agentgrep.AGENT_CHOICES,
            pattern=None,
            limit=None,
        ),
    )
    record_events = [e for e in emitted if isinstance(e, ag_events.FindRecordEmitted)]
    assert len(record_events) == 1


class SearchSourcePruneCase(t.NamedTuple):
    """Parametrized case proving the eager search path honors source_predicate."""

    test_id: str
    query: str
    source_agents: tuple[str, ...]
    expected_read_agents: tuple[str, ...]


SEARCH_SOURCE_PRUNE_CASES: tuple[SearchSourcePruneCase, ...] = (
    SearchSourcePruneCase(
        test_id="single-agent-prune",
        query="agent:codex bliss",
        source_agents=("codex", "claude", "cursor-cli"),
        expected_read_agents=("codex",),
    ),
    SearchSourcePruneCase(
        test_id="or-two-agents-prune",
        query="(agent:codex OR agent:claude) bliss",
        source_agents=("codex", "claude", "cursor-cli", "gemini"),
        expected_read_agents=("codex", "claude"),
    ),
    SearchSourcePruneCase(
        test_id="negation-prune",
        query="-agent:claude bliss",
        source_agents=("codex", "claude", "cursor-cli"),
        expected_read_agents=("codex", "cursor-cli"),
    ),
)


@pytest.mark.parametrize(
    "case",
    SEARCH_SOURCE_PRUNE_CASES,
    ids=[c.test_id for c in SEARCH_SOURCE_PRUNE_CASES],
)
def test_eager_search_path_prunes_sources_before_reading(
    case: SearchSourcePruneCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The eager run_search_query path skips pruned sources without reading them."""
    sources = [
        _make_source(agent=t.cast("agentgrep.AgentName", agent), path=f"/tmp/{agent}.jsonl")
        for agent in case.source_agents
    ]
    sources_read: list[str] = []

    def _stub_iter(
        source: agentgrep.SourceHandle,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        sources_read.append(source.agent)
        yield _make_record(
            agent=source.agent,
            text="bliss content",
            path=str(source.path),
        )

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources_, backends, **kwargs: list(sources_),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(_rm_scanning, "iter_source_records", _stub_iter)

    compiled = _compile_query(case.query)
    query = agentgrep.SearchQuery(
        terms=compiled.text_terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        compiled=compiled,
    )
    records = agentgrep.run_search_query(pathlib.Path.home(), query)
    assert tuple(sorted(sources_read)) == tuple(sorted(case.expected_read_agents))
    assert tuple(sorted(r.agent for r in records)) == tuple(sorted(case.expected_read_agents))


class FindEagerSourcePruneCase(t.NamedTuple):
    """Parametrized case for find's eager output modes honoring source predicates."""

    test_id: str
    argv: tuple[str, ...]
    source_agents: tuple[str, ...]
    expected_agents: tuple[str, ...]


FIND_EAGER_SOURCE_PRUNE_CASES: tuple[FindEagerSourcePruneCase, ...] = (
    FindEagerSourcePruneCase(
        test_id="json-mode-agent-prune",
        argv=("find", "--no-progress", "--json", "agent:codex"),
        source_agents=("codex", "claude", "cursor-cli", "gemini"),
        expected_agents=("codex",),
    ),
    FindEagerSourcePruneCase(
        test_id="json-mode-negated-prune",
        argv=("find", "--no-progress", "--json", "NOT agent:claude"),
        source_agents=("codex", "claude", "cursor-cli"),
        expected_agents=("codex", "cursor-cli"),
    ),
    FindEagerSourcePruneCase(
        test_id="list-details-agent-prune",
        argv=("find", "--no-progress", "-l", "agent:codex"),
        source_agents=("codex", "claude", "cursor-cli", "gemini"),
        expected_agents=("codex",),
    ),
    FindEagerSourcePruneCase(
        test_id="list-details-or-prune",
        argv=("find", "--no-progress", "-l", "(agent:codex OR agent:cursor-cli)"),
        source_agents=("codex", "claude", "cursor-cli", "gemini"),
        expected_agents=("codex", "cursor-cli"),
    ),
)


@pytest.mark.parametrize(
    "case",
    FIND_EAGER_SOURCE_PRUNE_CASES,
    ids=[c.test_id for c in FIND_EAGER_SOURCE_PRUNE_CASES],
)
def test_find_eager_path_honors_source_predicate(
    case: FindEagerSourcePruneCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both eager find output modes prune sources via the compiled query."""
    sources = [
        _make_source(agent=t.cast("agentgrep.AgentName", agent), path=f"/tmp/{agent}.jsonl")
        for agent in case.source_agents
    ]
    monkeypatch.setattr(
        _rm_find,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )

    args = agentgrep.parse_args(list(case.argv))
    assert args is not None
    assert isinstance(args, agentgrep.FindArgs)
    exit_code = agentgrep.run_find_command(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    # Parse the agent set out of actual records, not a naive
    # substring scan — the --json envelope carries the query's
    # agent list as metadata and a substring check would falsely
    # match successfully-pruned agents.
    if "--json" in case.argv:
        import json

        payload = json.loads(captured.out)
        emitted_agents = {record["agent"] for record in payload["results"]}
    else:
        # Long-format output: one record per line, agent in the
        # first tab-separated column.
        emitted_agents = {
            line.split("\t", 1)[0] for line in captured.out.splitlines() if line.strip()
        }
    assert emitted_agents == set(case.expected_agents)


class FastDiscoveryEntrypointCase(t.NamedTuple):
    """One frontend entrypoint that should use metadata-free discovery."""

    test_id: str
    entrypoint: str


FAST_DISCOVERY_ENTRYPOINT_CASES: tuple[FastDiscoveryEntrypointCase, ...] = (
    FastDiscoveryEntrypointCase(test_id="run-search-query", entrypoint="run_search_query"),
    FastDiscoveryEntrypointCase(test_id="iter-search-events", entrypoint="iter_search_events"),
    FastDiscoveryEntrypointCase(test_id="run-find-query", entrypoint="run_find_query"),
    FastDiscoveryEntrypointCase(test_id="iter-find-events", entrypoint="iter_find_events"),
)


@pytest.mark.parametrize(
    "case",
    FAST_DISCOVERY_ENTRYPOINT_CASES,
    ids=[c.test_id for c in FAST_DISCOVERY_ENTRYPOINT_CASES],
)
def test_fast_entrypoints_request_metadata_free_discovery(
    case: FastDiscoveryEntrypointCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search and find frontends request the fastest discovery path."""
    source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    calls: list[dict[str, object]] = []

    def fake_discover_sources(
        home: pathlib.Path,
        agents: tuple[agentgrep.AgentName, ...],
        backends: agentgrep.BackendSelection,
        **kwargs: object,
    ) -> list[agentgrep.SourceHandle]:
        del home, agents, backends
        calls.append(kwargs)
        return [source]

    monkeypatch.setattr(orchestration, "discover_sources", fake_discover_sources)
    monkeypatch.setattr(_rm_find, "discover_sources", fake_discover_sources)
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)

    def iter_no_records(
        _source: agentgrep.SourceHandle,
        **_kwargs: object,
    ) -> t.Iterator[agentgrep.SearchRecord]:
        return iter(())

    monkeypatch.setattr(_rm_scanning, "iter_source_records", iter_no_records)

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
    )

    if case.entrypoint == "run_search_query":
        _ = agentgrep.run_search_query(pathlib.Path.home(), query)
    elif case.entrypoint == "iter_search_events":
        _ = list(agentgrep.iter_search_events(pathlib.Path.home(), query))
    elif case.entrypoint == "run_find_query":
        _ = agentgrep.run_find_query(
            pathlib.Path.home(),
            ("codex",),
            pattern=None,
            limit=1,
        )
    elif case.entrypoint == "iter_find_events":
        _ = list(
            agentgrep.iter_find_events(
                pathlib.Path.home(),
                ("codex",),
                pattern=None,
                limit=1,
            ),
        )
    else:
        msg = f"unknown entrypoint: {case.entrypoint}"
        raise AssertionError(msg)

    assert calls
    assert all(call.get("version_detail") == "none" for call in calls)


class SearchDiscoveryRoleCase(t.NamedTuple):
    """One search scope and the store-role discovery calls it should issue."""

    test_id: str
    scope: agentgrep.SearchScope
    prompt_history_agents: frozenset[agentgrep.AgentName]
    expected_calls: tuple[
        tuple[
            tuple[agentgrep.AgentName, ...],
            frozenset[agentgrep.StoreRole] | None,
        ],
        ...,
    ]


SEARCH_DISCOVERY_ROLE_CASES: tuple[SearchDiscoveryRoleCase, ...] = (
    SearchDiscoveryRoleCase(
        test_id="all-keeps-default-discovery",
        scope="all",
        prompt_history_agents=frozenset(),
        expected_calls=((("codex", "claude"), None),),
    ),
    SearchDiscoveryRoleCase(
        test_id="conversations-discovers-conversation-roles",
        scope="conversations",
        prompt_history_agents=frozenset(),
        expected_calls=((("codex", "claude"), agentgrep.CONVERSATION_STORE_ROLES),),
    ),
    SearchDiscoveryRoleCase(
        test_id="prompts-falls-back-per-agent",
        scope="prompts",
        prompt_history_agents=frozenset({"codex"}),
        expected_calls=(
            (("codex", "claude"), frozenset({agentgrep.StoreRole.PROMPT_HISTORY})),
            (("claude",), agentgrep.CONVERSATION_STORE_ROLES),
        ),
    ),
    SearchDiscoveryRoleCase(
        test_id="prompts-skips-fallback-when-history-exists",
        scope="prompts",
        prompt_history_agents=frozenset({"codex", "claude"}),
        expected_calls=((("codex", "claude"), frozenset({agentgrep.StoreRole.PROMPT_HISTORY})),),
    ),
)


@pytest.mark.parametrize(
    "case",
    SEARCH_DISCOVERY_ROLE_CASES,
    ids=[c.test_id for c in SEARCH_DISCOVERY_ROLE_CASES],
)
def test_search_discovery_pushes_scope_into_store_roles(
    case: SearchDiscoveryRoleCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search discovery avoids enumerating store roles the query scope cannot use."""
    calls: list[
        tuple[
            tuple[agentgrep.AgentName, ...],
            frozenset[agentgrep.StoreRole] | None,
        ],
    ] = []

    def fake_discover_sources(
        home: pathlib.Path,
        agents: tuple[agentgrep.AgentName, ...],
        backends: agentgrep.BackendSelection,
        **kwargs: object,
    ) -> list[agentgrep.SourceHandle]:
        del home, backends
        store_roles = t.cast("frozenset[agentgrep.StoreRole] | None", kwargs.get("store_roles"))
        calls.append((agents, store_roles))
        if store_roles == frozenset({agentgrep.StoreRole.PROMPT_HISTORY}):
            prompt_sources = {
                "codex": ("codex.history", "codex.history_jsonl.v1"),
                "claude": ("claude.history", "claude.history_jsonl.v1"),
            }
            return [
                _make_source(
                    agent=agent,
                    path=f"/tmp/{agent}/history.jsonl",
                    store=prompt_sources[agent][0],
                    adapter_id=prompt_sources[agent][1],
                )
                for agent in agents
                if agent in case.prompt_history_agents and agent in prompt_sources
            ]
        return [
            _make_source(
                agent=agent,
                path=f"/tmp/{agent}/session.jsonl",
            )
            for agent in agents
        ]

    monkeypatch.setattr(orchestration, "discover_sources", fake_discover_sources)

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope=case.scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex", "claude"),
        limit=1,
    )

    _ = agentgrep.discover_sources_for_search(
        pathlib.Path.home(),
        query,
        agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
        version_detail="none",
    )

    assert calls == list(case.expected_calls)


class QueryPassesThroughCase(t.NamedTuple):
    """Parametrized case verifying CLI parsing routes query syntax to compiled."""

    test_id: str
    argv: tuple[str, ...]
    expect_compiled: bool


QUERY_PASSES_THROUGH_CASES: tuple[QueryPassesThroughCase, ...] = (
    QueryPassesThroughCase(
        test_id="grep-bare-term-legacy-path",
        argv=("grep", "bliss"),
        expect_compiled=False,
    ),
    QueryPassesThroughCase(
        test_id="grep-field-syntax-compiled",
        argv=("grep", "agent:codex", "bliss"),
        expect_compiled=True,
    ),
    QueryPassesThroughCase(
        test_id="find-no-pattern-legacy-path",
        argv=("find",),
        expect_compiled=False,
    ),
    QueryPassesThroughCase(
        test_id="find-bare-term-legacy-path",
        argv=("find", "sessions"),
        expect_compiled=False,
    ),
    QueryPassesThroughCase(
        test_id="find-field-syntax-compiled",
        argv=("find", "agent:codex"),
        expect_compiled=True,
    ),
    QueryPassesThroughCase(
        test_id="search-bare-term-legacy-path",
        argv=("search", "bliss"),
        expect_compiled=False,
    ),
    QueryPassesThroughCase(
        test_id="search-field-syntax-compiled",
        argv=("search", "agent:codex", "bliss"),
        expect_compiled=True,
    ),
    QueryPassesThroughCase(
        test_id="search-bare-or-engages",
        argv=("search", "ruff", "OR", "uv"),
        expect_compiled=True,
    ),
    QueryPassesThroughCase(
        test_id="search-bare-not-engages",
        argv=("search", "NOT", "tmux"),
        expect_compiled=True,
    ),
    QueryPassesThroughCase(
        test_id="search-plain-terms-legacy-path",
        argv=("search", "ruff", "uv", "tmux"),
        expect_compiled=False,
    ),
    QueryPassesThroughCase(
        test_id="search-lowercase-or-stays-literal",
        argv=("search", "ruff", "or", "uv"),
        expect_compiled=False,
    ),
    QueryPassesThroughCase(
        test_id="grep-bare-or-engages",
        argv=("grep", "ruff", "OR", "uv"),
        expect_compiled=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    QUERY_PASSES_THROUGH_CASES,
    ids=[c.test_id for c in QUERY_PASSES_THROUGH_CASES],
)
def test_cli_parsing_routes_query_syntax_to_compiled(
    case: QueryPassesThroughCase,
) -> None:
    """CLI parsing populates Args.compiled when (and only when) field syntax appears."""
    args = agentgrep.parse_args(list(case.argv))
    assert args is not None
    assert hasattr(args, "compiled")
    compiled = t.cast(
        "agentgrep.CompiledQuery | None",
        t.cast("t.Any", args).compiled,
    )
    if case.expect_compiled:
        assert compiled is not None
        assert compiled.is_pure_text is False
    else:
        assert compiled is None


def test_plain_terms_do_not_import_query_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain bare-term parsing must not import the heavy query module.

    The CLI gate decides whether to engage the parser with a cheap
    pure-Python heuristic so ``agentgrep search ruff uv tmux`` keeps the
    cold-start budget. Dropping the cached module and re-parsing proves
    the fast path never triggers the import.
    """
    import sys

    monkeypatch.delitem(sys.modules, "agentgrep.query", raising=False)
    monkeypatch.delitem(sys.modules, "agentgrep.query.compile", raising=False)
    monkeypatch.delitem(sys.modules, "agentgrep.query.parser", raising=False)

    args = agentgrep.parse_args(["search", "ruff", "uv", "tmux"])

    assert args is not None
    assert "agentgrep.query" not in sys.modules


def test_cli_query_field_names_mirror_the_registry() -> None:
    """The CLI gate's hardcoded field names must not drift from the registry.

    ``cli.parser`` hardcodes the queryable field names so the cold-start
    gate never imports the query module. This guard fails if a field or
    alias is added to the registry without updating that mirror.
    """
    from agentgrep.cli import parser as cli_parser

    registry = default_registry()
    expected = {name for spec in registry.specs for name in (spec.name, *spec.aliases)}
    assert expected == cli_parser._QUERY_FIELD_NAMES


def test_origin_query_field_sets_mirror_the_registry() -> None:
    """The origin field-name sets must not drift from the registry.

    ``query.evaluate`` dispatches origin predicates through the
    ``agentgrep.origin`` constants; a registered origin field missing
    from them would silently never match (evaluation falls through to
    ``return False`` with no error).
    """
    from agentgrep.origin import ORIGIN_PATH_QUERY_FIELDS, ORIGIN_STRING_QUERY_FIELDS

    registry = default_registry()
    record_path_fields = {
        spec.name for spec in registry.specs if spec.layer == "record" and spec.kind == "path"
    }
    assert record_path_fields == ORIGIN_PATH_QUERY_FIELDS
    specs_by_name = {spec.name: spec for spec in registry.specs}
    for name in sorted(ORIGIN_STRING_QUERY_FIELDS):
        spec = specs_by_name[name]
        assert spec.layer == "record"
        assert spec.kind == "string"


class PureTextResidualCase(t.NamedTuple):
    """Parametrized case: query syntax that collapses to clean residual terms."""

    test_id: str
    argv: tuple[str, ...]
    expected_terms: tuple[str, ...]


PURE_TEXT_RESIDUAL_CASES: tuple[PureTextResidualCase, ...] = (
    PureTextResidualCase(
        test_id="leading-quote-phrase-unquoted",
        argv=("search", '"deploy v1"'),
        expected_terms=("deploy v1",),
    ),
    PureTextResidualCase(
        test_id="phrase-collapses-internal-whitespace",
        argv=("search", '"deploy    v1"'),
        expected_terms=("deploy v1",),
    ),
)


@pytest.mark.parametrize(
    "case",
    PURE_TEXT_RESIDUAL_CASES,
    ids=[c.test_id for c in PURE_TEXT_RESIDUAL_CASES],
)
def test_pure_text_query_syntax_extracts_clean_terms(
    case: PureTextResidualCase,
) -> None:
    """A parsed query that is pure text routes clean residual terms, no predicate.

    Phrases engage the parser (leading quote) but compile to pure text,
    so ``compiled`` stays ``None`` and the unquoted, whitespace-collapsed
    phrase flows to the legacy fast path as a single term.
    """
    args = agentgrep.parse_args(list(case.argv))
    assert args is not None
    terms = t.cast("tuple[str, ...]", t.cast("t.Any", args).terms)
    compiled = t.cast(
        "agentgrep.CompiledQuery | None",
        t.cast("t.Any", args).compiled,
    )
    assert terms == case.expected_terms
    assert compiled is None


class MangledFieldPredicateCase(t.NamedTuple):
    """Parametrized case for argparse mangling rejection."""

    test_id: str
    argv: tuple[str, ...]


MANGLED_FIELD_PREDICATE_CASES: tuple[MangledFieldPredicateCase, ...] = (
    MangledFieldPredicateCase(
        test_id="find-mangled-agent",
        argv=("find", "-agent:claude"),
    ),
    MangledFieldPredicateCase(
        test_id="grep-mangled-path",
        argv=("grep", "-path:/foo", "bliss"),
    ),
    MangledFieldPredicateCase(
        test_id="find-mangled-timestamp",
        argv=("find", "-timestamp:2026"),
    ),
    MangledFieldPredicateCase(
        test_id="grep-mangled-scope",
        argv=("grep", "-scope:prompts", "bliss"),
    ),
)


@pytest.mark.parametrize(
    "case",
    MANGLED_FIELD_PREDICATE_CASES,
    ids=[c.test_id for c in MANGLED_FIELD_PREDICATE_CASES],
)
def test_mangled_field_predicate_rejected_at_parse_time(
    case: MangledFieldPredicateCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `-FIELD:VALUE` token errors with a workaround-hint before argparse mangles it."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(list(case.argv))
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "looks like a field predicate" in captured.err
    # Workaround list mentions only `--` and the `NOT` keyword.
    # "quoted positional" used to be there too but doesn't actually
    # work — the shell strips quotes before argparse runs.
    assert "--" in captured.err
    assert "NOT" in captured.err
    assert "quoted positional" not in captured.err


class MangledFieldFalsePositiveCase(t.NamedTuple):
    """Parametrized case ensuring valid `--`-escaped usage still parses."""

    test_id: str
    argv: tuple[str, ...]


MANGLED_FIELD_FALSE_POSITIVE_CASES: tuple[MangledFieldFalsePositiveCase, ...] = (
    MangledFieldFalsePositiveCase(
        test_id="dash-dash-escape",
        argv=("find", "--", "-agent:claude"),
    ),
    MangledFieldFalsePositiveCase(
        test_id="quoted-negation-via-NOT",
        argv=("find", "NOT agent:claude"),
    ),
    MangledFieldFalsePositiveCase(
        test_id="legitimate-short-flag-still-works",
        argv=("find", "-a", "agent:codex"),
    ),
    MangledFieldFalsePositiveCase(
        test_id="unrecognized-field-not-mangled",
        argv=("find", "-unknown:value"),
    ),
)


@pytest.mark.parametrize(
    "case",
    MANGLED_FIELD_FALSE_POSITIVE_CASES,
    ids=[c.test_id for c in MANGLED_FIELD_FALSE_POSITIVE_CASES],
)
def test_mangled_predicate_check_does_not_false_positive(
    case: MangledFieldFalsePositiveCase,
) -> None:
    """The mangle-check leaves valid argv alone (or fails for unrelated reasons)."""
    # The point isn't that all of these succeed — some may fail for
    # other reasons (e.g., `--` followed by an option-shaped
    # positional, unknown short flag). The point is that they DON'T
    # hit the `looks like a field predicate` error.
    import contextlib

    with contextlib.suppress(SystemExit):
        _ = agentgrep.parse_args(list(case.argv))


class FlagFieldCollisionCase(t.NamedTuple):
    """Parametrized case for flag-vs-field collision rejection."""

    test_id: str
    argv: tuple[str, ...]
    expected_message_fragment: str


FLAG_FIELD_COLLISION_CASES: tuple[FlagFieldCollisionCase, ...] = (
    FlagFieldCollisionCase(
        test_id="grep-agent-flag-and-field",
        argv=("grep", "--agent", "codex", "agent:claude", "bliss"),
        expected_message_fragment="cannot combine --agent flag with agent: field",
    ),
    FlagFieldCollisionCase(
        test_id="find-agent-flag-and-field",
        argv=("find", "--agent", "codex", "agent:claude"),
        expected_message_fragment="cannot combine --agent flag with agent: field",
    ),
    FlagFieldCollisionCase(
        test_id="grep-scope-flag-and-field",
        argv=("grep", "--scope", "conversations", "scope:prompts", "bliss"),
        expected_message_fragment="cannot combine --scope flag with scope: field",
    ),
    FlagFieldCollisionCase(
        test_id="grep-default-scope-flag-and-field",
        argv=("grep", "--scope", "prompts", "scope:conversations", "bliss"),
        expected_message_fragment="cannot combine --scope flag with scope: field",
    ),
    FlagFieldCollisionCase(
        test_id="search-scope-flag-and-field",
        argv=("search", "--scope", "conversations", "scope:prompts", "bliss"),
        expected_message_fragment="cannot combine --scope flag with scope: field",
    ),
    FlagFieldCollisionCase(
        test_id="search-default-scope-flag-and-field",
        argv=("search", "--scope", "prompts", "scope:conversations", "bliss"),
        expected_message_fragment="cannot combine --scope flag with scope: field",
    ),
    FlagFieldCollisionCase(
        test_id="search-cwd-flag-and-field",
        argv=("search", "--cwd", "/workspace/a", "cwd:/workspace/b", "bliss"),
        expected_message_fragment="cannot combine --cwd flag with cwd: field",
    ),
    FlagFieldCollisionCase(
        test_id="search-repo-flag-and-field",
        argv=("search", "--repo", "/workspace/a", "repo:/workspace/b", "bliss"),
        expected_message_fragment="cannot combine --repo flag with repo: field",
    ),
    FlagFieldCollisionCase(
        test_id="search-branch-flag-and-field",
        argv=("search", "--branch", "main", "branch:feature", "bliss"),
        expected_message_fragment="cannot combine --branch flag with branch: field",
    ),
)


@pytest.mark.parametrize(
    "case",
    FLAG_FIELD_COLLISION_CASES,
    ids=[c.test_id for c in FLAG_FIELD_COLLISION_CASES],
)
def test_flag_field_collision_errors_at_parse_time(
    case: FlagFieldCollisionCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mixing a flag and the equivalent field predicate errors with exit 2."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(list(case.argv))
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert case.expected_message_fragment in captured.err


def test_no_collision_when_only_field_used() -> None:
    """Bare `agent:codex` (no `--agent`) parses cleanly."""
    args = agentgrep.parse_args(["grep", "agent:codex", "bliss"])
    assert args is not None
    assert isinstance(args, agentgrep.GrepArgs)
    assert args.compiled is not None


def test_no_collision_when_only_flag_used() -> None:
    """Bare `--agent codex` (no `agent:`) parses cleanly."""
    args = agentgrep.parse_args(["grep", "--agent", "codex", "bliss"])
    assert args is not None
    assert isinstance(args, agentgrep.GrepArgs)
    assert args.compiled is None


def test_grep_query_with_no_text_pattern_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentgrep grep agent:codex`` (no text) errors with a steering message."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(["grep", "agent:codex"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "at least one text pattern" in captured.err


def test_compiled_none_falls_through_to_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `compiled is None`, the engine takes its existing code path."""
    source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    record = _make_record(agent="codex", text="bliss")

    monkeypatch.setattr(
        orchestration,
        "discover_sources",
        lambda *args, **kwargs: [source],
    )
    monkeypatch.setattr(
        orchestration,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(_rm_planning, "direct_source_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        _rm_scanning,
        "iter_source_records",
        lambda src: iter([record]),
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
    )
    assert query.compiled is None

    emitted = [
        event
        for event in agentgrep.iter_search_events(pathlib.Path.home(), query)
        if isinstance(event, ag_events.RecordEmitted)
    ]
    assert len(emitted) == 1


class GrepCasePredicateCase(t.NamedTuple):
    """Parametrized case for grep case flags on compiled query predicates."""

    test_id: str
    argv: tuple[str, ...]
    expected: bool


GREP_CASE_PREDICATE_CASES: tuple[GrepCasePredicateCase, ...] = (
    GrepCasePredicateCase(
        test_id="case-sensitive-flag-excludes-lowercase",
        argv=("grep", "-s", "text:Foo agent:codex"),
        expected=False,
    ),
    GrepCasePredicateCase(
        test_id="ignore-case-flag-includes-lowercase",
        argv=("grep", "-i", "text:Foo agent:codex"),
        expected=True,
    ),
    GrepCasePredicateCase(
        test_id="smart-case-uppercase-excludes-lowercase",
        argv=("grep", "text:Foo agent:codex"),
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    GREP_CASE_PREDICATE_CASES,
    ids=[c.test_id for c in GREP_CASE_PREDICATE_CASES],
)
def test_grep_case_flags_reach_compiled_predicate(
    case: GrepCasePredicateCase,
) -> None:
    """Grep's case resolution governs text: predicates like line matching."""
    args = agentgrep.parse_args(list(case.argv))
    assert isinstance(args, agentgrep.GrepArgs)
    assert args.compiled is not None
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="foo lowercase only",
    )
    query = agentgrep.SearchQuery(
        terms=args.patterns,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
        compiled=args.compiled,
    )
    assert agentgrep.matches_record(record, query) is case.expected
