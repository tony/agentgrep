"""Colored ``--help`` works on every supported argparse.

argparse builds the formatter as ``formatter_class(prog=...)`` and applies
color afterwards, and the hooks it owns (``_theme``, the constructor's
keywords) changed between 3.14 and 3.15. The CLI's own theme has to ride
alongside argparse's, not replace it.
"""

from __future__ import annotations

import typing as t

import pytest

from agentgrep import create_parser

if t.TYPE_CHECKING:
    from agentgrep.records import ColorMode

ESC = "\x1b["
EXAMPLES_HEADING = "\x1b[1;36msearch examples:\x1b[0m"


class HelpColorCase(t.NamedTuple):
    """One color mode and whether help output carries escape codes."""

    test_id: str
    color_mode: ColorMode
    expect_color: bool


HELP_COLOR_CASES = (
    HelpColorCase("always", "always", True),
    HelpColorCase("never", "never", False),
)


@pytest.mark.parametrize(
    list(HelpColorCase._fields),
    HELP_COLOR_CASES,
    ids=[case.test_id for case in HELP_COLOR_CASES],
)
def test_help_colors_examples_alongside_argparse(
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    color_mode: ColorMode,
    expect_color: bool,
) -> None:
    """Color the examples with the CLI theme and leave usage to argparse."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    help_text = create_parser(color_mode).parser.format_help()

    assert (EXAMPLES_HEADING in help_text) is expect_color
    usage_line = help_text.splitlines()[0]
    assert (ESC in usage_line) is expect_color
    if not expect_color:
        assert ESC not in help_text
        assert "search examples:" in help_text
