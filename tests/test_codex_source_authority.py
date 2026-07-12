"""Codex rollout authority over derived state-index prompt copies."""

from __future__ import annotations

import collections.abc as cabc
import json
import os
import pathlib
import sqlite3
import typing as t

import pytest

import agentgrep
from agentgrep._engine import search as engine_search
from agentgrep.events import RecordEmitted, SearchEvent, SearchFinished, SourceFinished
from agentgrep.query import compile_query, default_registry, parse_query

TERM = "authority-regression"
STATE_MTIME_NS = 1_900_000_000_000_000_000
ROLLOUT_MTIME_NS = 1_800_000_000_000_000_000
UNIQUE_MTIME_NS = 1_700_000_000_000_000_000


class CodexSourceAuthorityCase(t.NamedTuple):
    """One selected-source and dedupe shape for Codex thread resolution."""

    test_id: str
    surface: t.Literal["sync", "async"]
    limit: int | None
    dedupe: bool
    state_only: bool
    rollout_exists: bool
    unique_rollout: bool
    preview: str | None
    expected_rows: tuple[tuple[str, str], ...]


class CodexAuthorityCancellationCase(t.NamedTuple):
    """One result cap whose async partial answer must retain state fallback."""

    test_id: str
    limit: int | None


class CodexFiniteAuthorityEvidenceCase(t.NamedTuple):
    """One finite cap that must retain pre-dedupe rollout identity evidence."""

    test_id: str
    limit: int


CODEX_SOURCE_AUTHORITY_CASES: tuple[CodexSourceAuthorityCase, ...] = (
    CodexSourceAuthorityCase(
        test_id="paired-unlimited-prefers-rollout",
        surface="sync",
        limit=None,
        dedupe=True,
        state_only=False,
        rollout_exists=True,
        unique_rollout=True,
        preview=None,
        expected_rows=(
            ("codex.sessions", f"{TERM} duplicate"),
            ("codex.sessions", f"{TERM} unique"),
        ),
    ),
    CodexSourceAuthorityCase(
        test_id="paired-finite-resolves-before-limit",
        surface="sync",
        limit=2,
        dedupe=True,
        state_only=False,
        rollout_exists=True,
        unique_rollout=True,
        preview=None,
        expected_rows=(
            ("codex.sessions", f"{TERM} duplicate"),
            ("codex.sessions", f"{TERM} unique"),
        ),
    ),
    CodexSourceAuthorityCase(
        test_id="paired-finite-async-parity",
        surface="async",
        limit=2,
        dedupe=True,
        state_only=False,
        rollout_exists=True,
        unique_rollout=True,
        preview=None,
        expected_rows=(
            ("codex.sessions", f"{TERM} duplicate"),
            ("codex.sessions", f"{TERM} unique"),
        ),
    ),
    CodexSourceAuthorityCase(
        test_id="explicit-state-selection-keeps-fallback",
        surface="sync",
        limit=None,
        dedupe=True,
        state_only=True,
        rollout_exists=True,
        unique_rollout=False,
        preview=None,
        expected_rows=(("codex.state_db", f"{TERM} duplicate"),),
    ),
    CodexSourceAuthorityCase(
        test_id="dedupe-disabled-keeps-physical-copies",
        surface="sync",
        limit=None,
        dedupe=False,
        state_only=False,
        rollout_exists=True,
        unique_rollout=False,
        preview=None,
        expected_rows=(
            ("codex.sessions", f"{TERM} duplicate"),
            ("codex.state_db", f"{TERM} duplicate"),
        ),
    ),
    CodexSourceAuthorityCase(
        test_id="missing-rollout-keeps-state-fallback",
        surface="sync",
        limit=None,
        dedupe=True,
        state_only=False,
        rollout_exists=False,
        unique_rollout=False,
        preview=None,
        expected_rows=(("codex.state_db", f"{TERM} duplicate"),),
    ),
    CodexSourceAuthorityCase(
        test_id="distinct-preview-remains-searchable",
        surface="sync",
        limit=None,
        dedupe=True,
        state_only=False,
        rollout_exists=True,
        unique_rollout=False,
        preview=f"{TERM} distinct preview",
        expected_rows=(
            ("codex.sessions", f"{TERM} duplicate"),
            ("codex.state_db", f"{TERM} distinct preview"),
        ),
    ),
)


