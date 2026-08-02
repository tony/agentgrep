"""MCP surface for unregistered-field-predicate diagnostics (agentgrep#153).

``tests/test_query_gate.py`` proves the detection itself; this module
proves the MCP ``search`` tool attaches the same non-fatal diagnostic to
its response, through the existing ``SearchToolResponse.diagnostics``
field, instead of raising a :class:`~fastmcp.exceptions.ToolError` or
staying silent.
"""

from __future__ import annotations

import pathlib

import pytest

from agentgrep.mcp.models import SearchRequestModel
from agentgrep.mcp.tools.search_tools import _search_async

pytestmark = pytest.mark.mcp


async def test_search_tool_warns_for_an_unregistered_field_predicate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered field-shaped term stays a literal search, with a warning."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))

    response = await _search_async(
        SearchRequestModel(
            terms=["bogusfield:xyz"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.request.terms == ["bogusfield:xyz"]
    (diagnostic,) = response.diagnostics
    assert diagnostic.code == "unregistered_field_predicate"
    assert diagnostic.severity == "warning"
    assert "bogusfield" in diagnostic.message


async def test_search_tool_registered_field_predicate_gets_no_diagnostic(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real, registered field predicate needs no diagnostic."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))

    response = await _search_async(
        SearchRequestModel(
            terms=["kind:prompt"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.diagnostics == []
