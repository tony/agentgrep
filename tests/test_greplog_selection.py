"""Selectable plain-text contracts for the grep-log layout."""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Log

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui._context import UiContext
from agentgrep.ui.layouts.greplog import GrepLogLayout
from agentgrep.ui.workflows import SearchWorkflow

pytestmark = pytest.mark.tui


def _layout() -> GrepLogLayout:
    """Build the grep-log layout without mounting a Textual app."""
    query = SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )
    context = UiContext(
        home=pathlib.Path(),
        invoker=t.cast("t.Any", object()),
        query=query,
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
    )
    return GrepLogLayout(context, SearchWorkflow())


def _record(title: str, path: str) -> SearchRecord:
    """Build one compact grep-log record with predictable formatting."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions",
        path=pathlib.Path(path),
        title=title,
        text="",
    )


def _log(layout: GrepLogLayout) -> Log:
    """Return the log composed by ``layout``."""
    widget = next(widget for widget in layout.compose() if getattr(widget, "id", None) == "greplog")
    assert isinstance(widget, Log)
    widget.auto_scroll = False
    return widget


def test_greplog_exposes_multiline_text_selection() -> None:
    """Expose every selected log line through Textual's screen contract."""
    log = _log(_layout())
    log.write("alpha\nbeta")
    selection = Selection.from_offsets(Offset(0, 0), Offset(4, 1))

    assert log.get_selection(selection) == ("alpha\nbeta", "\n")


def test_greplog_chunks_keep_line_boundaries_and_cap() -> None:
    """Keep separate streamed chunks as separately selectable retained lines."""
    layout = _layout()
    log = _log(layout)
    log.max_lines = 2
    layout._log = log

    layout._write_chunk([_record("first", "one")])
    layout._write_chunk([_record("second", "two")])
    layout._write_chunk([_record("third", "three")])

    assert list(log.lines) == [
        "codex     prompt    second  two",
        "codex     prompt    third  three",
    ]
