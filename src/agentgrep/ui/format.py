"""Pure text/number formatting helpers for the Textual explorer.

These helpers compose the statusline, the progress meter, and the
scanning-detail row from plain numbers and strings. They are dependency-free
(no Textual, no engine, no factory state) so they can be unit-tested and
doctested offline, and so the streaming app can import them without pulling
anything heavier.
"""

from __future__ import annotations

import time


def scroll_percent(scroll_y: float, max_scroll_y: float) -> int:
    """Return an integer scroll percent clamped to ``[0, 100]``.

    Returns ``100`` when there is no scrollable region (everything fits)
    and ``0`` when scrolled to the very top. Mirrors tig's bottom-status
    convention where a fully visible view reads as ``100%``.
    """
    if max_scroll_y <= 0:
        return 100 if scroll_y <= 0 else 0
    return min(100, max(0, round((scroll_y / max_scroll_y) * 100)))


def format_elapsed_compact(seconds: float) -> str:
    """Format elapsed seconds as a compact ticker label.

    Every unit is truncated (floored) rather than rounded so a live
    1 Hz ticker never displays a second that has not fully elapsed.

    Parameters
    ----------
    seconds : float
        Elapsed wall-clock seconds. Negative values clamp to ``0``.

    Returns
    -------
    str
        ``"32s"`` under a minute, ``"7m 32s"`` under an hour, and
        ``"1h 02m"`` from an hour up (seconds dropped to bound width).

    Examples
    --------
    >>> format_elapsed_compact(0)
    '0s'
    >>> format_elapsed_compact(32.9)
    '32s'
    >>> format_elapsed_compact(60)
    '1m 0s'
    >>> format_elapsed_compact(452)
    '7m 32s'
    >>> format_elapsed_compact(3725)
    '1h 02m'
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


_PHASE_LABELS = {
    "starting": "Starting",
    "discovering": "Discovering",
    "discovered": "Discovered",
    # ``prefiltering`` is engine-internal jargon; users read "Filtering".
    "prefiltering": "Filtering",
    "planning": "Planning",
    "scanning": "Scanning",
}
"""Engine phase string -> user-facing present-continuous verb."""


_RELATIVE_UNITS = (
    (31536000, "y"),
    (604800, "w"),
    (86400, "d"),
    (3600, "h"),
    (60, "m"),
    (1, "s"),
)
"""Descending (seconds, single-letter-unit) pairs for relative-time labels."""


def format_relative_time(ts: float, now: float | None = None) -> str:
    """Format a unix timestamp as a compact ``"<n><unit> ago"`` label.

    Used by the search-history modal's left column. The largest whole unit
    wins (single-letter ``y/w/d/h/m/s``), matching the narrow relative-time
    style of a history picker. Future timestamps (clock skew) and the current
    second clamp to ``"just now"`` rather than emitting a negative or ``0s``.

    Parameters
    ----------
    ts : float
        The entry's unix timestamp (seconds).
    now : float or None
        The reference time; defaults to :func:`time.time` when omitted.

    Returns
    -------
    str
        e.g. ``"5m ago"``, ``"1d ago"``, ``"2w ago"``, or ``"just now"``.

    Examples
    --------
    >>> format_relative_time(0, 90)
    '1m ago'
    >>> format_relative_time(0, 86400)
    '1d ago'
    >>> format_relative_time(0, 14 * 86400)
    '2w ago'
    >>> format_relative_time(5, 5)
    'just now'
    >>> format_relative_time(100, 0)
    'just now'
    """
    current = time.time() if now is None else now
    diff = int(current - ts)
    if diff < 1:
        return "just now"
    for secs, unit in _RELATIVE_UNITS:
        if diff >= secs:
            return f"{diff // secs}{unit} ago"
    return "just now"


def phase_label(phase: str) -> str:
    """Map an engine phase string to a user-facing present-continuous verb.

    The engine reports an ordered phase vocabulary (``discovering`` ->
    ``prefiltering`` -> ``planning`` -> ``scanning``); the explorer shows
    these words next to its spinner so a stalled-looking dot always carries
    meaning. Most map to a title-cased form, but ``prefiltering`` is curated
    to ``Filtering`` so the user never sees the internal term. Unknown phases
    title-case rather than vanish.

    Parameters
    ----------
    phase : str
        The ``ProgressSnapshot.phase`` string.

    Returns
    -------
    str
        The user-facing verb, or ``""`` for an empty phase.

    Examples
    --------
    >>> phase_label("scanning")
    'Scanning'
    >>> phase_label("prefiltering")
    'Filtering'
    >>> phase_label("widgeting")
    'Widgeting'
    >>> phase_label("")
    ''
    """
    if not phase:
        return ""
    return _PHASE_LABELS.get(phase, phase[:1].upper() + phase[1:])


def render_progress_meter(fraction: float, width: int) -> str:
    """Render a ``▰▱`` progress bar of ``width`` cells.

    Parameters
    ----------
    fraction : float
        Completion in ``[0.0, 1.0]``; values outside the range clamp.
    width : int
        Bar width in cells. Non-positive widths render nothing.

    Returns
    -------
    str
        ``round(fraction * width)`` filled cells (``▰``) followed by
        empty cells (``▱``).

    Examples
    --------
    >>> render_progress_meter(0.52, 17)
    '▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱'
    >>> render_progress_meter(0.0, 5)
    '▱▱▱▱▱'
    >>> render_progress_meter(1.5, 5)
    '▰▰▰▰▰'
    >>> render_progress_meter(0.5, 0)
    ''
    """
    if width <= 0:
        return ""
    clamped = max(0.0, min(1.0, fraction))
    filled = min(width, round(clamped * width))
    return "▰" * filled + "▱" * (width - filled)


def format_progress_percent(fraction: float) -> str:
    """Format a completion fraction as an integer percent.

    Parameters
    ----------
    fraction : float
        Completion in ``[0.0, 1.0]``; values outside the range clamp.

    Returns
    -------
    str
        The rounded integer percent with a ``%`` suffix.

    Examples
    --------
    >>> format_progress_percent(0.524)
    '52%'
    >>> format_progress_percent(1.0)
    '100%'
    >>> format_progress_percent(-0.5)
    '0%'
    """
    clamped = max(0.0, min(1.0, fraction))
    return f"{round(clamped * 100)}%"


def format_scanning_detail(
    phase: str,
    current: int | None,
    total: int | None,
    detail: str | None,
) -> str:
    r"""Compose the verbose scanning detail for the toggleable ``Ctrl-\`` row.

    During scanning, the row carries per-source counts the compact header may
    omit: phase, source ordinal, and in-source record/match counts. Other phases
    keep their counters in ``detail`` so planner-group counts cannot masquerade
    as source ordinals. The phase word is capitalized to open the row.

    Parameters
    ----------
    phase : str
        Engine phase word (e.g. ``"scanning"``, ``"discovering"``).
    current : int or None
        Index of the source being scanned, when known.
    total : int or None
        Total number of sources, when known.
    detail : str or None
        In-source detail such as ``"2176 records, 354 source matches"``.

    Returns
    -------
    str
        The composed detail; the heading and the in-source detail are joined by
        a newline, and segments with unknown inputs are omitted.

    Examples
    --------
    >>> format_scanning_detail(
    ...     "scanning", 5662, 6748, "2176 records, 354 source matches",
    ... )
    'Scanning 5662/6748 sources\n2176 records, 354 source matches'
    >>> format_scanning_detail("prefiltering", None, None, "~/.codex/sessions/")
    'Prefiltering\n~/.codex/sessions/'
    >>> format_scanning_detail("discovering", None, None, None)
    'Discovering'
    >>> format_scanning_detail("planning", 7, 10, "candidate sources")
    'Planning\ncandidate sources'
    """
    heading = phase[:1].upper() + phase[1:]
    if phase == "scanning" and current is not None and total is not None:
        heading = f"{heading} {current}/{total} sources"
    if detail:
        return f"{heading}\n{detail}"
    return heading


def searching_left_text(elapsed: float, *, narrow: bool) -> str:
    """Compose the left status text shown next to the spinner.

    The query itself is not repeated — the search input directly above
    the statusline already shows it. Narrow mode also drops the elapsed ticker
    and its ellipsis so the remaining status facts keep their cells.

    Parameters
    ----------
    elapsed : float
        Wall-clock seconds since the search started.
    narrow : bool
        When ``True``, omit the elapsed suffix for small terminals.

    Returns
    -------
    str
        The left status segment, e.g. ``"Searching… (32s)"``.

    Examples
    --------
    >>> searching_left_text(32.4, narrow=False)
    'Searching… (32s)'
    >>> searching_left_text(32.4, narrow=True)
    'Searching'
    """
    if narrow:
        return "Searching"
    return f"Searching… ({format_elapsed_compact(elapsed)})"


__all__ = (
    "format_elapsed_compact",
    "format_progress_percent",
    "format_relative_time",
    "format_scanning_detail",
    "phase_label",
    "render_progress_meter",
    "scroll_percent",
    "searching_left_text",
)
