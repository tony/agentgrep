"""Tests for format_relative_time.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import datetime
import typing as t

import pytest

from agentgrep.cli.render import format_relative_time


class RelativeTimeCase(t.NamedTuple):
    """Parametrized case for relative time formatting."""

    test_id: str
    timestamp: str
    now: datetime.datetime
    expected: str


_NOW = datetime.datetime(2026, 5, 24, 12, 0, 0, tzinfo=datetime.UTC)

_CASES: tuple[RelativeTimeCase, ...] = (
    RelativeTimeCase(
        test_id="just-now",
        timestamp="2026-05-24T12:00:00Z",
        now=_NOW,
        expected="now",
    ),
    RelativeTimeCase(
        test_id="thirty-seconds-ago",
        timestamp="2026-05-24T11:59:30Z",
        now=_NOW,
        expected="now",
    ),
    RelativeTimeCase(
        test_id="boundary-60s-is-1m",
        timestamp="2026-05-24T11:59:00Z",
        now=_NOW,
        expected="1m ago",
    ),
    RelativeTimeCase(
        test_id="fifteen-minutes",
        timestamp="2026-05-24T11:45:00Z",
        now=_NOW,
        expected="15m ago",
    ),
    RelativeTimeCase(
        test_id="twenty-three-hours",
        timestamp="2026-05-23T13:00:00Z",
        now=_NOW,
        expected="23h ago",
    ),
    RelativeTimeCase(
        test_id="six-days",
        timestamp="2026-05-18T12:00:00Z",
        now=_NOW,
        expected="6d ago",
    ),
    RelativeTimeCase(
        test_id="three-weeks",
        timestamp="2026-05-03T12:00:00Z",
        now=_NOW,
        expected="3w ago",
    ),
    RelativeTimeCase(
        test_id="eleven-months",
        timestamp="2025-06-24T12:00:00Z",
        now=_NOW,
        expected="11mo ago",
    ),
    RelativeTimeCase(
        test_id="two-years",
        timestamp="2024-05-24T12:00:00Z",
        now=_NOW,
        expected="2y ago",
    ),
    RelativeTimeCase(
        test_id="no-timezone-assumed-utc",
        timestamp="2026-05-24T11:45:00",
        now=_NOW,
        expected="15m ago",
    ),
    RelativeTimeCase(
        test_id="with-timezone-offset",
        timestamp="2026-05-24T07:45:00-04:00",
        now=_NOW,
        expected="15m ago",
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=[c.test_id for c in _CASES])
def test_format_relative_time(case: RelativeTimeCase) -> None:
    """format_relative_time produces expected relative time strings."""
    result = format_relative_time(case.timestamp, now=case.now)
    assert result == case.expected


def test_parse_failure_returns_verbatim() -> None:
    """Unparseable timestamps are returned unchanged."""
    assert format_relative_time("not-a-date") == "not-a-date"


def test_future_timestamp_returns_verbatim() -> None:
    """Timestamps in the future relative to now are returned unchanged."""
    future = "2026-05-25T12:00:00Z"
    result = format_relative_time(future, now=_NOW)
    assert result == future


def test_default_now_uses_utc() -> None:
    """When now is not provided, uses datetime.now(UTC)."""
    past = "2020-01-01T00:00:00Z"
    result = format_relative_time(past)
    assert result.endswith("y ago")
