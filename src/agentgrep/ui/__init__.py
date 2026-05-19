"""Textual TUI subpackage for agentgrep.

This subpackage holds the streaming Textual explorer ``run_ui`` and the
:func:`build_streaming_ui_app` factory. It is imported lazily by the
top-level ``agentgrep`` package — bare ``import agentgrep`` does not
load Textual. Anyone who imports ``agentgrep.ui`` (or calls
``agentgrep.run_ui()``) requires Textual to be installed.
"""

from __future__ import annotations

from agentgrep.ui.app import build_streaming_ui_app, run_ui

__all__ = ["build_streaming_ui_app", "run_ui"]
