"""Headless event-stream engine for agentgrep.

This subpackage holds the typed-event producer that the CLI, TUI, and
MCP frontends all consume from. The producer is sync at this layer
(every consumer can wrap it cheaply via :func:`asyncio.to_thread` if
needed); the event vocabulary lives in :mod:`agentgrep.events`.

Public symbols are re-exported from :mod:`agentgrep` so callers reach
the engine via ``agentgrep.iter_search_events`` rather than the
underscore-prefixed module path. The underscore is a hint that the
*module layout* is internal — the *symbols* are stable.
"""

from __future__ import annotations

from agentgrep._engine.find import iter_find_events
from agentgrep._engine.search import iter_search_events

__all__ = ["iter_find_events", "iter_search_events"]
