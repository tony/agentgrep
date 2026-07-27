"""Bounded request-local routing contracts for targeted search."""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

from agentgrep import BackendSelection, SearchQuery, run_search_result
from agentgrep._engine import routing
from agentgrep._engine.orchestration import source_matches_scope
from agentgrep._engine.planning import build_logical_search_plan
from agentgrep._engine.routing import build_targeted_routing_plan
from agentgrep.progress import SearchControl
from agentgrep.query import compile_query, default_registry, parse_query
from agentgrep.records import AgentName, SourceHandle


def _write_codex_history(
    home: pathlib.Path,
    entries: tuple[tuple[str, int, str], ...],
) -> None:
    """Write a compact Codex prompt-history source."""
    root = home / ".codex"
    root.mkdir(parents=True, exist_ok=True)
    (root / "history.jsonl").write_text(
        "".join(
            json.dumps({"session_id": session_id, "ts": timestamp, "text": text}) + "\n"
            for session_id, timestamp, text in entries
        ),
        encoding="utf-8",
    )


def _write_codex_session(
    home: pathlib.Path,
    session_id: str,
    *,
    timestamp: str,
    text: str,
) -> pathlib.Path:
    """Write and return one current-shape Codex transcript."""
    path = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "26"
        / f"rollout-2026-07-26T12-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": timestamp,
                            "cwd": "/work/example",
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                            "timestamp": timestamp,
                        },
                    },
                ),
            ),
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_claude_history(
    home: pathlib.Path,
    entries: tuple[tuple[str, int, str, str], ...],
) -> pathlib.Path:
    """Write Claude prompt evidence with project-origin metadata."""
    path = home / ".claude" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "sessionId": session_id,
                    "timestamp": timestamp,
                    "display": text,
                    "project": project,
                },
            )
            + "\n"
            for session_id, timestamp, text, project in entries
        ),
        encoding="utf-8",
    )
    return path


def _write_claude_session(
    home: pathlib.Path,
    session_id: str,
    *,
    project: str,
) -> pathlib.Path:
    """Write one resolvable Claude transcript locator."""
    path = home / ".claude" / "projects" / project.strip("/").replace("/", "-")
    path = path / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": project,
                "message": {"role": "user", "content": "routing-clue"},
            },
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _targeted_query(
    terms: tuple[str, ...],
    *,
    conversation_limit: int = 25,
) -> SearchQuery:
    """Build a conversation-only targeted query."""
    return SearchQuery(
        terms=terms,
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="targeted",
        conversation_limit=conversation_limit,
    )


def _no_backends() -> BackendSelection:
    """Return deterministic pure-Python backend selection."""
    return BackendSelection(find_tool=None, grep_tool=None, json_tool=None)


def _compiled_targeted_query(
    text: str,
    *,
    agent: str,
    conversation_limit: int = 1,
) -> SearchQuery:
    """Compile one query-language expression for targeted routing."""
    registry = default_registry()
    compiled = compile_query(parse_query(text, registry), registry)
    return SearchQuery(
        terms=compiled.text_terms,
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=t.cast("tuple[t.Any, ...]", (agent,)),
        limit=None,
        compiled=compiled,
        effort="targeted",
        conversation_limit=conversation_limit,
    )


def _history_source(
    history: pathlib.Path,
    *,
    agent: AgentName,
    store: str,
    adapter_id: str,
) -> SourceHandle:
    """Build one prompt-history source handle for a written history file."""
    return SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=history,
        path_kind="history_file",
        source_kind="jsonl",
        search_root=history.parent,
        mtime_ns=history.stat().st_mtime_ns,
    )


def _routing_candidate(
    source: SourceHandle,
    native_id: str,
) -> routing.ConversationCandidate:
    """Build one provider-owned candidate for focused resolver contracts."""
    return routing.ConversationCandidate(
        key=routing.ConversationKey(
            agent=source.agent,
            provider=source.adapter_id,
            native_id=native_id,
        ),
        prompt_source=source,
        evidence_timestamp="",
    )


