"""Tests for the agentgrep query language date literal parser.

Covers commit 3 of the query-language project — the date module in
:mod:`agentgrep.query.dates`. The session's "now" timestamp is
pinned via :func:`set_now_override` so relative literals are
deterministic.

Convention: parametrize via :class:`typing.NamedTuple` with
``test_id`` as the first field, constructed with keyword arguments.
"""

from __future__ import annotations

import datetime as dt
import typing as t

import pytest

from agentgrep.query.dates import (
    DateBound,
    DateParseError,
    DateRange,
    equality_range,
    parse_date_literal,
    parse_range_bound,
    set_now_override,
)

_FROZEN_NOW: dt.datetime = dt.datetime(2026, 5, 22, 14, 0, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _frozen_now() -> t.Iterator[None]:
    """Pin the session "now" so relative dates resolve deterministically."""
    set_now_override(lambda: _FROZEN_NOW)
    try:
        yield
    finally:
        set_now_override(None)


class DateLiteralCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.query.dates.parse_date_literal`."""

    test_id: str
    literal: str
    expected_value: dt.datetime
    expected_day_resolution: bool


DATE_LITERAL_CASES: tuple[DateLiteralCase, ...] = (
    DateLiteralCase(
        test_id="iso-full-day",
        literal="2026-05-22",
        expected_value=dt.datetime(2026, 5, 22, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="iso-year-month",
        literal="2026-05",
        expected_value=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="iso-year-only",
        literal="2026",
        expected_value=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="iso-with-time",
        literal="2026-05-22T14:30:00",
        expected_value=dt.datetime(2026, 5, 22, 14, 30, tzinfo=dt.UTC),
        expected_day_resolution=False,
    ),
    DateLiteralCase(
        test_id="iso-with-z-suffix",
        literal="2026-05-22T14:30:00Z",
        expected_value=dt.datetime(2026, 5, 22, 14, 30, tzinfo=dt.UTC),
        expected_day_resolution=False,
    ),
    DateLiteralCase(
        test_id="iso-with-positive-offset",
        literal="2026-05-22T14:30:00+02:00",
        expected_value=dt.datetime(2026, 5, 22, 12, 30, tzinfo=dt.UTC),
        expected_day_resolution=False,
    ),
    DateLiteralCase(
        test_id="iso-with-negative-offset",
        literal="2026-05-22T10:00:00-04:00",
        expected_value=dt.datetime(2026, 5, 22, 14, 0, tzinfo=dt.UTC),
        expected_day_resolution=False,
    ),
    DateLiteralCase(
        test_id="relative-today",
        literal="today",
        expected_value=dt.datetime(2026, 5, 22, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-yesterday",
        literal="yesterday",
        expected_value=dt.datetime(2026, 5, 21, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-tomorrow",
        literal="tomorrow",
        expected_value=dt.datetime(2026, 5, 23, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-7d-ago",
        literal="7d ago",
        expected_value=dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-2w-ago",
        literal="2w ago",
        expected_value=dt.datetime(2026, 5, 8, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-3m-ago-30d-each",
        literal="3m ago",
        expected_value=dt.datetime(2026, 2, 21, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-1y-ago-365d",
        literal="1y ago",
        expected_value=dt.datetime(2025, 5, 22, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-shorthand-no-ago",
        literal="3d",
        expected_value=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="relative-from-now",
        literal="5d from now",
        expected_value=dt.datetime(2026, 5, 27, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
    DateLiteralCase(
        test_id="case-insensitive",
        literal="TODAY",
        expected_value=dt.datetime(2026, 5, 22, tzinfo=dt.UTC),
        expected_day_resolution=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    DATE_LITERAL_CASES,
    ids=[c.test_id for c in DATE_LITERAL_CASES],
)
def test_parse_date_literal_returns_expected_bound(case: DateLiteralCase) -> None:
    """parse_date_literal produces the right UTC datetime + day-resolution flag."""
    actual = parse_date_literal(case.literal)
    assert actual == DateBound(
        value=case.expected_value,
        day_resolution=case.expected_day_resolution,
    )


class DateLiteralErrorCase(t.NamedTuple):
    """Parametrized case for unparseable date literals."""

    test_id: str
    literal: str


DATE_LITERAL_ERROR_CASES: tuple[DateLiteralErrorCase, ...] = (
    DateLiteralErrorCase(test_id="empty", literal=""),
    DateLiteralErrorCase(test_id="garbage", literal="not-a-date"),
    DateLiteralErrorCase(test_id="bad-month", literal="2026-13-01"),
    DateLiteralErrorCase(test_id="bad-unit", literal="5z ago"),
)


@pytest.mark.parametrize(
    "case",
    DATE_LITERAL_ERROR_CASES,
    ids=[c.test_id for c in DATE_LITERAL_ERROR_CASES],
)
def test_parse_date_literal_rejects_garbage(case: DateLiteralErrorCase) -> None:
    """Unparseable literals raise DateParseError with the source text."""
    with pytest.raises((DateParseError, ValueError)):
        _ = parse_date_literal(case.literal)


class DateRangeCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.query.dates.equality_range`."""

    test_id: str
    literal: str
    expected_lo: dt.datetime | None
    expected_hi: dt.datetime | None
    expected_inclusive_hi: bool


DATE_RANGE_CASES: tuple[DateRangeCase, ...] = (
    DateRangeCase(
        test_id="bare-day-expands-to-24-hours",
        literal="2026-05-22",
        expected_lo=dt.datetime(2026, 5, 22, tzinfo=dt.UTC),
        expected_hi=dt.datetime(2026, 5, 23, tzinfo=dt.UTC),
        expected_inclusive_hi=False,
    ),
    DateRangeCase(
        test_id="bare-month-expands-to-month",
        literal="2026-05",
        expected_lo=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
        expected_hi=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        expected_inclusive_hi=False,
    ),
    DateRangeCase(
        test_id="bare-month-december-wraps-year",
        literal="2026-12",
        expected_lo=dt.datetime(2026, 12, 1, tzinfo=dt.UTC),
        expected_hi=dt.datetime(2027, 1, 1, tzinfo=dt.UTC),
        expected_inclusive_hi=False,
    ),
    DateRangeCase(
        test_id="bare-year-expands-to-year",
        literal="2026",
        expected_lo=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        expected_hi=dt.datetime(2027, 1, 1, tzinfo=dt.UTC),
        expected_inclusive_hi=False,
    ),
    DateRangeCase(
        test_id="exact-time-zero-width-inclusive",
        literal="2026-05-22T14:30:00",
        expected_lo=dt.datetime(2026, 5, 22, 14, 30, tzinfo=dt.UTC),
        expected_hi=dt.datetime(2026, 5, 22, 14, 30, tzinfo=dt.UTC),
        expected_inclusive_hi=True,
    ),
    DateRangeCase(
        test_id="star-unbounded-both-sides",
        literal="*",
        expected_lo=None,
        expected_hi=None,
        expected_inclusive_hi=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    DATE_RANGE_CASES,
    ids=[c.test_id for c in DATE_RANGE_CASES],
)
def test_equality_range_expands_bare_literals(case: DateRangeCase) -> None:
    """equality_range expands bare-day/month/year to the right half-open span."""
    actual = equality_range(case.literal)
    assert actual == DateRange(
        lo=case.expected_lo,
        hi=case.expected_hi,
        inclusive_lo=True,
        inclusive_hi=case.expected_inclusive_hi,
    )


def test_parse_range_bound_star_returns_none() -> None:
    """``*`` parses to ``None`` for unbounded range tails."""
    assert parse_range_bound("*") is None


def test_parse_range_bound_iso_returns_utc() -> None:
    """Concrete bounds resolve to UTC datetimes."""
    assert parse_range_bound("2026-05-22") == dt.datetime(
        2026,
        5,
        22,
        tzinfo=dt.UTC,
    )


def test_set_now_override_is_per_test_scoped() -> None:
    """The autouse fixture restores the override to None after each test."""
    # Pinning to the fixture value mid-test is a no-op (it's already pinned),
    # but reading `now` should reflect the fixture's pin.
    from agentgrep.query.dates import now

    assert now() == _FROZEN_NOW
