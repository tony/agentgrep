"""Tests for :func:`agentgrep.iter_search_events`.

These tests exercise the engine producer directly against fixture
filesystem layouts (small JSONL files). The legacy
:func:`agentgrep.run_search_query` keeps running its existing path —
this suite is the regression guard for the new generator and is
deliberately decoupled from it.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases,
NumPy docstrings, ty-strict.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep import events


def _write_codex_session(
    home: pathlib.Path,
    *,
    name: str,
    messages: list[tuple[str, str]],
) -> pathlib.Path:
    """Write a synthetic Codex session-jsonl file the engine can parse."""
    path = home / ".codex" / "sessions" / "2025" / "01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as out:
        for role, content in messages:
            event = {
                "type": "response_item",
                "payload": {"role": role, "content": content},
            }
            out.write(json.dumps(event))
            out.write("\n")
    return path


def _make_query(
    *,
    terms: tuple[str, ...] = ("bliss",),
    limit: int | None = None,
    dedupe: bool = True,
) -> agentgrep.SearchQuery:
    """Build a :class:`agentgrep.SearchQuery` with the helper defaults."""
    return agentgrep.SearchQuery(
        terms=terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        dedupe=dedupe,
    )


def test_iter_search_events_emits_started_and_finished_when_empty(
    tmp_path: pathlib.Path,
) -> None:
    """An empty home still produces a SearchStarted/SearchFinished envelope."""
    out = list(agentgrep.iter_search_events(tmp_path, _make_query()))
    assert isinstance(out[0], events.SearchStarted)
    assert isinstance(out[-1], events.SearchFinished)
    assert out[-1].match_count == 0
    # No sources discovered → no SourceStarted between the bookends.
    middle = [ev for ev in out if isinstance(ev, events.SourceStarted)]
    assert middle == []


def test_iter_search_events_yields_record_when_match_found(
    tmp_path: pathlib.Path,
) -> None:
    """A matching prompt produces a RecordEmitted between Started/Finished."""
    _ = _write_codex_session(
        tmp_path,
        name="match.jsonl",
        messages=[("user", "bliss is what we want")],
    )
    out = list(agentgrep.iter_search_events(tmp_path, _make_query()))
    record_events = [ev for ev in out if isinstance(ev, events.RecordEmitted)]
    assert len(record_events) == 1
    assert "bliss" in record_events[0].record.text


def test_iter_search_events_dedupes_within_session(tmp_path: pathlib.Path) -> None:
    """Two matching prompts in the same session collapse to one RecordEmitted."""
    _ = _write_codex_session(
        tmp_path,
        name="dup.jsonl",
        messages=[
            ("user", "bliss one"),
            ("user", "bliss one"),  # identical text → dedupe key collision
        ],
    )
    out = list(agentgrep.iter_search_events(tmp_path, _make_query(dedupe=True)))
    record_events = [ev for ev in out if isinstance(ev, events.RecordEmitted)]
    assert len(record_events) == 1


def test_iter_search_events_no_dedupe_emits_every_match(
    tmp_path: pathlib.Path,
) -> None:
    """With dedupe disabled, both copies of an identical line emit."""
    _ = _write_codex_session(
        tmp_path,
        name="dup.jsonl",
        messages=[
            ("user", "bliss one"),
            ("user", "bliss one"),
        ],
    )
    out = list(agentgrep.iter_search_events(tmp_path, _make_query(dedupe=False)))
    record_events = [ev for ev in out if isinstance(ev, events.RecordEmitted)]
    assert len(record_events) == 2


class LimitCase(t.NamedTuple):
    """Parametrized case for ``query.limit`` early-stop semantics."""

    test_id: str
    matches_in_source: int
    limit: int
    expected_record_count: int


LIMIT_CASES: tuple[LimitCase, ...] = (
    LimitCase("limit-matches-exactly", 5, 5, 5),
    LimitCase("limit-truncates-below-matches", 5, 2, 2),
    LimitCase("limit-above-matches-passes-all", 5, 10, 5),
    LimitCase("limit-one", 5, 1, 1),
)


@pytest.mark.parametrize(
    "case",
    LIMIT_CASES,
    ids=[c.test_id for c in LIMIT_CASES],
)
def test_iter_search_events_respects_limit(
    case: LimitCase,
    tmp_path: pathlib.Path,
) -> None:
    """The generator stops yielding RecordEmitted once ``limit`` is hit."""
    _ = _write_codex_session(
        tmp_path,
        name="many.jsonl",
        messages=[("user", f"bliss line {i}") for i in range(case.matches_in_source)],
    )
    query = _make_query(limit=case.limit, dedupe=False)
    out = list(agentgrep.iter_search_events(tmp_path, query))
    record_events = [ev for ev in out if isinstance(ev, events.RecordEmitted)]
    assert len(record_events) == case.expected_record_count


def test_iter_search_events_source_started_pairs_with_source_finished(
    tmp_path: pathlib.Path,
) -> None:
    """Every SourceStarted is matched by a SourceFinished from the same source."""
    _ = _write_codex_session(
        tmp_path,
        name="a.jsonl",
        messages=[("user", "bliss alpha")],
    )
    _ = _write_codex_session(
        tmp_path,
        name="b.jsonl",
        messages=[("user", "bliss beta")],
    )
    out = list(agentgrep.iter_search_events(tmp_path, _make_query()))
    starts = [ev for ev in out if isinstance(ev, events.SourceStarted)]
    finishes = [ev for ev in out if isinstance(ev, events.SourceFinished)]
    assert len(starts) == len(finishes)
    for start, finish in zip(starts, finishes, strict=True):
        assert start.adapter_id == finish.adapter_id


def test_iter_search_events_event_sequence_is_well_formed(
    tmp_path: pathlib.Path,
) -> None:
    """Documented event sequence is well-formed.

    Started → (SourceStarted, *RecordEmitted, SourceFinished)* → Finished.
    """
    _ = _write_codex_session(
        tmp_path,
        name="one.jsonl",
        messages=[("user", "bliss only")],
    )
    out = list(agentgrep.iter_search_events(tmp_path, _make_query()))
    types = [ev.type for ev in out]
    assert types[0] == "search_started"
    assert types[-1] == "search_finished"
    # The middle alternates source_started / record_emitted* / source_finished.
    middle = types[1:-1]
    assert middle == [
        "source_started",
        "record_emitted",
        "source_finished",
    ]


def test_iter_search_events_answer_now_breaks_early(
    tmp_path: pathlib.Path,
) -> None:
    """Setting ``control.request_answer_now()`` stops the stream at the next boundary."""
    _ = _write_codex_session(
        tmp_path,
        name="many.jsonl",
        messages=[("user", f"bliss row {i}") for i in range(20)],
    )
    control = agentgrep.SearchControl()
    control.request_answer_now()  # request before the iterator starts
    out = list(
        agentgrep.iter_search_events(
            tmp_path,
            _make_query(dedupe=False),
            control=control,
        ),
    )
    # The generator still emits the Started/Finished envelope so cleanup runs.
    assert isinstance(out[0], events.SearchStarted)
    assert isinstance(out[-1], events.SearchFinished)
    record_events = [ev for ev in out if isinstance(ev, events.RecordEmitted)]
    # No records should appear — the request-answer-now flag short-circuits
    # the per-source loop on entry.
    assert record_events == []


def test_iter_search_events_match_count_matches_emitted_records(
    tmp_path: pathlib.Path,
) -> None:
    """The terminal Finished event's match_count equals the count of RecordEmitted."""
    _ = _write_codex_session(
        tmp_path,
        name="three.jsonl",
        messages=[
            ("user", "bliss one"),
            ("user", "bliss two"),
            ("user", "bliss three"),
        ],
    )
    out = list(agentgrep.iter_search_events(tmp_path, _make_query(dedupe=False)))
    record_count = sum(1 for ev in out if isinstance(ev, events.RecordEmitted))
    finished = [ev for ev in out if isinstance(ev, events.SearchFinished)]
    assert len(finished) == 1
    assert finished[0].match_count == record_count


