"""Tests for AnsiColors accent and dim tiers.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import typing as t

import pytest

import agentgrep


class ColorMethodCase(t.NamedTuple):
    """Parametrized case for AnsiColors method output."""

    test_id: str
    method: str
    text: str
    enabled: bool
    expected: str


_CASES: tuple[ColorMethodCase, ...] = (
    ColorMethodCase(
        test_id="accent-enabled",
        method="accent",
        text="match",
        enabled=True,
        expected=f"{agentgrep.AnsiColors.ACCENT}match{agentgrep.AnsiColors.RESET}",
    ),
    ColorMethodCase(
        test_id="accent-disabled",
        method="accent",
        text="match",
        enabled=False,
        expected="match",
    ),
    ColorMethodCase(
        test_id="dim-enabled",
        method="dim",
        text="metadata",
        enabled=True,
        expected=f"{agentgrep.AnsiColors.DIM}metadata{agentgrep.AnsiColors.RESET}",
    ),
    ColorMethodCase(
        test_id="dim-disabled",
        method="dim",
        text="metadata",
        enabled=False,
        expected="metadata",
    ),
    ColorMethodCase(
        test_id="accent-empty-string",
        method="accent",
        text="",
        enabled=True,
        expected=f"{agentgrep.AnsiColors.ACCENT}{agentgrep.AnsiColors.RESET}",
    ),
    ColorMethodCase(
        test_id="dim-empty-string",
        method="dim",
        text="",
        enabled=True,
        expected=f"{agentgrep.AnsiColors.DIM}{agentgrep.AnsiColors.RESET}",
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=[c.test_id for c in _CASES])
def test_color_method_output(case: ColorMethodCase) -> None:
    """AnsiColors accent/dim methods produce expected ANSI output."""
    colors = agentgrep.AnsiColors(enabled=case.enabled)
    method = getattr(colors, case.method)
    assert method(case.text) == case.expected


def test_accent_class_var_is_256_color_amber() -> None:
    """ACCENT uses 256-color warm amber (color 179)."""
    assert agentgrep.AnsiColors.ACCENT == "\x1b[38;5;179m"


def test_dim_class_var_is_ansi_dim_attribute() -> None:
    """DIM uses the ANSI dim/faint attribute (SGR 2)."""
    assert agentgrep.AnsiColors.DIM == "\x1b[2m"


def test_for_stream_produces_working_accent() -> None:
    """AnsiColors.for_stream builds an instance whose accent() works."""
    import io

    stream = io.StringIO()
    colors = agentgrep.AnsiColors.for_stream("never", stream)
    assert colors.accent("hello") == "hello"
