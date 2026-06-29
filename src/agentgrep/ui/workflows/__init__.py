"""Pluggable interaction/query workflows for the explorer (ADR 0013).

A *workflow* is the behavior axis of the TUI: it decides what the primary input
does (search the engine vs. filter a loaded set), seeds the initial dispatch, and
contributes extra key bindings. It is a plain, Textual-light strategy object
driven by a :class:`~agentgrep.ui.layouts._base.LayoutScreen` host, so it is
unit-testable with a fake host and reusable across layouts.

The concrete workflows (``search``, ``browse``) import nothing heavy at module
scope beyond the protocol, so the registry can list them without importing
Textual (the layouts that present them carry that cost).
"""

from __future__ import annotations

import logging

from agentgrep.ui.workflows._protocol import Workflow, WorkflowHost
from agentgrep.ui.workflows.browse import BrowseWorkflow
from agentgrep.ui.workflows.deductive import DeductiveWorkflow
from agentgrep.ui.workflows.search import SearchWorkflow

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "BrowseWorkflow",
    "DeductiveWorkflow",
    "SearchWorkflow",
    "Workflow",
    "WorkflowHost",
]