CODEX_AUTHORITY_CANCELLATION_CASES: tuple[CodexAuthorityCancellationCase, ...] = (
    CodexAuthorityCancellationCase(
        test_id="unlimited-partial-answer",
        limit=None,
    ),
    CodexAuthorityCancellationCase(
        test_id="finite-partial-answer",
        limit=1,
    ),
)


CODEX_FINITE_AUTHORITY_EVIDENCE_CASES: tuple[CodexFiniteAuthorityEvidenceCase, ...] = (
    CodexFiniteAuthorityEvidenceCase(
        test_id="limit-one",
        limit=1,
    ),
    CodexFiniteAuthorityEvidenceCase(
        test_id="limit-two",
        limit=2,
    ),
)


def _write_rollout(
    home: pathlib.Path,
    *,
    filename: str,
    session_id: str,
    text: str,
    timestamp: str,
    mtime_ns: int,
) -> pathlib.Path:
    """Write one discoverable Codex rollout.

    Parameters
    ----------
    home : pathlib.Path
        Synthetic user home.
    filename : str
        Rollout basename.
    session_id : str
        Canonical Codex thread identifier.
    text : str
        User prompt text.
    timestamp : str
        Record timestamp.
    mtime_ns : int
        File timestamp controlling adversarial source order.

    Returns
    -------
    pathlib.Path
        Written rollout path.
    """
    path = home / ".codex" / "sessions" / "2026" / "07" / "11" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = (
        {"type": "session_meta", "payload": {"id": session_id}},
        {"type": "turn_context", "payload": {"model": "gpt-test"}},
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    )
    _ = path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _write_state_db(
    home: pathlib.Path,
    *,
    rollout_path: pathlib.Path,
    preview: str | None,
    thread_id: str = "thread-shared",
    title: str | None = "Authority fixture",
    model: str | None = None,
    cwd: str | None = None,
) -> None:
    """Write one Codex state-index row pointing at a rollout.

    Parameters
    ----------
    home : pathlib.Path
        Synthetic user home.
    rollout_path : pathlib.Path
        Indexed canonical rollout path, which need not exist.
    preview : str or None
        Optional preview distinct from the first prompt.
    thread_id : str
        Indexed thread identity.
    title : str or None
        State-only thread title.
    model : str or None
        State-only thread model.
    cwd : str or None
        State-only working directory.
    """
    path = home / ".codex" / "state_5.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "first_user_message TEXT, preview TEXT, title TEXT, "
            "updated_at_ms INTEGER, model TEXT, cwd TEXT)",
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                str(rollout_path),
                f"{TERM} duplicate",
                preview,
                title,
                1_900_000_000_000,
                model,
                cwd,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    os.utime(path, ns=(STATE_MTIME_NS, STATE_MTIME_NS))


def _query(
    *,
    limit: int | None,
    dedupe: bool,
    state_only: bool,
    term: str = TERM,
    expression: str | None = None,
) -> agentgrep.SearchQuery:
    """Build one conversation-scope authority query.

    Parameters
    ----------
    limit : int or None
        Result cap.
    dedupe : bool
        Whether logical duplicate removal is enabled.
    state_only : bool
        Whether the source-layer query selects only ``codex.state_db``.
    term : str
        Plain-text term used when no compiled expression is supplied.
    expression : str or None
        Optional compiled query expression.

    Returns
    -------
    agentgrep.SearchQuery
        Search request for the fixture term.
    """
    compiled = None
    terms = (term,)
    if expression is not None or state_only:
        registry = default_registry()
        query_text = expression if expression is not None else f"store:codex.state_db {term}"
        compiled = compile_query(
            parse_query(query_text, registry),
            registry,
        )
        terms = compiled.text_terms
    return agentgrep.SearchQuery(
        terms=terms,
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        dedupe=dedupe,
        compiled=compiled,
    )


class CodexIndexedMetadataCase(t.NamedTuple):
    """One index-only match that must remain searchable."""

    test_id: str
    query_input: str
    state_title: str | None
    state_model: str | None
    state_cwd: str | None
    expected_rows: tuple[tuple[str, str], ...]