async def test_aiter_search_events_streams_through_bounded_async_queue(
    tmp_path: pathlib.Path,
) -> None:
    """Async event streaming preserves order while yielding to the event loop."""
    _ = _write_codex_session(
        tmp_path,
        name="async.jsonl",
        messages=[
            ("user", "bliss one"),
            ("user", "bliss two"),
        ],
    )
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _index in range(3):
            await asyncio.sleep(0)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    out: list[events.SearchEvent] = []
    async for event in agentgrep.aiter_search_events(
        tmp_path,
        _make_query(dedupe=False),
        max_queue_size=1,
    ):
        out.append(event)
        await asyncio.sleep(0)
    await tick_task

    assert [event.type for event in out] == [
        "search_started",
        "source_started",
        "record_emitted",
        "record_emitted",
        "source_finished",
        "search_finished",
    ]
    assert ticks == 3


async def test_aiter_search_events_closing_the_stream_requests_cancellation(
    tmp_path: pathlib.Path,
) -> None:
    """Closing a partially consumed stream stops the worker scan.

    This is the contract every consumer relies on to cancel a scan: the
    cancellation request lives in the stream's ``finally`` block, so nothing
    stops until the generator is finalized.
    """
    _ = _write_codex_session(
        tmp_path,
        name="close.jsonl",
        messages=[
            ("user", "bliss one"),
            ("user", "bliss two"),
        ],
    )
    control = agentgrep.SearchControl()
    stream = agentgrep.aiter_search_events(
        tmp_path,
        _make_query(dedupe=False),
        control=control,
        max_queue_size=1,
    )

    async with asyncio.timeout(5.0):
        first = await anext(stream)
        assert not control.answer_now_requested()
        await stream.aclose()

    assert first.type == "search_started"
    assert control.answer_now_requested()


async def test_aiter_search_events_finishes_when_control_is_already_requested(
    tmp_path: pathlib.Path,
) -> None:
    """Async event streaming terminates when an external control is already closed."""
    control = agentgrep.SearchControl()
    control.request_answer_now()

    async with asyncio.timeout(1.0):
        out = [
            event
            async for event in agentgrep.aiter_search_events(
                tmp_path,
                _make_query(),
                control=control,
                max_queue_size=1,
            )
        ]

    assert [event.type for event in out] == ["search_started", "search_finished"]
