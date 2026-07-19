"""Shared constructors for mounted legacy Textual tests."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep as _agentgrep_module


def load_agentgrep_module() -> object:
    """Return the installed ``agentgrep`` package."""
    return _agentgrep_module


def _build_empty_ui_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Any:
    """Build a streaming UI app with the search worker stubbed to a no-op."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Isolate the search-history state file under tmp so tests never read or
    # trim the developer's real ~/.local/state/agentgrep/history.jsonl.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Keep persisted UI preferences away from the developer's real config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    return agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())


def _ui_record(agentgrep: t.Any, path: pathlib.Path, text: str, session_id: str) -> t.Any:
    """Build a minimal prompt :class:`SearchRecord` for detail-pane tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        text=text,
        session_id=session_id,
    )


def _static_content(widget: t.Any) -> t.Any:
    """Return Static content across Textual's supported inspection APIs."""
    content = getattr(widget, "content", None)
    return content if content is not None else widget._content


def _seed_records(
    agentgrep: t.Any,
    tmp_path: pathlib.Path,
    count: int,
) -> list[t.Any]:
    """Build ``count`` ``SearchRecord`` instances under ``tmp_path``."""
    return [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(count)
    ]


def _set_result_records(results: t.Any, records: t.Iterable[t.Any]) -> None:
    """Adopt one test-prepared result model."""
    prepared = list(records)
    results.set_records(
        prepared,
        record_ids={id(record) for record in prepared},
    )


def _filter_completed(app: t.Any, records: t.Iterable[t.Any], *, text: str = "") -> t.Any:
    """Build a generation-scoped filter completion for a mounted test app."""
    from agentgrep.ui.widgets import FilterCompleted

    prepared = list(records)
    return FilterCompleted(
        text=text,
        records=prepared,
        record_ids={id(record) for record in prepared},
        generation=app.screen._filter_generation,
        records_generation=app.screen._records_generation,
    )