CODEX_INDEXED_METADATA_CASES: tuple[CodexIndexedMetadataCase, ...] = (
    CodexIndexedMetadataCase(
        test_id="state-title-match-survives-selected-rollout",
        query_input="Authority fixture",
        state_title="Authority fixture",
        state_model=None,
        state_cwd=None,
        expected_rows=(("codex.state_db", f"{TERM} duplicate"),),
    ),
    CodexIndexedMetadataCase(
        test_id="state-model-match-survives-nonmatching-rollout",
        query_input="model:state-only-model",
        state_title=None,
        state_model="state-only-model",
        state_cwd=None,
        expected_rows=(("codex.state_db", f"{TERM} duplicate"),),
    ),
    CodexIndexedMetadataCase(
        test_id="state-cwd-match-survives-nonmatching-rollout",
        query_input="cwd:/state-only/cwd",
        state_title=None,
        state_model=None,
        state_cwd="/state-only/cwd",
        expected_rows=(("codex.state_db", f"{TERM} duplicate"),),
    ),
)


@pytest.mark.parametrize(
    CodexIndexedMetadataCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_INDEXED_METADATA_CASES],
)
def test_codex_index_only_metadata_match_is_not_shadowed(
    test_id: str,
    query_input: str,
    state_title: str | None,
    state_model: str | None,
    state_cwd: str | None,
    expected_rows: tuple[tuple[str, str], ...],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selected rollout cannot shadow state metadata it does not represent."""
    _ = test_id
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    rollout_path = _write_rollout(
        tmp_path,
        filename="rollout-duplicate.jsonl",
        session_id="thread-shared",
        text=f"{TERM} duplicate",
        timestamp="2026-07-11T12:00:00Z",
        mtime_ns=ROLLOUT_MTIME_NS,
    )
    _write_state_db(
        tmp_path,
        rollout_path=rollout_path,
        preview=None,
        title=state_title,
        model=state_model,
        cwd=state_cwd,
    )

    records = agentgrep.run_search_query(
        tmp_path,
        _query(
            limit=None,
            dedupe=True,
            state_only=False,
            term=query_input,
            expression=query_input if ":" in query_input else None,
        ),
        backends=agentgrep.BackendSelection(
            find_tool=None,
            grep_tool=None,
            json_tool=None,
        ),
    )

    actual_rows = tuple(sorted((record.store, record.text) for record in records))
    assert actual_rows == tuple(sorted(expected_rows))


class CodexCandidateIdentityCase(t.NamedTuple):
    """One path/thread identity combination for matched candidates."""

    test_id: str
    path_mode: t.Literal["exact", "stale"]
    rollout_thread_id: str
    state_thread_id: str
    expected_stores: tuple[str, ...]


CODEX_CANDIDATE_IDENTITY_CASES: tuple[CodexCandidateIdentityCase, ...] = (
    CodexCandidateIdentityCase(
        test_id="exact-path-survives-thread-id-mismatch",
        path_mode="exact",
        rollout_thread_id="rollout-thread",
        state_thread_id="state-thread",
        expected_stores=("codex.sessions",),
    ),
    CodexCandidateIdentityCase(
        test_id="moved-path-resolves-by-thread-id",
        path_mode="stale",
        rollout_thread_id="shared-thread",
        state_thread_id="shared-thread",
        expected_stores=("codex.sessions",),
    ),
    CodexCandidateIdentityCase(
        test_id="unrelated-path-and-thread-remain-distinct",
        path_mode="stale",
        rollout_thread_id="rollout-thread",
        state_thread_id="state-thread",
        expected_stores=("codex.sessions", "codex.state_db"),
    ),
)


@pytest.mark.parametrize(
    CodexCandidateIdentityCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_CANDIDATE_IDENTITY_CASES],
)
def test_codex_authority_uses_matching_candidate_identity(
    test_id: str,
    path_mode: t.Literal["exact", "stale"],
    rollout_thread_id: str,
    state_thread_id: str,
    expected_stores: tuple[str, ...],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact paths or thread IDs identify copies without filesystem probes."""
    _ = test_id
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    rollout_path = _write_rollout(
        tmp_path,
        filename="rollout-duplicate.jsonl",
        session_id=rollout_thread_id,
        text=f"{TERM} duplicate",
        timestamp="2026-07-11T12:00:00Z",
        mtime_ns=ROLLOUT_MTIME_NS,
    )
    indexed_path = (
        rollout_path if path_mode == "exact" else tmp_path / "old-home" / "rollout-duplicate.jsonl"
    )
    _write_state_db(
        tmp_path,
        rollout_path=indexed_path,
        preview=None,
        thread_id=state_thread_id,
    )

    records = agentgrep.run_search_query(
        tmp_path,
        _query(limit=None, dedupe=True, state_only=False),
        backends=agentgrep.BackendSelection(
            find_tool=None,
            grep_tool=None,
            json_tool=None,
        ),
    )

    assert tuple(sorted(record.store for record in records)) == tuple(
        sorted(expected_stores),
    )


@pytest.mark.parametrize(
    CodexFiniteAuthorityEvidenceCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_FINITE_AUTHORITY_EVIDENCE_CASES],
)
def test_finite_authority_retains_identity_from_deduped_rollout_candidate(
    test_id: str,
    limit: int,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic rollout dedupe cannot erase exact-path authority evidence."""
    _ = test_id
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    indexed_rollout = _write_rollout(
        tmp_path,
        filename="rollout-indexed.jsonl",
        session_id="rollout-thread",
        text=f"{TERM} duplicate",
        timestamp="2026-07-11T12:00:00Z",
        mtime_ns=ROLLOUT_MTIME_NS,
    )
    _ = _write_rollout(
        tmp_path,
        filename="rollout-newer-copy.jsonl",
        session_id="rollout-thread",
        text=f"{TERM} duplicate",
        timestamp="2026-07-12T12:00:00Z",
        mtime_ns=UNIQUE_MTIME_NS,
    )
    _write_state_db(
        tmp_path,
        rollout_path=indexed_rollout,
        preview=None,
        thread_id="state-thread",
    )

    records = agentgrep.run_search_query(
        tmp_path,
        _query(limit=limit, dedupe=True, state_only=False),
        backends=agentgrep.BackendSelection(
            find_tool=None,
            grep_tool=None,
            json_tool=None,
        ),
    )

    assert [(record.store, record.path.name) for record in records] == [
        ("codex.sessions", "rollout-newer-copy.jsonl"),
    ]


class CodexExplicitStateCase(t.NamedTuple):
    """One explicit state-source selector and execution entry point."""

    test_id: str
    selector: str
    entrypoint: t.Literal["planned", "legacy"]


CODEX_EXPLICIT_STATE_CASES: tuple[CodexExplicitStateCase, ...] = (
    CodexExplicitStateCase(
        test_id="planned-store",
        selector="store:codex.state_db",
        entrypoint="planned",
    ),
    CodexExplicitStateCase(
        test_id="planned-path",
        selector="path:state_5.sqlite",
        entrypoint="planned",
    ),
    CodexExplicitStateCase(
        test_id="planned-adapter",
        selector="adapter_id:codex.state_sqlite.v1",
        entrypoint="planned",
    ),
    CodexExplicitStateCase(
        test_id="legacy-store",
        selector="store:codex.state_db",
        entrypoint="legacy",
    ),
    CodexExplicitStateCase(
        test_id="legacy-path",
        selector="path:state_5.sqlite",
        entrypoint="legacy",
    ),
    CodexExplicitStateCase(
        test_id="legacy-adapter",
        selector="adapter_id:codex.state_sqlite.v1",
        entrypoint="legacy",
    ),
)


@pytest.mark.parametrize(
    CodexExplicitStateCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_EXPLICIT_STATE_CASES],
)
def test_explicit_state_source_selection_preserves_the_physical_row(
    test_id: str,
    selector: str,
    entrypoint: t.Literal["planned", "legacy"],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late and planned source predicates cannot inherit rollout authority."""
    _ = test_id
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    rollout_path = _write_rollout(
        tmp_path,
        filename="rollout-duplicate.jsonl",
        session_id="thread-shared",
        text=f"{TERM} duplicate",
        timestamp="2026-07-11T12:00:00Z",
        mtime_ns=ROLLOUT_MTIME_NS,
    )
    _write_state_db(tmp_path, rollout_path=rollout_path, preview=None)
    state_path = tmp_path / ".codex" / "state_5.sqlite"
    backends = agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None)
    query = _query(
        limit=None,
        dedupe=True,
        state_only=False,
        expression=f"{selector} {TERM}",
    )

    if entrypoint == "planned":
        records = agentgrep.run_search_query(tmp_path, query, backends=backends)
    else:
        sources = [
            agentgrep.SourceHandle(
                agent="codex",
                store="codex.state_db",
                adapter_id="codex.state_sqlite.v1",
                path=state_path,
                path_kind="sqlite_db",
                source_kind="sqlite",
                search_root=None,
                mtime_ns=STATE_MTIME_NS,
            ),
            agentgrep.SourceHandle(
                agent="codex",
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                path=rollout_path,
                path_kind="session_file",
                source_kind="jsonl",
                search_root=None,
                mtime_ns=ROLLOUT_MTIME_NS,
            ),
        ]
        records = agentgrep.collect_search_records(query, sources)

    assert [(record.store, record.text) for record in records] == [
        ("codex.state_db", f"{TERM} duplicate"),
    ]


