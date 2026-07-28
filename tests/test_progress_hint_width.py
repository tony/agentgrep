"""Width contracts for the CLI progress line's interrupt reminder.

The reminder is the only place answering early is advertised, so it has to
survive a narrow pane. These cases pin the width at which each spelling starts
to fit, pin that the abbreviated spelling is never reached while the full one
still fits, and pin that no rung ever returns a line wider than its budget in
display cells.
"""

from __future__ import annotations

import typing as t
import unicodedata

import pytest

from agentgrep._text import ANSI_CSI_RE, AnsiColors
from agentgrep.progress import ProgressSnapshot, format_search_progress_line

ANSWER_NOW_HINT = "[Press enter, answer now]"
SHORT_ANSWER_NOW_HINT = "[↵ answer]"
CANCEL_HINT = "[Ctrl-C to cancel]"
SHORT_CANCEL_HINT = "[^C cancel]"


def reference_cells(text: str) -> int:
    """Measure display cells without reusing the helper under test.

    Parameters
    ----------
    text : str
        Rendered progress line, possibly carrying ANSI CSI escapes.

    Returns
    -------
    int
        Terminal cells the line occupies.
    """
    total = 0
    for char in ANSI_CSI_RE.sub("", text):
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return total


SCANNING = ProgressSnapshot(
    query_label="tmux",
    phase="scanning",
    current=14932,
    total=19000,
    detail="812 records, 0 matches in rollout-2026-07-20.jsonl",
    matches=0,
    elapsed=149.6,
)
DISCOVERING = ProgressSnapshot(
    query_label="tmux",
    phase="discovering",
    current=None,
    total=None,
    detail="codex sessions",
    matches=0,
    elapsed=4.0,
)
LONG_QUERY = ProgressSnapshot(
    query_label="tmux new-session attach-session kill-serv",
    phase="scanning",
    current=14932,
    total=19000,
    detail="812 records, 0 matches in rollout-2026-07-20.jsonl",
    matches=0,
    elapsed=149.6,
)
WIDE_LABEL = ProgressSnapshot(
    query_label="日本語のセッション履歴を検索",
    phase="scanning",
    current=14932,
    total=19000,
    detail="会话记录测试文件.jsonl",
    matches=0,
    elapsed=149.6,
)


class HintWidthCase(t.NamedTuple):
    """One progress snapshot and the widths at which its reminder fits.

    Attributes
    ----------
    test_id : str
        Stable identifier for ``pytest.mark.parametrize`` ids.
    snapshot : ProgressSnapshot
        Progress counters the line is rendered from.
    short_hint : str
        Abbreviated reminder spelling this phase uses.
    long_hint : str
        Full reminder spelling this phase uses.
    short_floor : int
        Narrowest formatter width that still renders any reminder.
    long_floor : int
        Narrowest formatter width that renders the full reminder.
    line_at_short_floor : str
        Whole uncolored line expected at ``short_floor``.
    """

    test_id: str
    snapshot: ProgressSnapshot
    short_hint: str
    long_hint: str
    short_floor: int
    long_floor: int
    line_at_short_floor: str


