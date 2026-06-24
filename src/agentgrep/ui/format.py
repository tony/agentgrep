"""Pure text/number formatting helpers for the Textual explorer.

These helpers compose the statusline, the progress meter, and the
scanning-detail row from plain numbers and strings. They are dependency-free
(no Textual, no engine, no factory state) so they can be unit-tested and
doctested offline, and so the streaming app can import them without pulling
anything heavier.
"""

from __future__ import annotations


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
    r"""Compose the verbose scanning line for the toggleable detail row.

    The ``Ctrl-\`` row carries the per-source counts the compact
    statusline omits — phase, scanned/total sources, and in-source
    record/match counts — with the phase word capitalized to open the
    row as a sentence.

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
        The composed detail line; segments with unknown inputs are
        omitted.

    Examples
    --------
    >>> format_scanning_detail(
    ...     "scanning", 5662, 6748, "2176 records, 354 source matches",
    ... )
    'Scanning 5662/6748 sources | 2176 records, 354 source matches'
    >>> format_scanning_detail("prefiltering", None, None, "~/.codex/sessions/")
    'Prefiltering ~/.codex/sessions/'
    >>> format_scanning_detail("discovering", None, None, None)
    'Discovering'
    """
    heading = phase[:1].upper() + phase[1:]
    if current is not None and total is not None:
        line = f"{heading} {current}/{total} sources"
        if detail:
            line = f"{line} | {detail}"
        return line
    if detail:
        return f"{heading} {detail}"
    return heading


def searching_left_text(elapsed: float, *, narrow: bool) -> str:
    """Compose the left status text shown next to the spinner.

    The query itself is not repeated — the search input directly above
    the statusline already shows it. Narrow mode also drops the elapsed
    ticker (and its ellipsis) so the percent and match count keep their
    cells on small terminals.

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
    "format_scanning_detail",
    "phase_label",
    "render_progress_meter",
    "scroll_percent",
    "searching_left_text",
)
