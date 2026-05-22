"""Date literal parsing for the agentgrep query language.

Field values typed as :data:`agentgrep.query.registry.FieldKind` =
``"date"`` (today: ``timestamp:``, ``mtime:``) accept three syntaxes:

- **ISO**: ``2026-05-22``, ``2026-05``, ``2026``, ``2026-05-22T14:30:00``,
  ``2026-05-22T14:30:00Z``. Naive ISO inputs are interpreted as UTC.
- **Relative**: ``today``, ``yesterday``, ``tomorrow``, ``Nd ago``,
  ``Nw ago``, ``Nm ago``, ``Ny ago``, ``N(d|w|m|y) from now``. Units:
  d=days, w=weeks, m≈30 days, y≈365 days.
- **Open-ended**: the literal ``*`` for "no bound on this side"
  inside a range.

Every parsed value is a :class:`DateBound` carrying a UTC
:class:`datetime.datetime` and a hint about whether the input was
day-resolution (so the engine knows to expand a bare day into a
half-open ``[00:00, 24:00)`` range when used as an equality match).

Examples
--------
>>> from agentgrep.query.dates import parse_date_literal
>>> bound = parse_date_literal("2026-05-22")
>>> bound.day_resolution
True
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import typing as t

_NOW_OVERRIDE: t.Callable[[], dt.datetime] | None = None
"""Test hook for pinning the session's "now" timestamp.

