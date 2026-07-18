"""Shared constructors for focused Textual export tests."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep


def _search_requested(text: str) -> object:
    """Build a ``SearchRequested`` message carrying ``text``."""
    from agentgrep.progress import SearchRequestedPayload
    from agentgrep.ui.widgets import SearchRequested

    return SearchRequested(payload=SearchRequestedPayload(text=text))


def _build_empty_ui_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Any:
    """Build an isolated streaming UI with a no-op search worker."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    return agentgrep.build_streaming_ui_app(
        home,
        query,
        control=agentgrep.SearchControl(),
    )