class _UnreadableTranscriptCase(t.NamedTuple):
    """One prompt-history provider with transcript locator verification.

    Attributes
    ----------
    test_id : str
        Stable pytest case identifier.
    agent : Literal["codex", "claude"]
        Provider whose selected transcript is unreadable.
    """

    test_id: str
    agent: t.Literal["codex", "claude"]


_UNREADABLE_TRANSCRIPT_CASES = (
    _UnreadableTranscriptCase("codex", "codex"),
    _UnreadableTranscriptCase("claude", "claude"),
)


class _LaterRootBudgetCase(t.NamedTuple):
    """One provider whose locator walk exhausts its budget on a later root.

    Attributes
    ----------
    test_id : str
        Stable pytest case identifier.
    agent : Literal["codex", "claude"]
        Provider whose prompt-history roots the resolver walks.
    store : str
        Prompt-history store the candidate sources belong to.
    adapter_id : str
        Provider key that selects the resolver branch. Codex and Claude own
        independent copies of the pending-root slice, so a shared adapter id
        would stop exercising one of those implementations.
    entry_limit : int
        Locator entry budget that this provider's on-disk layout exhausts part
        way through the second root's walk.
    native_ids : tuple[str, str, str, str]
        Session ids of the resolved, missing, current, and unvisited
        candidates, in that order.
    """

    test_id: str
    agent: t.Literal["codex", "claude"]
    store: str
    adapter_id: str
    entry_limit: int
    native_ids: tuple[str, str, str, str]


_LATER_ROOT_BUDGET_CASES = (
    _LaterRootBudgetCase(
        test_id="codex",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        entry_limit=5,
        native_ids=(
            "00000000-0000-0000-0000-000000000181",
            "00000000-0000-0000-0000-000000000182",
            "00000000-0000-0000-0000-000000000183",
            "00000000-0000-0000-0000-000000000184",
        ),
    ),
    _LaterRootBudgetCase(
        test_id="claude",
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
        entry_limit=3,
        native_ids=(
            "00000000-0000-0000-0000-000000000185",
            "00000000-0000-0000-0000-000000000186",
            "00000000-0000-0000-0000-000000000187",
            "00000000-0000-0000-0000-000000000188",
        ),
    ),
)


def test_targeted_search_groups_then_caps_conversations(tmp_path: pathlib.Path) -> None:
    """Duplicate prompt evidence consumes one bounded conversation slot."""
    older = "00000000-0000-0000-0000-000000000101"
    newer = "00000000-0000-0000-0000-000000000102"
    _write_codex_history(
        tmp_path,
        (
            (older, 1_700_000_000, "routing-clue"),
            (newer, 1_800_000_000, "routing-clue"),
            (newer, 1_800_000_001, "routing-clue repeated"),
        ),
    )
    _write_codex_session(
        tmp_path,
        older,
        timestamp="2023-11-14T00:00:00Z",
        text="routing-clue older",
    )
    _write_codex_session(
        tmp_path,
        newer,
        timestamp="2027-01-15T00:00:00Z",
        text="routing-clue newer",
    )

    result = run_search_result(
        tmp_path,
        _targeted_query(("routing-clue",), conversation_limit=1),
        backends=_no_backends(),
    )

    assert [record.text for record in result.records] == ["routing-clue newer"]
    assert result.summary.coverage.conversations_eligible == 2
    assert result.summary.coverage.conversations_selected == 1
    assert result.summary.coverage.conversations_completed == 1
    assert result.summary.status.state == "approximate"
    assert result.summary.status.reason == "heuristic_candidate_selection"
    assert result.summary.completed_effort == "targeted"
    assert result.summary.outcome == "matches"