@pytest.mark.parametrize(
    CodexSourceAuthorityCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_SOURCE_AUTHORITY_CASES],
)
async def test_codex_state_first_prompt_is_rollout_fallback(
    test_id: str,
    surface: t.Literal["sync", "async"],
    limit: int | None,
    dedupe: bool,
    state_only: bool,
    rollout_exists: bool,
    unique_rollout: bool,
    preview: str | None,
    expected_rows: tuple[tuple[str, str], ...],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State first prompts yield only when no selected rollout is authoritative."""
    _ = test_id
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    duplicate_path = (
        tmp_path / ".codex" / "sessions" / "2026" / "07" / "11" / "rollout-duplicate.jsonl"
    )
    if rollout_exists:
        _ = _write_rollout(
            tmp_path,
            filename=duplicate_path.name,
            session_id="thread-shared",
            text=f"{TERM} duplicate",
            timestamp="2026-07-11T12:00:00Z",
            mtime_ns=ROLLOUT_MTIME_NS,
        )
    if unique_rollout:
        _ = _write_rollout(
            tmp_path,
            filename="rollout-unique.jsonl",
            session_id="thread-unique",
            text=f"{TERM} unique",
            timestamp="2026-07-11T11:00:00Z",
            mtime_ns=UNIQUE_MTIME_NS,
        )
    _write_state_db(tmp_path, rollout_path=duplicate_path, preview=preview)
    query = _query(limit=limit, dedupe=dedupe, state_only=state_only)
    backends = agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None)

    if surface == "async":
        records = [
            event.record
            async for event in agentgrep.aiter_search_events(
                tmp_path,
                query,
                backends=backends,
            )
            if isinstance(event, RecordEmitted)
        ]
    else:
        records = agentgrep.run_search_query(tmp_path, query, backends=backends)

    actual_rows = tuple(sorted((record.store, record.text) for record in records))
    assert actual_rows == tuple(sorted(expected_rows))


@pytest.mark.parametrize(
    CodexAuthorityCancellationCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_AUTHORITY_CANCELLATION_CASES],
)
async def test_async_partial_answer_flushes_codex_state_fallback(
    test_id: str,
    limit: int | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async cancellation returns resolved partial records and a terminal event."""
    _ = test_id
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    rollout_path = _write_rollout(
        tmp_path,
        filename="rollout-duplicate.jsonl",
        session_id="thread-shared",
        text=f"{TERM} duplicate",
        timestamp="2026-07-11T12:00:00Z",
        mtime_ns=ROLLOUT_MTIME_NS,
    )
    _write_state_db(tmp_path, rollout_path=rollout_path, preview=None)
    control = agentgrep.SearchControl()
    original_iter_search_events = engine_search.iter_search_events

    def cancel_after_state_source(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        backends: agentgrep.BackendSelection | None = None,
        control: agentgrep.SearchControl | None = None,
        runtime: agentgrep.SearchRuntime | None = None,
    ) -> cabc.Iterator[SearchEvent]:
        """Request a partial answer after the state candidate is buffered."""
        assert control is not None
        for event in original_iter_search_events(
            home,
            query,
            backends=backends,
            control=control,
            runtime=runtime,
        ):
            yield event
            if isinstance(event, SourceFinished) and event.adapter_id == "codex.state_sqlite.v1":
                control.request_answer_now()

    monkeypatch.setattr(engine_search, "iter_search_events", cancel_after_state_source)
    output = [
        event
        async for event in agentgrep.aiter_search_events(
            tmp_path,
            _query(limit=limit, dedupe=True, state_only=False),
            backends=agentgrep.BackendSelection(
                find_tool=None,
                grep_tool=None,
                json_tool=None,
            ),
            control=control,
            max_queue_size=1,
        )
    ]

    records = [event.record for event in output if isinstance(event, RecordEmitted)]
    finished = [event for event in output if isinstance(event, SearchFinished)]
    assert [(record.store, record.text) for record in records] == [
        ("codex.state_db", f"{TERM} duplicate"),
    ]
    assert len(finished) == 1
    assert finished[0].match_count == 1
