"""Tests for the :mod:`agentgrep.events` discriminated event union.

Covers the four invariants the event API promises consumers:

1. Each event type is independently constructable with its own
   ``type`` literal default.
2. Discriminator narrowing works through pydantic's
   :class:`pydantic.TypeAdapter` — passing a dict with the right
   ``type`` tag selects the right variant.
3. Embedded dataclass records survive a round-trip through the union
   model without conversion.
4. ``isinstance`` narrowing inside a consumer loop is supported
   (ty / mypy-friendly).

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import pathlib
import typing as t

import pydantic
import pytest

import agentgrep
from agentgrep import events


def _make_search_record(
    *,
    text: str = "match text",
    timestamp: str | None = "2026-05-22T12:00:00Z",
) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for event tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/demo.jsonl"),
        text=text,
        timestamp=timestamp,
    )


def _make_find_record() -> agentgrep.FindRecord:
    """Build a synthetic FindRecord for event tests."""
    return agentgrep.FindRecord(
        kind="find",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/demo.jsonl"),
        path_kind="session_file",
        metadata={"source_kind": "jsonl"},
    )


class ConstructionCase(t.NamedTuple):
    """Parametrized case for direct event construction."""

    test_id: str
    factory: t.Callable[
        [],
        events.SearchStarted
        | events.SourceStarted
        | events.RecordEmitted
        | events.SourceFinished
        | events.SearchFinished
        | events.FindStarted
        | events.FindRecordEmitted
        | events.FindFinished,
    ]
    expected_type: str


CONSTRUCTION_CASES: tuple[ConstructionCase, ...] = (
    ConstructionCase(
        test_id="search-started-default-type",
        factory=lambda: events.SearchStarted(source_count=3),
        expected_type="search_started",
    ),
    ConstructionCase(
        test_id="source-started-default-type",
        factory=lambda: events.SourceStarted(
            adapter_id="codex.sessions_jsonl.v1",
            index=1,
            total=3,
        ),
        expected_type="source_started",
    ),
    ConstructionCase(
        test_id="record-emitted-embeds-dataclass",
        factory=lambda: events.RecordEmitted(record=_make_search_record()),
        expected_type="record_emitted",
    ),
    ConstructionCase(
        test_id="source-finished-default-type",
        factory=lambda: events.SourceFinished(
            adapter_id="codex.sessions_jsonl.v1",
            records_seen=10,
            matches_seen=3,
        ),
        expected_type="source_finished",
    ),
    ConstructionCase(
        test_id="search-finished-default-type",
        factory=lambda: events.SearchFinished(
            match_count=3,
            elapsed_seconds=0.42,
        ),
        expected_type="search_finished",
    ),
    ConstructionCase(
        test_id="find-started-default-type",
        factory=lambda: events.FindStarted(source_count=5),
        expected_type="find_started",
    ),
    ConstructionCase(
        test_id="find-record-emitted-embeds-dataclass",
        factory=lambda: events.FindRecordEmitted(record=_make_find_record()),
        expected_type="find_record_emitted",
    ),
    ConstructionCase(
        test_id="find-finished-default-type",
        factory=lambda: events.FindFinished(
            match_count=5,
            elapsed_seconds=0.1,
        ),
        expected_type="find_finished",
    ),
)


@pytest.mark.parametrize(
    "case",
    CONSTRUCTION_CASES,
    ids=[c.test_id for c in CONSTRUCTION_CASES],
)
def test_event_construction_tags_type_literal(case: ConstructionCase) -> None:
    """Every event constructs with its discriminator default in place."""
    event = case.factory()
    assert event.type == case.expected_type


class DiscriminatorCase(t.NamedTuple):
    """Parametrized case for pydantic discriminator narrowing through TypeAdapter."""

    test_id: str
    payload: dict[str, t.Any]
    union: t.Any  # SearchEvent or FindEvent
    expected_class: type[events._BaseEvent]


DISCRIMINATOR_CASES: tuple[DiscriminatorCase, ...] = (
    DiscriminatorCase(
        test_id="search-started-routes-to-search-started",
        payload={"type": "search_started", "source_count": 3},
        union=events.SearchEvent,
        expected_class=events.SearchStarted,
    ),
    DiscriminatorCase(
        test_id="source-started-routes-to-source-started",
        payload={
            "type": "source_started",
            "adapter_id": "codex.sessions_jsonl.v1",
            "index": 1,
            "total": 3,
        },
        union=events.SearchEvent,
        expected_class=events.SourceStarted,
    ),
    DiscriminatorCase(
        test_id="search-finished-routes-to-search-finished",
        payload={"type": "search_finished", "match_count": 3, "elapsed_seconds": 0.42},
        union=events.SearchEvent,
        expected_class=events.SearchFinished,
    ),
    DiscriminatorCase(
        test_id="find-started-routes-to-find-started",
        payload={"type": "find_started", "source_count": 5},
        union=events.FindEvent,
        expected_class=events.FindStarted,
    ),
    DiscriminatorCase(
        test_id="find-finished-routes-to-find-finished",
        payload={"type": "find_finished", "match_count": 5, "elapsed_seconds": 0.1},
        union=events.FindEvent,
        expected_class=events.FindFinished,
    ),
)


@pytest.mark.parametrize(
    "case",
    DISCRIMINATOR_CASES,
    ids=[c.test_id for c in DISCRIMINATOR_CASES],
)
def test_event_discriminator_narrows_via_type_adapter(case: DiscriminatorCase) -> None:
    """TypeAdapter validates the payload to the correct event variant by type."""
    adapter: pydantic.TypeAdapter[t.Any] = pydantic.TypeAdapter(case.union)
    validated = adapter.validate_python(case.payload)
    assert isinstance(validated, case.expected_class)


def test_event_invalid_discriminator_rejected() -> None:
    """Unknown ``type`` values raise a discriminator validation error."""
    adapter: pydantic.TypeAdapter[t.Any] = pydantic.TypeAdapter(events.SearchEvent)
    with pytest.raises(pydantic.ValidationError):
        _ = adapter.validate_python({"type": "definitely_not_an_event"})


def test_record_emitted_embeds_dataclass_directly() -> None:
    """RecordEmitted.record is the SearchRecord dataclass, not a copy."""
    record = _make_search_record(text="bliss")
    event = events.RecordEmitted(record=record)
    # Identity check: pydantic stores the dataclass reference (arbitrary_types_allowed).
    assert event.record is record
    # And the consumer reads attributes directly:
    assert event.record.text == "bliss"
    assert event.record.path == pathlib.Path("/tmp/demo.jsonl")


def test_find_record_emitted_embeds_find_dataclass_directly() -> None:
    """FindRecordEmitted.record is the FindRecord dataclass."""
    record = _make_find_record()
    event = events.FindRecordEmitted(record=record)
    assert event.record is record
    assert event.record.kind == "find"


def test_event_isinstance_narrowing_inside_loop() -> None:
    """Isinstance against the union narrows to the concrete event type.

    Models the consumer pattern used by the CLI render path. The
    interpreter-level narrowing (not ty's) is what's exercised here —
    the test runs at runtime to confirm isinstance returns True for the
    right variant and False otherwise.
    """
    stream: list[events.SearchStarted | events.SourceStarted | events.RecordEmitted] = [
        events.SearchStarted(source_count=2),
        events.SourceStarted(adapter_id="codex.sessions_jsonl.v1", index=1, total=2),
        events.RecordEmitted(record=_make_search_record()),
    ]
    record_count = 0
    for event in stream:
        if isinstance(event, events.RecordEmitted):
            record_count += 1
            assert event.record.kind == "prompt"
    assert record_count == 1


def test_events_are_frozen() -> None:
    """Events can't be mutated after construction (safe for fan-out)."""
    event = events.SearchStarted(source_count=3)
    with pytest.raises(pydantic.ValidationError):
        event.source_count = 4  # type: ignore[misc]


def test_events_reject_extra_fields() -> None:
    """``extra='forbid'`` rejects payloads with stray keys."""
    adapter: pydantic.TypeAdapter[t.Any] = pydantic.TypeAdapter(events.SearchEvent)
    with pytest.raises(pydantic.ValidationError):
        _ = adapter.validate_python(
            {"type": "search_started", "source_count": 3, "unexpected_field": "boom"},
        )