HINT_WIDTH_CASES = (
    HintWidthCase(
        test_id="scanning",
        snapshot=SCANNING,
        short_hint=SHORT_ANSWER_NOW_HINT,
        long_hint=ANSWER_NOW_HINT,
        short_floor=58,
        long_floor=94,
        line_at_short_floor="Searching tmux | scanning 14932/19000 sources | [↵ answer]",
    ),
    HintWidthCase(
        test_id="discovering",
        snapshot=DISCOVERING,
        short_hint=SHORT_CANCEL_HINT,
        long_hint=CANCEL_HINT,
        short_floor=42,
        long_floor=68,
        line_at_short_floor="Searching tmux | discovering | [^C cancel]",
    ),
    HintWidthCase(
        test_id="long-query-label",
        snapshot=LONG_QUERY,
        short_hint=SHORT_ANSWER_NOW_HINT,
        long_hint=ANSWER_NOW_HINT,
        short_floor=95,
        long_floor=131,
        line_at_short_floor=(
            "Searching tmux new-session attach-session kill-serv"
            " | scanning 14932/19000 sources | [↵ answer]"
        ),
    ),
    HintWidthCase(
        test_id="wide-query-label",
        snapshot=WIDE_LABEL,
        short_hint=SHORT_ANSWER_NOW_HINT,
        long_hint=ANSWER_NOW_HINT,
        short_floor=82,
        long_floor=118,
        line_at_short_floor=(
            "Searching 日本語のセッション履歴を検索 | scanning 14932/19000 sources | [↵ answer]"
        ),
    ),
)

PLAIN = AnsiColors(enabled=False)


def _render(snapshot: ProgressSnapshot, width: int, *, hint: bool = True) -> str:
    """Render one uncolored progress line at ``width``."""
    return format_search_progress_line(
        snapshot,
        colors=PLAIN,
        answer_now_hint=hint,
        max_width=width,
    )


def _has_hint(line: str) -> bool:
    """Report whether ``line`` still advertises the interrupt affordance."""
    return "answer" in line or "cancel" in line


@pytest.mark.parametrize(
    "case",
    HINT_WIDTH_CASES,
    ids=[case.test_id for case in HINT_WIDTH_CASES],
)
def test_progress_hint_floor(case: HintWidthCase) -> None:
    """The reminder survives down to the pinned floor and vanishes below it."""
    at_floor = _render(case.snapshot, case.short_floor)
    assert _has_hint(at_floor)
    assert at_floor == case.line_at_short_floor
    assert reference_cells(at_floor) <= case.short_floor
    assert not _has_hint(_render(case.snapshot, case.short_floor - 1))


@pytest.mark.parametrize(
    "case",
    HINT_WIDTH_CASES,
    ids=[case.test_id for case in HINT_WIDTH_CASES],
)
def test_progress_hint_stays_abbreviated_only_while_narrow(case: HintWidthCase) -> None:
    """Cheap rungs are unreachable at every width the full reminder fits."""
    for width in range(case.long_floor, case.long_floor + 60):
        line = _render(case.snapshot, width)
        assert case.long_hint in line, width
        assert case.short_hint not in line, width
    for width in range(case.short_floor, case.long_floor):
        line = _render(case.snapshot, width)
        assert case.short_hint in line, width


@pytest.mark.parametrize(
    "case",
    HINT_WIDTH_CASES,
    ids=[case.test_id for case in HINT_WIDTH_CASES],
)
def test_progress_line_never_exceeds_its_budget_in_cells(case: HintWidthCase) -> None:
    """No rung may return a line wider than the terminal cells it was given.

    A wide query label or filename is the case a code-point budget gets wrong,
    and an overflowing status line wraps and strands a row on every frame.
    """
    over: list[tuple[int, int]] = []
    for hint in (False, True):
        for width in range(1, case.long_floor + 60):
            line = _render(case.snapshot, width, hint=hint)
            cells = reference_cells(line)
            if cells > width:
                over.append((width, cells))
    assert over == []


@pytest.mark.parametrize(
    "case",
    HINT_WIDTH_CASES,
    ids=[case.test_id for case in HINT_WIDTH_CASES],
)
def test_progress_line_without_hint_keeps_every_counter(case: HintWidthCase) -> None:
    """Dropping counters is reserved for keeping the reminder on screen."""
    full = _render(case.snapshot, 400, hint=False)
    for width in range(reference_cells(full), reference_cells(full) + 40):
        assert _render(case.snapshot, width, hint=False) == full
    for width in range(20, reference_cells(full)):
        line = _render(case.snapshot, width, hint=False)
        assert not _has_hint(line)
        assert reference_cells(line) <= width