def test_codex_selected_ids_share_one_sessions_walk(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch traversal because no fixture exposes provider walk counts."""
    first = "00000000-0000-0000-0000-000000000151"
    second = "00000000-0000-0000-0000-000000000152"
    _write_codex_history(
        tmp_path,
        (
            (first, 1_700_000_000, "routing-clue"),
            (second, 1_800_000_000, "routing-clue"),
        ),
    )
    _write_codex_session(
        tmp_path,
        first,
        timestamp="2023-11-14T00:00:00Z",
        text="routing-clue first",
    )
    _write_codex_session(
        tmp_path,
        second,
        timestamp="2027-01-15T00:00:00Z",
        text="routing-clue second",
    )
    history = tmp_path / ".codex" / "history.jsonl"
    source = _history_source(
        history,
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
    )
    sessions_root = history.parent / "sessions"
    original_bounded_files = routing._bounded_files
    walk_count = 0

    def counted_bounded_files(
        root: pathlib.Path,
        *,
        control: SearchControl,
        entry_budget: routing._LocatorEntryBudget,
        max_depth: int | None = None,
    ) -> tuple[pathlib.Path, ...] | None:
        nonlocal walk_count
        if root == sessions_root:
            walk_count += 1
        return original_bounded_files(
            root,
            control=control,
            entry_budget=entry_budget,
            max_depth=max_depth,
        )

    monkeypatch.setattr(routing, "_bounded_files", counted_bounded_files)

    plan = build_targeted_routing_plan(
        _targeted_query(("routing-clue",), conversation_limit=2),
        [source],
        conversation_limit=2,
    )

    assert walk_count == 1
    assert len(plan.sources) == 2


@pytest.mark.parametrize(
    "case",
    _UNREADABLE_TRANSCRIPT_CASES,
    ids=[case.test_id for case in _UNREADABLE_TRANSCRIPT_CASES],
)
def test_unreadable_selected_transcript_keeps_other_resolution(
    tmp_path: pathlib.Path,
    case: _UnreadableTranscriptCase,
) -> None:
    """Invalid UTF-8 consumes one attempt without discarding valid resolutions."""
    unreadable = "00000000-0000-0000-0000-000000000157"
    readable = "00000000-0000-0000-0000-000000000158"
    if case.agent == "codex":
        _write_codex_history(
            tmp_path,
            (
                (readable, 1_700_000_000, "routing-clue"),
                (unreadable, 1_800_000_000, "routing-clue"),
            ),
        )
        unreadable_path = _write_codex_session(
            tmp_path,
            unreadable,
            timestamp="2027-01-15T00:00:00Z",
            text="routing-clue unreadable",
        )
        readable_path = _write_codex_session(
            tmp_path,
            readable,
            timestamp="2023-11-14T00:00:00Z",
            text="routing-clue readable",
        )
        history = tmp_path / ".codex" / "history.jsonl"
        store = "codex.history"
        adapter_id = "codex.history_jsonl.v1"
    else:
        history = _write_claude_history(
            tmp_path,
            (
                (
                    readable,
                    1_700_000_000_000,
                    "routing-clue",
                    "/work/example",
                ),
                (
                    unreadable,
                    1_800_000_000_000,
                    "routing-clue",
                    "/work/example",
                ),
            ),
        )
        unreadable_path = _write_claude_session(
            tmp_path,
            unreadable,
            project="/work/example",
        )
        readable_path = _write_claude_session(
            tmp_path,
            readable,
            project="/work/example",
        )
        store = "claude.history"
        adapter_id = "claude.history_jsonl.v1"
    unreadable_path.write_bytes(b"\xff\n")
    source = _history_source(
        history,
        agent=case.agent,
        store=store,
        adapter_id=adapter_id,
    )

    plan = build_targeted_routing_plan(
        _compiled_targeted_query(
            "routing-clue",
            agent=case.agent,
            conversation_limit=2,
        ),
        [source],
        conversation_limit=2,
    )

    assert [resolved.path for resolved in plan.sources] == [readable_path]
    assert {attempt.key.native_id: attempt.outcome for attempt in plan.attempts} == {
        unreadable: "unresolved",
        readable: "resolved",
    }
    assert [diagnostic.code for diagnostic in plan.diagnostics] == [
        "targeted_locator_unresolved",
    ]


def test_locator_entry_budget_reports_unresolved_coverage(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized locator tree cannot turn targeted work into an unbounded walk."""
    session_id = "00000000-0000-0000-0000-000000000159"
    _write_codex_history(
        tmp_path,
        ((session_id, 1_800_000_000, "routing-clue"),),
    )
    _write_codex_session(
        tmp_path,
        session_id,
        timestamp="2027-01-15T00:00:00Z",
        text="routing-clue response",
    )
    history = tmp_path / ".codex" / "history.jsonl"
    source = _history_source(
        history,
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
    )
    monkeypatch.setattr(
        "agentgrep._engine.routing._TARGETED_LOCATOR_ENTRY_LIMIT",
        1,
        raising=False,
    )

    plan = build_targeted_routing_plan(
        _targeted_query(("routing-clue",), conversation_limit=1),
        [source],
        conversation_limit=1,
    )

    assert plan.sources == ()
    assert [attempt.outcome for attempt in plan.attempts] == [
        "budget_exhausted",
    ]
    assert [diagnostic.code for diagnostic in plan.diagnostics] == [
        "targeted_locator_budget_exhausted",
    ]


@pytest.mark.parametrize(
    "case",
    _LATER_ROOT_BUDGET_CASES,
    ids=[case.test_id for case in _LATER_ROOT_BUDGET_CASES],
)
def test_later_root_budget_keeps_completed_root_outcomes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _LaterRootBudgetCase,
) -> None:
    """A later walk cannot erase a completed root's hit or miss."""
    resolved, missing, current, unvisited = case.native_ids
    homes = tuple(tmp_path / name for name in ("first", "second", "third"))
    ids_by_home = ((resolved, missing), (current,), (unvisited,))
    sources: list[SourceHandle] = []
    for home, native_ids in zip(homes, ids_by_home, strict=True):
        if case.agent == "codex":
            _write_codex_history(
                home,
                tuple((native_id, 1_800_000_000, "routing-clue") for native_id in native_ids),
            )
            history = home / ".codex" / "history.jsonl"
        else:
            history = _write_claude_history(
                home,
                tuple(
                    (native_id, 1_800_000_000_000, "routing-clue", "/work/example")
                    for native_id in native_ids
                ),
            )
        sources.append(
            _history_source(
                history,
                agent=case.agent,
                store=case.store,
                adapter_id=case.adapter_id,
            ),
        )

    def write_session(home: pathlib.Path, native_id: str, label: str) -> pathlib.Path:
        """Write the transcript one candidate's locator must resolve to."""
        if case.agent == "codex":
            return _write_codex_session(
                home,
                native_id,
                timestamp="2027-01-15T00:00:00Z",
                text=f"routing-clue {label}",
            )
        return _write_claude_session(home, native_id, project="/work/example")

    resolved_path = write_session(homes[0], resolved, "resolved")
    write_session(homes[1], current, "current")
    write_session(homes[2], unvisited, "unvisited")
    monkeypatch.setattr(routing, "_TARGETED_LOCATOR_ENTRY_LIMIT", case.entry_limit)
    candidates = tuple(
        _routing_candidate(source, native_id)
        for source, native_ids in zip(sources, ids_by_home, strict=True)
        for native_id in native_ids
    )

    attempts = routing._resolve_candidates(candidates, control=SearchControl())

    outcomes = {attempt.key.native_id: attempt.outcome for attempt in attempts}
    assert outcomes == {
        resolved: "resolved",
        missing: "unresolved",
        current: "budget_exhausted",
        unvisited: "budget_exhausted",
    }
    assert (
        next(
            attempt.source.path
            for attempt in attempts
            if attempt.key.native_id == resolved and attempt.source is not None
        )
        == resolved_path
    )


