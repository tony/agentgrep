"""Tests for the ``--ui`` overlay across grep / find / fuzzy subcommands.

The ``--ui`` flag is the ``tig``-shaped overlay: any subcommand can have
its query opened in the Textual explorer instead of the text/JSON
renderer. The TUI itself is launched by :func:`agentgrep.run_ui`; these
tests monkeypatch it and assert the dispatcher passes the right
:class:`agentgrep.SearchQuery` shape.
"""

from __future__ import annotations

import typing as t

import pytest

import agentgrep


def _capture_run_ui(monkeypatch: pytest.MonkeyPatch) -> list[agentgrep.SearchQuery]:
    """Replace ``agentgrep.run_ui`` with a recorder and return the captured calls."""
    captured: list[agentgrep.SearchQuery] = []

    def _record(
        home: object,
        query: agentgrep.SearchQuery,
        *,
        control: object,
        initial_search_text: str | None = None,
    ) -> None:
        del initial_search_text  # accepted for signature compat; not asserted here
        captured.append(query)

    monkeypatch.setattr(agentgrep, "run_ui", _record)
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

    monkeypatch.setattr(agentgrep, "run_search_query", _stub)


class OverlayCase(t.NamedTuple):
    """Parametrized case for the ``--ui`` overlay across subcommands."""

    test_id: str
    argv: tuple[str, ...]
    expected_search_type: agentgrep.SearchType
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
    OverlayCase(
        "fuzzy-ui-passes-query",
        ("fuzzy", "--ui", "design"),
        "all",
        ("design",),
    ),
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
    """``--ui`` on grep/find/fuzzy routes through agentgrep.run_ui with a query."""
    captured = _capture_run_ui(monkeypatch)
    _stub_run_search_query(monkeypatch)
    exit_code = agentgrep.main(list(case.argv))
    assert exit_code == 0
    assert len(captured) == 1
    query = captured[0]
    assert query.search_type == case.expected_search_type
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


def test_fuzzy_ui_with_case_sensitive_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fuzzy --ui --no-ignore-case forwards case-sensitivity to the TUI query."""
    captured = _capture_run_ui(monkeypatch)
    _stub_run_search_query(monkeypatch)
    exit_code = agentgrep.main(["fuzzy", "--ui", "--no-ignore-case", "FOO"])
    assert exit_code == 0
    assert captured[0].case_sensitive is True
