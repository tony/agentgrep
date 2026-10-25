"""Grok memory stores: discovery contracts."""

from __future__ import annotations

import pathlib

import pytest

from agentgrep.adapters._generic import parse_text_store_file
from agentgrep.discovery import discover_grok_sources
from agentgrep.records import BackendSelection

from .conftest import fixture_path

_NO_BACKENDS = BackendSelection(find_tool=None, grep_tool=None, json_tool=None)


def test_memory_v2_notes_are_read_as_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Markdown note under ``memory-v2/`` is a source; its SQLite indexes are not.

    Grok Build writes its memory there and no longer to ``memory/``.
    """
    monkeypatch.delenv("GROK_HOME", raising=False)
    root = tmp_path / ".grok" / "memory-v2"
    note = fixture_path("grok.memory_v2", "note.md").read_text(encoding="utf-8")
    notes = {
        root / "global" / "MEMORY.md",
        root / "global" / "topics" / "testing.md",
        root / "workspaces" / "workspace" / "observations" / "_inbox" / "note.md",
        root / "workspaces" / "workspace" / "archive" / "dream_1" / "note.md",
    }
    for path in notes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note, encoding="utf-8")
    (root / "global" / "index.sqlite").write_bytes(b"")

    sources = [
        source
        for source in discover_grok_sources(tmp_path, _NO_BACKENDS, include_non_default=True)
        if source.store == "grok.memory_v2"
    ]
    assert {source.path for source in sources} == notes
    records = [record for source in sources for record in parse_text_store_file(source)]
    assert all("redacted placeholder command" in record.text for record in records)