def test_targeted_routing_applies_invariant_metadata_before_the_bound(
    tmp_path: pathlib.Path,
) -> None:
    """A newer prompt from the wrong project cannot consume the only slot."""
    wanted = "00000000-0000-0000-0000-000000000161"
    other = "00000000-0000-0000-0000-000000000162"
    history = _write_claude_history(
        tmp_path,
        (
            (wanted, 1_700_000_000_000, "routing-clue", "/work/wanted"),
            (other, 1_800_000_000_000, "routing-clue", "/work/other"),
        ),
    )
    wanted_session = _write_claude_session(
        tmp_path,
        wanted,
        project="/work/wanted",
    )
    _write_claude_session(tmp_path, other, project="/work/other")
    source = _history_source(
        history,
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
    )

    plan = build_targeted_routing_plan(
        _compiled_targeted_query(
            "routing-clue cwd:/work/wanted",
            agent="claude",
        ),
        [source],
        conversation_limit=1,
    )

    assert [resolved.path for resolved in plan.sources] == [wanted_session]


def test_targeted_routing_accepts_positive_metadata_without_text(
    tmp_path: pathlib.Path,
) -> None:
    """Positive project metadata alone can select a bounded conversation."""
    wanted = "00000000-0000-0000-0000-000000000171"
    history = _write_claude_history(
        tmp_path,
        ((wanted, 1_700_000_000_000, "unrelated prompt", "/work/wanted"),),
    )
    wanted_session = _write_claude_session(
        tmp_path,
        wanted,
        project="/work/wanted",
    )
    source = _history_source(
        history,
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
    )

    plan = build_targeted_routing_plan(
        _compiled_targeted_query("cwd:/work/wanted", agent="claude"),
        [source],
        conversation_limit=1,
    )

    assert [resolved.path for resolved in plan.sources] == [wanted_session]
    assert plan.diagnostics == ()


