"""Tests for the deductive narrowing workflow (ADR 0014, the behavior axis).

``DeductiveWorkflow`` drives a layout through the ``WorkflowHost`` surface, so its
policy — first submit fixes the haystack, later submits narrow in-memory, widen
pops, clear resets — is verified by the host-call sequence against a recording
host. One Pilot test proves the keys route end-to-end on the chat layout.
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl, StreamingRecordsBatch, StreamingSearchFinished
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui._context import UiContext
from agentgrep.ui.workflows.deductive import DeductiveWorkflow
from tests._agentgrep_tui_support import _build_empty_ui_app

pytestmark = pytest.mark.tui


def _query(*terms: str) -> SearchQuery:
    """Build a minimal :class:`SearchQuery` with ``terms``."""
    return SearchQuery(
        terms=terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


class _NoopInvoker:
    """A search seam that runs nothing (the workflow tests never reach it)."""

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        del query, control, emit


class _DeductiveHost:
    """Records the host calls a deductive workflow makes (the host surface)."""

    def __init__(self, query: SearchQuery) -> None:
        self._ctx = UiContext(
            home=pathlib.Path("/nonexistent"),
            invoker=_NoopInvoker(),
            query=query,
            control=SearchControl(),
            base_scope=query.scope,
        )
        self.calls: list[tuple[str, object]] = []

    @property
    def context(self) -> UiContext:
        return self._ctx

    def build_query(self, text: str) -> SearchQuery:
        self.calls.append(("build_query", text))
        return _query(*text.split())

    def run_search(self, query: SearchQuery) -> None:
        self.calls.append(("run_search", query.terms))

    def filter_loaded(self, text: str) -> None:
        self.calls.append(("filter_loaded", text))

    def reset_view(self) -> None:
        self.calls.append(("reset_view", None))

    def record_history(self, text: str) -> None:
        self.calls.append(("record_history", text))

    def request_cancel(self) -> None:
        self.calls.append(("request_cancel", None))

    def set_input_text(self, text: str) -> None:
        self.calls.append(("set_input_text", text))

    def update_breadcrumb(self, frames: cabc.Sequence[str]) -> None:
        self.calls.append(("update_breadcrumb", tuple(frames)))

    def kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, _ in self.calls)

    def payloads(self, kind: str) -> list[object]:
        return [payload for k, payload in self.calls if k == kind]


class AttachCase(t.NamedTuple):
    """A launch query and the attach host-call sequence + breadcrumb it yields."""

    test_id: str
    terms: tuple[str, ...]
    expected_kinds: tuple[str, ...]
    expected_crumb: tuple[str, ...]


ATTACH_CASES = (
    AttachCase("terms-seed-haystack", ("rust",), ("run_search", "update_breadcrumb"), ("rust",)),
    AttachCase("empty-goes-idle", (), ("reset_view", "update_breadcrumb"), ()),
)


@pytest.mark.parametrize("case", ATTACH_CASES, ids=lambda c: c.test_id)
def test_deductive_on_attach(case: AttachCase) -> None:
    """Attach seeds the haystack from a launch query, else goes idle."""
    host = _DeductiveHost(_query(*case.terms))
    DeductiveWorkflow().on_attach(host)
    assert host.kinds() == case.expected_kinds
    assert host.payloads("update_breadcrumb")[-1] == case.expected_crumb


def test_deductive_first_submit_runs_engine() -> None:
    """The first non-empty submit fixes the haystack with one engine search."""
    host = _DeductiveHost(_query())
    DeductiveWorkflow().on_query(host, "rust")
    assert host.kinds() == ("record_history", "build_query", "run_search", "update_breadcrumb")
    assert host.payloads("update_breadcrumb")[-1] == ("rust",)


def test_deductive_later_submit_filters_in_memory() -> None:
    """A later submit narrows the loaded set with a composed AND (no engine run)."""
    host = _DeductiveHost(_query())
    workflow = DeductiveWorkflow()
    workflow.on_query(host, "rust")  # first → run_search
    host.calls.clear()
    workflow.on_query(host, "anyhow")  # second → filter_loaded
    assert host.kinds() == (
        "record_history",
        "request_cancel",
        "filter_loaded",
        "update_breadcrumb",
    )
    # A single refinement is passed verbatim — NOT wrapped in parens, which the
    # query parser would read as a literal term matching nothing.
    assert host.payloads("filter_loaded") == ["anyhow"]
    assert host.payloads("update_breadcrumb")[-1] == ("rust", "anyhow")
    assert "run_search" not in host.kinds()


def test_deductive_widen_pops_and_reseeds() -> None:
    """Widen drops the top level, re-filters weaker, and re-seeds the prompt."""
    host = _DeductiveHost(_query())
    workflow = DeductiveWorkflow()
    workflow.on_query(host, "rust")
    workflow.on_query(host, "anyhow")
    workflow.on_query(host, "thiserror")
    host.calls.clear()
    handled = workflow.on_action(host, "widen")
    assert handled is True
    assert host.payloads("filter_loaded") == ["anyhow"]
    assert host.payloads("set_input_text") == ["anyhow"]
    assert host.payloads("update_breadcrumb")[-1] == ("rust", "anyhow")


def test_deductive_widen_at_base_is_noop() -> None:
    """Widen with only the base haystack is handled but changes nothing."""
    host = _DeductiveHost(_query())
    workflow = DeductiveWorkflow()
    workflow.on_query(host, "rust")
    host.calls.clear()
    handled = workflow.on_action(host, "widen")
    assert handled is True
    assert host.calls == []


def test_deductive_clear_resets() -> None:
    """Clear cancels, resets the view, empties the prompt and the breadcrumb."""
    host = _DeductiveHost(_query())
    workflow = DeductiveWorkflow()
    workflow.on_query(host, "rust")
    workflow.on_query(host, "anyhow")
    host.calls.clear()
    handled = workflow.on_action(host, "clear")
    assert handled is True
    assert host.kinds() == ("request_cancel", "reset_view", "set_input_text", "update_breadcrumb")
    assert host.payloads("set_input_text") == [""]
    assert host.payloads("update_breadcrumb")[-1] == ()


def test_deductive_empty_submit_resets() -> None:
    """An empty submit cancels, resets, and clears the breadcrumb."""
    host = _DeductiveHost(_query())
    workflow = DeductiveWorkflow()
    workflow.on_query(host, "rust")
    host.calls.clear()
    workflow.on_query(host, "")
    assert host.kinds() == ("request_cancel", "reset_view", "update_breadcrumb")
    assert host.payloads("update_breadcrumb")[-1] == ()


def test_deductive_unknown_action_returns_false() -> None:
    """An action the workflow does not own is left for the layout."""
    host = _DeductiveHost(_query())
    assert DeductiveWorkflow().on_action(host, "nope") is False
    assert host.calls == []


def test_deductive_metadata() -> None:
    """The workflow exposes a stable id, a summary, and its widen/clear keys."""
    assert DeductiveWorkflow.name == "deductive"
    assert DeductiveWorkflow.summary
    keys = {t.cast("t.Any", b).key for b in DeductiveWorkflow.BINDINGS}
    assert {"ctrl+up", "ctrl+l"} <= keys


def test_deductive_composed_filter_form() -> None:
    """A single refinement filters verbatim; two or more AND-group with parens."""
    host = _DeductiveHost(_query())
    workflow = DeductiveWorkflow()
    workflow.on_query(host, "rust")  # base haystack (run_search)
    workflow.on_query(host, "anyhow")  # single refinement
    assert host.payloads("filter_loaded")[-1] == "anyhow"
    workflow.on_query(host, "error")  # second refinement
    assert host.payloads("filter_loaded")[-1] == "(anyhow) AND (error)"


class _MatchInvoker:
    """A search seam that emits a fixed record set so narrowing can be exercised."""

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self._tmp_path = tmp_path
        self.runs = 0

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Emit three records (two containing 'anyhow') then finish."""
        del query
        self.runs += 1
        if control.answer_now_requested():
            return
        records = tuple(
            SearchRecord(
                kind="prompt",
                agent="codex",
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                path=self._tmp_path / f"r{idx}.jsonl",
                text=text,
            )
            for idx, text in enumerate(
                ["rust anyhow here", "rust plain error", "rust anyhow again"],
            )
        )
        emit(StreamingRecordsBatch(records=records, total=len(records)))
        emit(StreamingSearchFinished(outcome="complete", total=len(records), elapsed=0.01))


