"""Narrow compatibility fixes for terminal input parsed by Textual."""

from __future__ import annotations

import importlib
import re
import typing as t

__all__ = ["install_terminal_input_compat"]

_MALFORMED_SGR_REPORT = "\x1b[<32;NaN;NaNM"
_UP_KEY = "\x1b[A"
_COMPAT_MOUSE_PATTERN = "^" + re.escape("\x1b[") + r"(<[^\x1bMm]*[mM]|[-\d;]+[mM]|M...)\Z"


def _leaks_malformed_sgr(parser_type: type[t.Any]) -> bool:
    """Return whether ``parser_type`` reissues malformed mouse data as keys."""
    try:
        messages = parser_type().feed(_MALFORMED_SGR_REPORT + _UP_KEY)
        keys = [getattr(message, "key", None) for message in messages]
    except AttributeError, TypeError, ValueError:
        return False
    return any(key not in {None, "up"} for key in keys)


def install_terminal_input_compat() -> bool:
    """Consume malformed completed SGR mouse reports on affected Textual versions.

    Some terminals emit nonnumeric SGR coordinates during focus changes.
    Affected Textual parsers reissue those bytes as literal key events after
    their escape timeout. Broaden only the parser's completed mouse-report
    classifier; Textual still validates and discards the malformed payload.

    Returns
    -------
    bool
        ``True`` only when this call installed the compatibility pattern.
    """
    try:
        xterm_parser = importlib.import_module("textual._xterm_parser")
    except ModuleNotFoundError as error:
        if error.name not in {"textual", "textual._xterm_parser"}:
            raise
        return False

    parser_type = getattr(xterm_parser, "XTermParser", None)
    original = getattr(xterm_parser, "_re_mouse_event", None)
    if not isinstance(parser_type, type) or not isinstance(original, re.Pattern):
        return False
    if not _leaks_malformed_sgr(parser_type):
        return False

    replacement = re.compile(_COMPAT_MOUSE_PATTERN)
    xterm_module = t.cast("t.Any", xterm_parser)
    xterm_module._re_mouse_event = replacement
    if _leaks_malformed_sgr(parser_type):
        xterm_module._re_mouse_event = original
        return False
    return True
