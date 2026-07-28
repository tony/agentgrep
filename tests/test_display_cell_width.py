"""Progress-line width helpers must budget in display cells, not code points.

A wide-character query label or filename made every frame of the CLI progress
line wider than the terminal, so each frame wrapped and the single-line erase
left the overflow row on screen.
"""

from __future__ import annotations

import typing as t
import unicodedata

import pytest

from agentgrep._text import ANSI_CSI_RE, _hard_truncate_ansi, _visible_width

# Written as escapes: a normalizing editor would silently turn the decomposed
# forms into precomposed ones, and the combining cases would stop testing
# anything.
ECOLE = "école"
ACUTE_A = "á"


def reference_cells(text: str) -> int:
    """Measure display cells independently of the implementation under test.

    Parameters
    ----------
    text : str
        Text that may carry ANSI CSI escape sequences.

    Returns
    -------
    int
        Terminal cells the text occupies.
    """
    stripped = ANSI_CSI_RE.sub("", text)
    total = 0
    for char in stripped:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return total


class WidthCase(t.NamedTuple):
    """One string whose display width is known by hand.

    Attributes
    ----------
    test_id : str
        Identifier used for the parametrized test node.
    text : str
        Input passed to the width helpers.
    cells : int
        Display cells the text occupies in a terminal.
    """

    test_id: str
    text: str
    cells: int


WIDTH_CASES = (
    WidthCase(test_id="ascii", text="plain ascii", cells=11),
    WidthCase(test_id="empty", text="", cells=0),
    WidthCase(test_id="cjk-filename", text="会话记录测试文件.jsonl", cells=22),
    WidthCase(test_id="japanese-query", text="日本語のセッション履歴を検索", cells=28),
    WidthCase(test_id="emoji", text="🚀🚀🚀 records", cells=14),
    WidthCase(test_id="combining", text=ECOLE, cells=5),
    WidthCase(test_id="fullwidth-digits", text="１２３", cells=6),  # noqa: RUF001
    WidthCase(test_id="ansi-wrapped-cjk", text="\x1b[36m会話\x1b[0m", cells=4),
    WidthCase(test_id="ansi-only", text="\x1b[36m\x1b[0m", cells=0),
    WidthCase(test_id="mixed", text="Searching 会话 | 3/19000 sources", cells=32),
)


@pytest.mark.parametrize("case", WIDTH_CASES, ids=[case.test_id for case in WIDTH_CASES])
def test_visible_width_counts_display_cells(case: WidthCase) -> None:
    """``_visible_width`` reports terminal cells, not code points."""
    assert _visible_width(case.text) == case.cells


def test_combining_case_is_decomposed() -> None:
    """The combining case only tests anything while it stays decomposed."""
    assert len(ECOLE) == 6
    assert unicodedata.combining(ECOLE[1])


TRUNCATION_TEXTS = (
    *(case.text for case in WIDTH_CASES),
    "\x1b[1mSearching\x1b[0m 日本語のセッション履歴を検索 \x1b[33m|\x1b[0m 0 matches",
    "会" * 40,
    ACUTE_A * 20,
    "🚀" * 15,
)


@pytest.mark.parametrize(
    "text",
    TRUNCATION_TEXTS,
    ids=[f"text-{index}" for index in range(len(TRUNCATION_TEXTS))],
)
def test_hard_truncate_ansi_never_exceeds_any_budget(text: str) -> None:
    """Sweep every budget: a two-cell character must never straddle the edge.

    A spot check passes on a naive implementation whenever the budget happens
    to land on a safe parity, so the assertion has to cover the whole range.
    """
    over_budgets: list[tuple[int, int]] = []
    for budget in range(1, reference_cells(text) + 11):
        truncated = _hard_truncate_ansi(text, budget)
        width = reference_cells(truncated)
        if width > budget:
            over_budgets.append((budget, width))
    assert over_budgets == []


@pytest.mark.parametrize(
    "text",
    TRUNCATION_TEXTS,
    ids=[f"text-{index}" for index in range(len(TRUNCATION_TEXTS))],
)
def test_hard_truncate_ansi_returns_text_that_already_fits(text: str) -> None:
    """Text within budget is returned untouched, escapes included."""
    assert _hard_truncate_ansi(text, max(1, reference_cells(text))) == text


def test_hard_truncate_ansi_resets_color_it_kept() -> None:
    """A truncation that keeps an escape closes it so color cannot leak."""
    truncated = _hard_truncate_ansi("\x1b[36m会话记录测试\x1b[0m", 5)
    assert truncated.startswith("\x1b[36m")
    assert truncated.endswith("\x1b[0m")
    assert reference_cells(truncated) <= 5


def test_hard_truncate_ansi_leaves_plain_text_without_a_reset() -> None:
    """Text with no escapes gains none."""
    assert _hard_truncate_ansi("abcdef", 4) == "abc…"


def test_hard_truncate_ansi_rejects_a_wide_character_at_the_edge() -> None:
    """The odd budget that exposes the accumulate-then-test bug."""
    assert _hard_truncate_ansi("会话记录", 4) == "会…"


@pytest.mark.parametrize("budget", [0, -1, -80])
def test_hard_truncate_ansi_empties_on_a_nonpositive_budget(budget: int) -> None:
    """No budget means no output."""
    assert _hard_truncate_ansi("会话记录", budget) == ""