@pytest.mark.slow
async def test_deductive_narrow_filters_to_matching_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a single-word narrow actually filters the haystack to matches.

    The earlier fakes asserted the host-call *shape* (``filter_loaded`` got a
    string) but never ran the matcher, so a paren-wrapped single term that
    matched nothing slipped through. This runs the real engine seam + matcher.
    """
    from agentgrep.ui.layouts.chat import ChatLayout
    from agentgrep.ui.widgets.turns import MessageTurn, ResultTurn

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    invoker = _MatchInvoker(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        query = _query()
        ctx = UiContext(
            home=tmp_path,
            invoker=invoker,
            query=query,
            control=SearchControl(),
            base_scope=query.scope,
        )
        layout = ChatLayout(ctx, DeductiveWorkflow())
        await app.push_screen(layout)
        await pilot.pause()

        def result_turns() -> list[MessageTurn]:
            return [
                child
                for child in layout.query_one("#transcript").children
                if isinstance(child, MessageTurn) and isinstance(child.turn, ResultTurn)
            ]

        layout._search_input.value = "rust"  # first submit fixes the haystack
        await pilot.press("enter")
        await pilot.pause(0.3)
        haystack = len(result_turns())
        assert haystack == 3

        layout._search_input.value = "anyhow"  # single-word narrow
        await pilot.press("enter")
        await pilot.pause(0.3)
        narrowed = result_turns()[haystack:]
        assert len(narrowed) == 2  # would be 0 with the (anyhow)-literal bug
        assert all("anyhow" in t.cast(ResultTurn, turn.turn).record.text for turn in narrowed)

@pytest.mark.slow
async def test_deductive_keys_route_on_chat_layout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the chat layout, two submits then ctrl+up narrow then widen the path."""
    from agentgrep.ui.layouts.chat import ChatLayout

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = ChatLayout(app._ctx, DeductiveWorkflow())
        await app.push_screen(layout)
        await pilot.pause()
        layout._search_input.value = "rust"
        await pilot.press("enter")
        await pilot.pause()
        layout._search_input.value = "anyhow"
        await pilot.press("enter")
        await pilot.pause()
        assert layout._breadcrumb._frames == ("rust", "anyhow")
        await pilot.press("ctrl+up")  # widen
        await pilot.pause()
        assert layout._breadcrumb._frames == ("rust",)
        assert layout._search_input.value == "rust"
