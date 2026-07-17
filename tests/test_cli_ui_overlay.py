"""Tests for the ``--ui`` overlay across grep / find subcommands.

The ``--ui`` flag is the ``tig``-shaped overlay: any subcommand can have
its query opened in the Textual explorer instead of the text/JSON
renderer. The TUI itself is launched by :func:`agentgrep.run_ui`; these
tests monkeypatch it and assert the dispatcher passes the right
:class:`agentgrep.SearchQuery` shape.

The dispatcher lives in :mod:`agentgrep.cli.render`, which binds ``run_ui``
and ``run_search_query`` into its own namespace at import time (ADR 0010).
Monkeypatches therefore target ``render``'s bindings, not the facade
re-exports, or the real Textual app would launch and block.
"""

from __future__ import annotations

import typing as t

import pytest

import agentgrep
from agentgrep.cli import render as _r_render


def _capture_run_ui(
    monkeypatch: pytest.MonkeyPatch,
    initial_texts: list[str | None] | None = None,
) -> list[agentgrep.SearchQuery]:
    """Replace the renderer's ``run_ui`` with a recorder; return captured calls."""
    captured: list[agentgrep.SearchQuery] = []

    def _record(
        home: object,
        query: agentgrep.SearchQuery,
        *,
        control: object,
        initial_search_text: str | None = None,
    ) -> None:
        captured.append(query)
        if initial_texts is not None:
            initial_texts.append(initial_search_text)

    monkeypatch.setattr(_r_render, "run_ui", _record)
    return captured


def _stub_run_search_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the search engine so overlay tests don't touch the filesystem."""

    def _stub(
        home: object,
        query: object,
        *,
        progress: object = None,
        control: object = None,
    ) -> list[agentgrep.SearchRecord]:
        return []

    monkeypatch.setattr(_r_render, "run_search_query", _stub)


class OverlayCase(t.NamedTuple):
    """Parametrized case for the ``--ui`` overlay across subcommands."""

    test_id: str
    argv: tuple[str, ...]
    expected_scope: agentgrep.SearchScope
    expected_terms: tuple[str, ...]


OVERLAY_CASES: tuple[OverlayCase, ...] = (
    OverlayCase("grep-ui-passes-pattern", ("grep", "--ui", "foo"), "prompts", ("foo",)),
    OverlayCase(
        "grep-ui-uppercase-flips-case",
        ("grep", "--ui", "FOO"),
        "prompts",
        ("FOO",),
    ),
    OverlayCase("find-ui-passes-pattern", ("find", "--ui", "codex"), "all", ("codex",)),
)


@pytest.mark.parametrize(
    "case",
    OVERLAY_CASES,
    ids=[c.test_id for c in OVERLAY_CASES],
)
def test_ui_overlay_dispatches_to_run_ui(
    case: OverlayCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--ui`` on grep/find routes through agentgrep.run_ui with a query."""
    captured = _capture_run_ui(monkeypatch)
    _stub_run_search_query(monkeypatch)
    exit_code = agentgrep.main(list(case.argv))
    assert exit_code == 0
    assert len(captured) == 1
    query = captured[0]
    assert query.scope == case.expected_scope
    assert query.terms == case.expected_terms


def test_grep_ui_overlay_preserves_dedupe_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grep --ui with --no-dedupe should disable engine dedup on the TUI query too."""
    captured = _capture_run_ui(monkeypatch)
    _stub_run_search_query(monkeypatch)
    exit_code = agentgrep.main(["grep", "--ui", "--no-dedupe", "foo"])
    assert exit_code == 0
    assert captured[0].dedupe is False


def test_find_ui_overlay_passes_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Find --ui --agent codex narrows the TUI query to codex sources."""
    captured = _capture_run_ui(monkeypatch)
    _stub_run_search_query(monkeypatch)
    exit_code = agentgrep.main(["find", "--ui", "--agent", "codex", "anything"])
    assert exit_code == 0
    assert captured[0].agents == ("codex",)


def test_search_ui_overlay_passes_raw_query_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI receives the expression users expect to see and edit."""
    initial_texts: list[str | None] = []
    _capture_run_ui(monkeypatch, initial_texts)
    assert agentgrep.main(["search", "--ui", "agent:codex"]) == 0
    assert initial_texts == ["agent:codex"]


def test_ui_overlay_reports_oversized_query_without_traceback() -> None:
    """A UI-only query budget error becomes a concise CLI diagnostic."""
    from agentgrep.ui._history import QUERY_TEXT_MAX_CHARS

    oversized = "x" * (QUERY_TEXT_MAX_CHARS + 1)
    with pytest.raises(SystemExit, match="launch query exceeds 4096 characters"):
        agentgrep.main(["search", "--ui", oversized])


def test_ui_overlay_preserves_unexpected_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An internal runtime defect is not mislabeled as an input diagnostic."""
    message = "sentinel runtime defect"

    def fail_run_ui(*args: object, **kwargs: object) -> t.NoReturn:
        del args, kwargs
        raise ValueError(message)

    monkeypatch.setattr(_r_render, "run_ui", fail_run_ui)
    with pytest.raises(ValueError, match=message):
        agentgrep.main(["search", "--ui", "bliss"])
