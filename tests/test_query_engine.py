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
from agentgrep import events as ag_events
from agentgrep.query import compile_query, default_registry, parse_query


def _make_source(
    *,
    agent: agentgrep.AgentName,
    path: str,
    adapter_id: str = "codex.sessions_jsonl.v1",
) -> agentgrep.SourceHandle:
    """Build a synthetic SourceHandle for engine tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store="sessions",
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
        agentgrep,
        "discover_sources",
        lambda *args, **kwargs: [codex_source, claude_source],
    )
    monkeypatch.setattr(
        agentgrep,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(agentgrep, "iter_source_records", _stub_iter)

    compiled = _compile_query("-agent:claude bliss")
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
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
        agentgrep,
        "discover_sources",
        lambda *args, **kwargs: [codex_source, claude_source],
    )
    monkeypatch.setattr(
        agentgrep,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(agentgrep, "iter_source_records", _stub_iter)

    compiled = _compile_query("agent:codex model:claude bliss")
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
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
            agent="cursor",
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
        query="-agent:cursor bliss",
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
        agentgrep,
        "discover_sources",
        lambda *args, **kwargs: list(sources),
    )
    monkeypatch.setattr(
        agentgrep,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(agentgrep, "iter_source_records", _stub_iter)

    compiled = _compile_query(case.query)
    query = agentgrep.SearchQuery(
        terms=compiled.text_terms,
        search_type="prompts",
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
        query="-agent:cursor",
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
        _make_source(agent="cursor", path="/tmp/cursor/c.jsonl"),
    ]
    monkeypatch.setattr(
        agentgrep,
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


def test_find_pipeline_compiled_none_keeps_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``compiled=None``, iter_find_events takes the existing path."""
    sources = [_make_source(agent="codex", path="/tmp/codex.jsonl")]
    monkeypatch.setattr(
        agentgrep,
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


class QueryPassesThroughCase(t.NamedTuple):
    """Parametrized case verifying CLI parsing routes query syntax to compiled."""

    test_id: str
    argv: tuple[str, ...]
    expect_compiled: bool


QUERY_PASSES_THROUGH_CASES: tuple[QueryPassesThroughCase, ...] = (
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


class FlagFieldCollisionCase(t.NamedTuple):
    """Parametrized case for flag-vs-field collision rejection."""

    test_id: str
    argv: tuple[str, ...]
    expected_message_fragment: str


FLAG_FIELD_COLLISION_CASES: tuple[FlagFieldCollisionCase, ...] = (
    FlagFieldCollisionCase(
        test_id="search-agent-flag-and-field",
        argv=("search", "--agent", "codex", "agent:claude", "bliss"),
        expected_message_fragment="cannot combine --agent flag with agent: field",
    ),
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
        test_id="search-type-flag-and-field",
        argv=("search", "--type", "history", "type:prompts", "bliss"),
        expected_message_fragment="cannot combine --type flag with type: field",
    ),
    FlagFieldCollisionCase(
        test_id="grep-type-flag-and-field",
        argv=("grep", "--type", "history", "type:prompts", "bliss"),
        expected_message_fragment="cannot combine --type flag with type: field",
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
    args = agentgrep.parse_args(["search", "agent:codex", "bliss"])
    assert args is not None
    assert isinstance(args, agentgrep.SearchArgs)
    assert args.compiled is not None


def test_no_collision_when_only_flag_used() -> None:
    """Bare `--agent codex` (no `agent:`) parses cleanly."""
    args = agentgrep.parse_args(["search", "--agent", "codex", "bliss"])
    assert args is not None
    assert isinstance(args, agentgrep.SearchArgs)
    assert args.compiled is None


def test_grep_query_with_no_text_pattern_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentgrep grep agent:codex`` (no text) errors with a steering message."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(["grep", "agent:codex"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "use 'agentgrep search'" in captured.err


def test_compiled_none_falls_through_to_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `compiled is None`, the engine takes its existing code path."""
    source = _make_source(agent="codex", path="/tmp/codex.jsonl")
    record = _make_record(agent="codex", text="bliss")

    monkeypatch.setattr(
        agentgrep,
        "discover_sources",
        lambda *args, **kwargs: [source],
    )
    monkeypatch.setattr(
        agentgrep,
        "plan_search_sources",
        lambda query, sources, backends, **kwargs: list(sources),
    )
    monkeypatch.setattr(
        agentgrep,
        "iter_source_records",
        lambda src: iter([record]),
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
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
