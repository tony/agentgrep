"""Compatibility tests for malformed terminal mouse reports."""

from __future__ import annotations

import pathlib
import re

import pytest
from textual import _xterm_parser as xterm_parser, events

from agentgrep.ui import _terminal_compat
from tests.test_agentgrep_tui import _build_empty_ui_app

pytestmark = pytest.mark.tui

_MALFORMED_REPORTS = ("\x1b[<32;NaN;NaNM", "\x1b[<35;NaN;NaNm")
_TEXTUAL_MOUSE_PATTERN = xterm_parser._re_mouse_event


def _parse_chunks(*chunks: str) -> list[object]:
    parser = xterm_parser.XTermParser()
    messages: list[object] = []
    for chunk in chunks:
        messages.extend(parser.feed(chunk))
    return messages


@pytest.mark.parametrize("report", _MALFORMED_REPORTS)
def test_terminal_compat_consumes_fragmented_malformed_sgr(
    monkeypatch: pytest.MonkeyPatch,
    report: str,
) -> None:
    """Every driver-read split is consumed without leaking literal keys."""
    monkeypatch.setattr(xterm_parser, "_re_mouse_event", _TEXTUAL_MOUSE_PATTERN)

    assert _terminal_compat.install_terminal_input_compat() is True
    assert _terminal_compat.install_terminal_input_compat() is False
    for split in range(1, len(report)):
        messages = _parse_chunks(report[:split], report[split:], "\x1b[A")
        keys = [message.key for message in messages if isinstance(message, events.Key)]
        assert keys == ["up"], split


def test_terminal_compat_preserves_mouse_keys_and_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility classifier leaves valid input under Textual's parser."""
    monkeypatch.setattr(xterm_parser, "_re_mouse_event", _TEXTUAL_MOUSE_PATTERN)
    _terminal_compat.install_terminal_input_compat()

    valid_mouse = _parse_chunks("\x1b[<0;5;5M")
    assert len(valid_mouse) == 1
    assert isinstance(valid_mouse[0], events.MouseDown)
    keys = _parse_chunks("\x1b[A", "\x1b[15~", "\x1b[97u")
    assert [message.key for message in keys if isinstance(message, events.Key)] == [
        "up",
        "f5",
        "a",
    ]
    pasted = _parse_chunks("\x1b[200~", "literal paste", "\x1b[201~")
    assert len(pasted) == 1
    assert isinstance(pasted[0], events.Paste)
    assert pasted[0].text == "literal paste"


def test_terminal_compat_is_idempotent_and_defers_to_future_textual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser that already consumes malformed SGR reports is left untouched."""
    future_pattern = re.compile(_terminal_compat._COMPAT_MOUSE_PATTERN)
    monkeypatch.setattr(xterm_parser, "_re_mouse_event", future_pattern)

    assert _terminal_compat.install_terminal_input_compat() is False
    assert xterm_parser._re_mouse_event is future_pattern


def test_ui_factory_installs_terminal_input_compat(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the TUI installs compatibility before a driver is created."""
    calls = 0

    def install() -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(_terminal_compat, "install_terminal_input_compat", install)

    _build_empty_ui_app(tmp_path, monkeypatch)

    assert calls == 1