Production code reads :func:`now` which honors this override when
set. Tests set it via :func:`set_now_override` to make relative
literals deterministic.
"""


def set_now_override(provider: t.Callable[[], dt.datetime] | None) -> None:
    """Install a test hook returning the session's "now" timestamp.

    Pass ``None`` to clear. Production code never sets this — it's a
    test seam, not a configuration knob.
    """
    global _NOW_OVERRIDE
    _NOW_OVERRIDE = provider


def now() -> dt.datetime:
    """Return the session's current UTC datetime.

    Honors :func:`set_now_override` when set so tests can pin the
    moment relative-date literals resolve against. The override
    convention follows the project's [[feedback-date-source]] memo:
    use the session-supplied date, not host-local time, when both
    matter.
    """
    if _NOW_OVERRIDE is not None:
        return _NOW_OVERRIDE()
    return dt.datetime.now(dt.UTC)


@dataclasses.dataclass(slots=True, frozen=True)
class DateBound:
    """A parsed date literal normalized to a UTC :class:`datetime.datetime`.

    ``day_resolution`` is ``True`` when the source literal carried
    only date precision (no hour / minute) — the compiler uses this
    to decide whether an equality match should expand to the full
    day or to the exact instant.
    """

    value: dt.datetime
    day_resolution: bool


class DateParseError(ValueError):
    """Raised when a date literal can't be parsed.

    Carries no position field (the parser layer already knows where
    in the source query the literal sits) — callers re-wrap with
    that context when surfacing to the user.
    """


_ISO_FULL_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[T ](?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$",
)
_ISO_YEAR_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")
_ISO_YEAR_RE = re.compile(r"^(?P<year>\d{4})$")
_RELATIVE_AGO_RE = re.compile(
    r"^(?P<count>\d+)\s*(?P<unit>[dwmy])(?:\s+ago)?$",
    flags=re.IGNORECASE,
)
_RELATIVE_FROM_NOW_RE = re.compile(
    r"^(?P<count>\d+)\s*(?P<unit>[dwmy])\s+from\s+now$",
    flags=re.IGNORECASE,
)

_UNIT_TO_DAYS: dict[str, int] = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_date_literal(literal: str) -> DateBound:
    """Parse one date literal into a :class:`DateBound`.

    Parameters
    ----------
    literal : str
        The raw value text from a ``field:value`` predicate (the
        parser strips quotes before passing).

    Returns
    -------
    DateBound
        Normalized UTC datetime plus a hint about whether the input
        was day-resolution.

    Raises
    ------
    DateParseError
        If the literal matches none of the supported forms.
    """
    text = literal.strip()
    lowered = text.lower()
    if lowered == "today":
        return _day_bound(now().date())
    if lowered == "yesterday":
        return _day_bound(now().date() - dt.timedelta(days=1))
    if lowered == "tomorrow":
        return _day_bound(now().date() + dt.timedelta(days=1))

    relative = _RELATIVE_AGO_RE.match(text)
    if relative is not None:
        count = int(relative.group("count"))
        unit = relative.group("unit").lower()
        delta = dt.timedelta(days=count * _UNIT_TO_DAYS[unit])
        return _day_bound((now() - delta).date())

    relative_future = _RELATIVE_FROM_NOW_RE.match(text)
    if relative_future is not None:
        count = int(relative_future.group("count"))
        unit = relative_future.group("unit").lower()
        delta = dt.timedelta(days=count * _UNIT_TO_DAYS[unit])
        return _day_bound((now() + delta).date())

    iso_year = _ISO_YEAR_RE.match(text)
    if iso_year is not None:
        moment = dt.datetime(
            int(iso_year.group("year")),
            1,
            1,
            tzinfo=dt.UTC,
        )
        return DateBound(value=moment, day_resolution=True)

    iso_ym = _ISO_YEAR_MONTH_RE.match(text)
    if iso_ym is not None:
        moment = dt.datetime(
            int(iso_ym.group("year")),
            int(iso_ym.group("month")),
            1,
            tzinfo=dt.UTC,
        )
        return DateBound(value=moment, day_resolution=True)

    iso_full = _ISO_FULL_RE.match(text)
    if iso_full is not None:
        return _parse_iso_full(iso_full)

    message = f"could not parse date literal {literal!r}"
    raise DateParseError(message)


def _day_bound(date_value: dt.date) -> DateBound:
    """Build a day-resolution :class:`DateBound` from a :class:`date`."""
    moment = dt.datetime(
        date_value.year,
        date_value.month,
        date_value.day,
        tzinfo=dt.UTC,
    )
    return DateBound(value=moment, day_resolution=True)


def _parse_iso_full(match: re.Match[str]) -> DateBound:
    """Build a :class:`DateBound` from a full ISO regex match.

    Handles the optional time + timezone offset. Naive timestamps
    are treated as UTC (the engine's default), matching the
    convention the rest of the codebase uses for record
    timestamps.
    """
    hour_raw = match.group("hour")
    has_time = hour_raw is not None
    hour = int(hour_raw) if hour_raw is not None else 0
    minute_raw = match.group("minute")
    minute = int(minute_raw) if minute_raw is not None else 0
    second_raw = match.group("second")
    second = int(second_raw) if second_raw is not None else 0
    tz_raw = match.group("tz")
    tzinfo: dt.tzinfo = dt.UTC
    if tz_raw and tz_raw.upper() != "Z":
        sign = 1 if tz_raw.startswith("+") else -1
        body = tz_raw[1:].replace(":", "")
        offset_hours = int(body[:2])
        offset_minutes = int(body[2:4]) if len(body) >= 4 else 0
        tzinfo = dt.timezone(
            sign * dt.timedelta(hours=offset_hours, minutes=offset_minutes),
        )
    moment = dt.datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        hour,
        minute,
        second,
        tzinfo=tzinfo,
    ).astimezone(dt.UTC)
    return DateBound(value=moment, day_resolution=not has_time)


@dataclasses.dataclass(slots=True, frozen=True)
class DateRange:
    """A half-open date interval ``[lo, hi)`` in UTC.

    The compiler uses this for both range literals (`field:[a TO b]`)
    and for the implicit expansion of bare-day equality matches
    (`timestamp:2026-05-22` becomes the range covering that calendar
    day in UTC).

    Either bound may be ``None`` for "unbounded on this side" —
    written as ``*`` in source (`timestamp:[* TO 2026-05-22]`).
    """

    lo: dt.datetime | None
    hi: dt.datetime | None
    inclusive_lo: bool = True
    inclusive_hi: bool = False


def equality_range(literal: str) -> DateRange:
    """Expand a bare-date equality (`field:2026-05-22`) into a range.

    A bare ISO day matches the half-open range covering that day in
    UTC. A bare ISO month matches the month. A literal with explicit
    time matches the exact instant (zero-width range — both bounds
    set to the same datetime, inclusive_hi=True).
    """
    if literal.strip() == "*":
        return DateRange(lo=None, hi=None, inclusive_lo=True, inclusive_hi=False)
    bound = parse_date_literal(literal)
    if not bound.day_resolution:
        return DateRange(
            lo=bound.value,
            hi=bound.value,
            inclusive_lo=True,
            inclusive_hi=True,
        )
    # Determine the natural granularity of the source literal so a
    # bare ``2026`` matches an entire year and ``2026-05`` matches
    # the month, not just the first day.
    text = literal.strip()
    if _ISO_YEAR_RE.match(text):
        upper = bound.value.replace(year=bound.value.year + 1)
    elif _ISO_YEAR_MONTH_RE.match(text):
        if bound.value.month == 12:
            upper = bound.value.replace(year=bound.value.year + 1, month=1)
        else:
            upper = bound.value.replace(month=bound.value.month + 1)
    else:
        upper = bound.value + dt.timedelta(days=1)
    return DateRange(
        lo=bound.value,
        hi=upper,
        inclusive_lo=True,
        inclusive_hi=False,
    )


def parse_range_bound(literal: str) -> dt.datetime | None:
    """Parse one bound of an explicit range, treating ``*`` as unbounded."""
    if literal.strip() == "*":
        return None
    return parse_date_literal(literal).value