def test_targeted_search_keeps_candidates_out_of_results(tmp_path: pathlib.Path) -> None:
    """Routing evidence cannot establish a final transcript match."""
    session_id = "00000000-0000-0000-0000-000000000201"
    _write_codex_history(
        tmp_path,
        ((session_id, 1_800_000_000, "routing-clue"),),
    )
    _write_codex_session(
        tmp_path,
        session_id,
        timestamp="2027-01-15T00:00:00Z",
        text="different transcript content",
    )

    result = run_search_result(
        tmp_path,
        _targeted_query(("routing-clue",)),
        backends=_no_backends(),
    )

    assert result.records == ()
    assert result.summary.coverage.conversations_selected == 1
    assert result.summary.coverage.conversations_completed == 1
    assert result.summary.outcome == "no_selected_conversation_match"


def test_targeted_result_limit_applies_after_prompt_transcript_merge(
    tmp_path: pathlib.Path,
) -> None:
    """A prompt hit cannot consume the cap before a newer routed transcript."""
    session_id = "00000000-0000-0000-0000-000000000251"
    _write_codex_history(
        tmp_path,
        ((session_id, 1_700_000_000, "routing-clue older prompt"),),
    )
    _write_codex_session(
        tmp_path,
        session_id,
        timestamp="2027-01-15T00:00:00Z",
        text="routing-clue newer response",
    )
    query = SearchQuery(
        terms=("routing-clue",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
        effort="targeted",
        conversation_limit=1,
    )

    result = run_search_result(
        tmp_path,
        query,
        backends=_no_backends(),
    )

    assert [record.text for record in result.records] == [
        "routing-clue newer response",
    ]
    assert result.summary.status.state == "approximate"
    assert result.summary.status.conditions == (
        "result_limit",
        "heuristic_candidate_selection",
    )


def test_targeted_search_does_not_fall_back_after_no_candidate(
    tmp_path: pathlib.Path,
) -> None:
    """An unrouted transcript stays unread instead of triggering a sweep."""
    session_id = "00000000-0000-0000-0000-000000000301"
    _write_codex_history(
        tmp_path,
        ((session_id, 1_800_000_000, "unrelated prompt"),),
    )
    _write_codex_session(
        tmp_path,
        session_id,
        timestamp="2027-01-15T00:00:00Z",
        text="routing-clue exists only in the transcript",
    )

    result = run_search_result(
        tmp_path,
        _targeted_query(("routing-clue",)),
        backends=_no_backends(),
    )

    assert result.records == ()
    assert result.summary.coverage.conversations_eligible == 0
    assert result.summary.coverage.conversations_selected == 0
    assert result.summary.coverage.conversations_completed == 0
    assert result.summary.outcome == "no_candidate_conversation"


def test_unresolved_candidate_consumes_the_conversation_limit(
    tmp_path: pathlib.Path,
) -> None:
    """A stale newest locator cannot backfill past the request-local cap."""
    stale = "00000000-0000-0000-0000-000000000401"
    available = "00000000-0000-0000-0000-000000000402"
    _write_codex_history(
        tmp_path,
        (
            (available, 1_700_000_000, "routing-clue"),
            (stale, 1_800_000_000, "routing-clue"),
        ),
    )
    _write_codex_session(
        tmp_path,
        available,
        timestamp="2023-11-14T00:00:00Z",
        text="routing-clue available",
    )

    result = run_search_result(
        tmp_path,
        _targeted_query(("routing-clue",), conversation_limit=1),
        backends=_no_backends(),
    )

    assert result.records == ()
    assert result.summary.coverage.conversations_eligible == 2
    assert result.summary.coverage.conversations_selected == 1
    assert result.summary.coverage.conversations_completed == 0
    assert result.summary.outcome == "no_selected_conversation_match"
    assert [diagnostic.code for diagnostic in result.summary.diagnostics] == [
        "targeted_locator_unresolved",
    ]


def test_prompt_evidence_failure_cannot_complete_targeted_effort(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch the adapter seam because valid prompt fixtures do not fail reads."""
    session_id = "00000000-0000-0000-0000-000000000451"
    _write_codex_history(
        tmp_path,
        ((session_id, 1_800_000_000, "routing-clue"),),
    )
    private_failure = "private evidence failure"

    def fail_evidence_read(*_args: object, **_kwargs: object) -> t.Never:
        raise OSError(private_failure)

    monkeypatch.setattr(
        "agentgrep._engine.routing.iter_source_records",
        fail_evidence_read,
    )

    result = run_search_result(
        tmp_path,
        _targeted_query(("routing-clue",)),
        backends=_no_backends(),
    )

    assert result.records == ()
    assert result.summary.status.state == "failed"
    assert result.summary.status.reason == "engine_failure"
    assert result.summary.completed_effort is None
    assert result.summary.outcome == "undetermined"
    assert [diagnostic.code for diagnostic in result.summary.diagnostics] == [
        "targeted_evidence_failure",
        "engine_failure",
    ]
    assert all(
        "private evidence failure" not in diagnostic.message
        for diagnostic in result.summary.diagnostics
    )


def test_targeted_conversation_limit_must_be_positive() -> None:
    """Reject a work bound that would make targeted effort unbounded or inert."""
    with pytest.raises(ValueError, match="conversation_limit must be greater than 0"):
        run_search_result(
            pathlib.Path(),
            _targeted_query(("routing-clue",), conversation_limit=0),
            backends=_no_backends(),
        )


def test_targeted_effort_rejects_prompt_only_scope() -> None:
    """Callers must make the targeted conversation result scope explicit."""
    query = SearchQuery(
        terms=("routing-clue",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="targeted",
    )

    with pytest.raises(
        ValueError,
        match="targeted effort requires conversation or all scope",
    ):
        build_logical_search_plan(query)
    with pytest.raises(
        ValueError,
        match="targeted effort requires conversation or all scope",
    ):
        source_matches_scope(
            SourceHandle(
                agent="codex",
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                path=pathlib.Path("session.jsonl"),
                path_kind="session_file",
                source_kind="jsonl",
                search_root=None,
                mtime_ns=0,
            ),
            "prompts",
            effort="targeted",
        )


def test_compiled_query_exposes_only_positive_routing_terms() -> None:
    """Negative clauses never become positive conversation clues."""
    registry = default_registry()

    mixed = compile_query(
        parse_query("keep OR NOT discard", registry),
        registry,
    )
    negative_only = compile_query(
        parse_query("NOT discard", registry),
        registry,
    )

    assert mixed.routing_terms == ("keep",)
    assert negative_only.routing_terms == ()
