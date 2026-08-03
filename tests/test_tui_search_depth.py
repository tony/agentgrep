"""TUI contracts for stable launch-time search depth."""

from __future__ import annotations

import dataclasses
import pathlib
import types
import typing as t

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input

from agentgrep.progress import SearchControl, StreamingSearchFinished
from agentgrep.records import SearchEffort, SearchQuery, SearchScopeProvenance
from agentgrep.results import RunCoverage, build_search_summary, offered_depth_actions
from agentgrep.ui import registry, theme as ui_theme
from agentgrep.ui._context import UiContext
from agentgrep.ui._result_status import (
    format_depth_offer_lead,
    format_depth_offer_rows,
    format_empty_evidence,
    format_empty_outcome,
    format_next_action_hint,
    format_run_status,
)
from agentgrep.ui._seams import EngineSearchInvoker
from agentgrep.ui.app import build_streaming_ui_app
from agentgrep.ui.commands import resolve_command
from agentgrep.ui.widgets import DepthOffer
from agentgrep.ui.widgets.welcome import _DEPTH_OFFER_CURSOR_STYLE, depth_offer_content

pytestmark = pytest.mark.tui


def _summary_for_effort(
    effort: SearchEffort,
    *,
    selected: int = 0,
    scope_provenance: SearchScopeProvenance = "inferred",
) -> t.Any:
    query = SearchQuery(
        terms=("missing",),
        scope="prompts" if effort == "prompt" else "all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort=effort,
        conversation_limit=25 if effort == "targeted" else None,
        scope_provenance=scope_provenance,
    )
    return build_search_summary(
        query,
        effort=effort,
        coverage=RunCoverage(
            sources_discovered=selected,
            sources_eligible=selected,
            sources_planned=selected,
            sources_attempted=selected,
            sources_completed=selected,
            sources_bounded=0,
            sources_skipped=0,
            sources_unsupported=0,
            sources_failed=0,
            sources_cancelled=0,
            records_seen=0,
            matches_seen=0,
            conversations_eligible=selected,
            conversations_selected=selected,
            conversations_completed=selected,
        ),
        match_count=0,
        elapsed_seconds=0.1,
    )


def _idle_query() -> SearchQuery:
    """Build the launch plan of a cold session: prompt scope, no terms."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


@pytest.mark.slow
@pytest.mark.parametrize("layout_name", registry.layout_names())
async def test_typed_depth_starts_a_targeted_run_without_a_prior_offer(
    tmp_path: pathlib.Path,
    layout_name: str,
) -> None:
    """Reach targeted effort by typing ``depth:`` directly, skipping the offer panel.

    Unlike :func:`test_idle_canvas_depth_offer_starts_a_targeted_run`, no
    click or arrow-key selection is involved — the box's own text carries the
    request, through the same :func:`~agentgrep.query.build_query_from_input`
    every layout's search box already calls; no TUI-local wrapper is needed.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            tmp_path,
            _idle_query(),
            control=SearchControl(),
            layout=layout_name,
        ),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        assert layout._run_summary is None

        layout._search_input.load_query("depth:targeted needle")
        layout._search_input.focus()
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert layout.search_query.terms == ("needle",)
        summary = layout._run_summary
        assert summary is not None
        assert summary.requested_effort == "targeted"
        assert summary.request.scope == "all"

        # The typed token stays request-local: the next plain edit is back
        # at the launch policy, exactly like a slash-command escalation.
        rebuilt = layout.build_query("other")
        assert rebuilt.effort == "prompt"
        assert rebuilt.scope == "prompts"


async def test_idle_canvas_depth_offer_starts_a_targeted_run(
    tmp_path: pathlib.Path,
) -> None:
    """Reach targeted effort from a cold session with no prior search."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        assert layout._run_summary is None

        offer = layout.query_one("#empty-depth", DepthOffer)
        rows = format_depth_offer_rows(layout.pending_depth_actions())
        assert [action_id for action_id, _ in rows] == [
            "search.targeted",
            "search.exhaustive",
        ]
        # An inline scope predicate already reads conversations, so the offer
        # must retire mid-edit rather than keep claiming prompt-only coverage.
        layout._search_input.load_query("scope:all deploy")
        await pilot.pause()
        assert layout.pending_depth_actions() == ()

        layout._search_input.load_query("needle")
        await pilot.pause()

        # Row 0 of the panel is the lead sentence; the offered actions follow.
        await pilot.click(offer, offset=(2, 1))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        summary = layout._run_summary
        assert summary is not None
        assert summary.requested_effort == "targeted"
        assert summary.request.scope == "all"
        assert layout._search_input.value == "needle depth:targeted"
        # Starting the search hides the idle canvas; focus must land back on
        # the input or every following keystroke is silently discarded.
        assert app.focused is layout._search_input
        # The escalation stays request-local: the next plain edit is prompt
        # effort again at the launch scope.
        assert layout.build_query("needle").effort == "prompt"


async def test_depth_offer_click_replaces_an_already_typed_directive(
    tmp_path: pathlib.Path,
) -> None:
    """Selecting a further rung replaces, rather than doubles, the box's own directive."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()

        layout._search_input.load_query("needle depth:targeted")
        await pilot.pause()
        rows = format_depth_offer_rows(layout.pending_depth_actions())
        assert [action_id for action_id, _ in rows] == ["search.exhaustive"]

        offer = layout.query_one("#empty-depth", DepthOffer)
        await pilot.click(offer, offset=(2, 1))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        summary = layout._run_summary
        assert summary is not None
        assert summary.requested_effort == "exhaustive"
        assert layout._search_input.value == "needle depth:exhaustive"


class _StubInput:
    """Primary-input stand-in exposing only the value a layout reads off it.

    Attributes
    ----------
    value : str
        Text the layout sees in its primary input.
    """

    def __init__(self, value: str) -> None:
        self.value = value


def _escalation_layout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    layout_name: str,
    input_text: str,
) -> tuple[t.Any, list[str], list[SearchQuery]]:
    """Build one layout off the message pump, recording its lifecycle calls.

    Escalation is a request-lifecycle contract, not a rendering one, so it is
    provable without mounting an app.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Isolated home for the layout context.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to record the host calls instead of performing them.
    layout_name : str
        Registered layout to construct.
    input_text : str
        Text the stubbed primary input holds.

    Returns
    -------
    tuple[t.Any, list[str], list[SearchQuery]]
        The layout, its ordered lifecycle call log, and dispatched queries.
    """
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=_idle_query(),
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))
    monkeypatch.setattr(layout, "_search_input", _StubInput(input_text))
    calls: list[str] = []
    started: list[SearchQuery] = []
    monkeypatch.setattr(layout, "request_cancel", lambda: calls.append("cancel"))
    monkeypatch.setattr(
        layout,
        "record_history",
        lambda text: calls.append(f"history:{text}"),
    )

    def run_search(query: SearchQuery) -> None:
        calls.append("run")
        started.append(query)

    monkeypatch.setattr(layout, "run_search", run_search)
    return layout, calls, started


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_depth_escalation_runs_the_replacement_lifecycle(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
) -> None:
    """Give an escalation the lifecycle a submitted query gets.

    Order is the contract, not timing: ``run_search`` installs a fresh
    :class:`SearchControl`, so a cancel issued after it reaches nobody and the
    replaced run scans to completion behind the new one. The escalated draft is
    submitted for the first time here, so it also owes a history entry.
    """
    layout, calls, started = _escalation_layout(
        tmp_path,
        monkeypatch,
        layout_name=layout_name,
        input_text="needle",
    )

    assert layout.run_next_action("search.targeted") is True

    assert calls == ["cancel", "history:needle", "run"]
    assert started[0].effort == "targeted"


def test_depth_escalation_agrees_with_enter_on_a_literal_slash_query(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escalate the ``/``-shaped query the box holds, not the query before it.

    ``/usr/local`` resolves to no slash command, so Enter searches it as a
    literal path. An escalation that fell back to the retained draft would
    silently deepen a different request than the one on screen.
    """
    layout, _calls, started = _escalation_layout(
        tmp_path,
        monkeypatch,
        layout_name="greplog",
        input_text="/usr/local",
    )
    # A retained draft makes a wrong fallback visible instead of merely empty.
    layout._remember_active_search_text("deploy")

    assert layout.run_next_action("search.targeted") is True

    assert started[0].terms == layout.build_query("/usr/local").terms
    assert started[0].terms != layout.build_query("deploy").terms


class DraftCase(t.NamedTuple):
    """One typed line and the query a later depth command should escalate.

    Attributes
    ----------
    test_id : str
        Stable pytest id.
    typed : str
        Text the user leaves in the box before emptying it for a command.
    expected : str
        Query the escalation must run once the box holds ``/deep``.
    """

    test_id: str
    typed: str
    expected: str


DRAFT_CASES = (
    # Completes to no command, so Enter searches it — it is a query, and
    # clearing the box to reach /deep must not lose it.
    DraftCase("slash-shaped-query", "/usr/local", "/usr/local"),
    # Completes to /deep, so it is a command the user is still typing.
    DraftCase("partial-command", "/de", "deploy"),
    DraftCase("plain-query", "later", "later"),
)


@pytest.mark.parametrize("case", DRAFT_CASES, ids=[case.test_id for case in DRAFT_CASES])
def test_depth_escalation_remembers_the_query_it_was_typed_after(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DraftCase,
) -> None:
    """Keep a slash-shaped query as a query once the box empties for a command.

    Reaching a slash command means emptying the box, so the escalated query
    comes from what was typed before it. Treating every leading slash as a
    command line drops a path query such as ``/usr/local`` on the way, and the
    escalation silently deepens whatever preceded it instead.
    """
    layout, _calls, started = _escalation_layout(
        tmp_path,
        monkeypatch,
        layout_name="greplog",
        input_text="/deep",
    )
    layout._update_command_completion("deploy")
    layout._update_command_completion(case.typed)

    assert layout.run_next_action("search.targeted") is True

    assert started[0].terms == layout.build_query(case.expected).terms


def _depth_cursor_spans(offer: DepthOffer) -> list[t.Any]:
    """Return the accent cursor spans in the panel's rendered content."""
    visual = t.cast("t.Any", offer.visual)
    return [span for span in visual.spans if span.style == _DEPTH_OFFER_CURSOR_STYLE]


class _DepthOfferApp(App[None]):
    """One offer panel and one focusable sibling, to walk focus between them."""

    def compose(self) -> ComposeResult:
        """Yield a tab stop before the panel so focus can arrive and leave."""
        yield Input(id="away")
        yield DepthOffer(id="empty-depth")


@pytest.mark.slow
async def test_depth_offer_cursor_appears_on_first_focus() -> None:
    """Paint the keyboard cursor exactly while the panel holds focus.

    Driven through real focus keys rather than by assigning ``has_focus``:
    the defect this pins was an ordering bug between the panel's own focus
    handler and the assignment, so a test that sets the flag itself would
    bypass the thing under test.
    """
    app = _DepthOfferApp()
    async with app.run_test() as pilot:
        offer = app.query_one("#empty-depth", DepthOffer)
        offer.show_offer(offered_depth_actions(_idle_query()))
        await pilot.pause()
        assert _depth_cursor_spans(offer) == []

        await pilot.press("tab")
        await pilot.pause()

        assert app.focused is offer
        assert len(_depth_cursor_spans(offer)) == 1

        await pilot.press("shift+tab")
        await pilot.pause()

        assert app.focused is not offer
        assert _depth_cursor_spans(offer) == []


@pytest.mark.slow
async def test_depth_offer_arrow_keys_release_focus_at_the_top_row() -> None:
    """Up/down no longer wrap the two rows forever with no keyboard escape.

    Before the fix, ``down`` wrapped 1 -> 0 -> 1 forever and ``up`` never
    left row 0, so a keyboard user who arrived here (by Tab or by mouse
    click) had no arrow-key way out — only an unhinted Tab.
    """
    app = _DepthOfferApp()
    async with app.run_test() as pilot:
        offer = app.query_one("#empty-depth", DepthOffer)
        offer.show_offer(offered_depth_actions(_idle_query()))
        offer.focus()
        await pilot.pause()
        assert offer.highlighted == 0

        await pilot.press("down")
        await pilot.pause()
        assert offer.highlighted == 1

        # Clamped at the last row, not wrapped back to row 0.
        await pilot.press("down")
        await pilot.pause()
        assert offer.highlighted == 1
        assert app.focused is offer

        await pilot.press("up")
        await pilot.pause()
        assert offer.highlighted == 0
        assert app.focused is offer

        # Released at row 0, escaping to the previous focusable widget.
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not offer


def test_depth_offer_content_lead_line_uses_the_supplied_lead_style() -> None:
    """The lead sentence carries whatever resolved style the caller passes.

    Decoupled from theme setup: :meth:`DepthOffer._repaint_offer` resolves
    ``ag-muted`` via the running app's theme; this pins the shape of that
    hookup without needing a themed app.
    """
    content = depth_offer_content(
        offered_depth_actions(_idle_query()),
        lead_style="#abcdef",
    )
    lead_spans = [span for span in content.spans if span.style == "#abcdef"]
    assert lead_spans
    assert not any(span.style == "dim" for span in content.spans)


@pytest.mark.slow
async def test_depth_offer_lead_line_resolves_the_ag_muted_theme_token(
    tmp_path: pathlib.Path,
) -> None:
    """The running app resolves a real ``ag-muted`` hex, not the blunt ``dim``.

    ``ag-muted`` is registered by agentgrep's own theme (unlike Textual's
    stock palette), so this needs the real app factory rather than the
    minimal ``_DepthOfferApp`` shell.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        offer = layout.query_one("#empty-depth", DepthOffer)
        offer.show_offer(offered_depth_actions(_idle_query()))
        await pilot.pause()

        theme_vars = app.theme_variables
        expected = ui_theme.resolve(theme_vars, "ag-muted")
        assert expected  # sanity: agentgrep's theme registers this token
        visual = offer.visual
        assert any(span.style == expected for span in visual.spans)
        assert not any(span.style == "dim" for span in visual.spans)


def test_idle_depth_offer_matches_engine_authored_actions() -> None:
    """Author the pre-run offer from engine vocabulary, not TUI-local copy."""
    actions = offered_depth_actions(_idle_query())

    assert [action.action_id for action in actions] == [
        "search.targeted",
        "search.exhaustive",
    ]
    assert format_depth_offer_lead(actions) == (
        "Enter searches prompt history; conversation bodies are not read."
    )
    assert format_depth_offer_rows(actions) == (
        (
            "search.targeted",
            (
                "Deep search — read the conversations selected from prompt evidence "
                "(type depth:targeted)"
            ),
        ),
        (
            "search.exhaustive",
            "Search all conversations — read every readable conversation (type depth:exhaustive)",
        ),
    )


def test_idle_depth_offer_defers_to_explicit_scope_confirmation() -> None:
    """Keep the explicit-scope confirmation contract identical before a run."""
    actions = offered_depth_actions(
        dataclasses.replace(_idle_query(), scope_provenance="explicit"),
    )

    assert format_depth_offer_rows(actions) == ()
    assert format_depth_offer_lead(actions) == (
        "Change the explicit scope to all before searching conversations."
    )


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_pre_run_depth_action_refuses_an_empty_request(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
) -> None:
    """Never let a pre-run escalation scan conversations for nothing."""
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=_idle_query(),
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))
    # A draft the user has since cleared must not resurrect itself: escalating
    # from a visibly empty box would scan conversations for a hidden query.
    layout._remember_active_search_text("secret")
    notices: list[str] = []
    monkeypatch.setattr(layout, "notify", lambda message, **_kwargs: notices.append(message))
    started: list[SearchQuery] = []
    monkeypatch.setattr(layout, "run_search", started.append)

    assert layout.run_next_action("search.targeted") is False
    assert started == []
    assert notices == ["Type a query before choosing its conversation coverage."]


@pytest.mark.slow
async def test_idle_canvas_depth_offer_is_reachable_without_a_mouse(
    tmp_path: pathlib.Path,
) -> None:
    """Keep the advertised canvas affordance usable from the keyboard alone."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        offer = layout.query_one("#empty-depth", DepthOffer)
        layout._search_input.load_query("needle")
        await pilot.pause()

        await pilot.press("tab")
        assert app.focused is offer

        # The cursor starts on the cheapest rung; selection follows it.
        await pilot.press("down", "enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        summary = layout._run_summary
        assert summary is not None
        assert summary.requested_effort == "exhaustive"


class _DepthBoundCase(t.NamedTuple):
    """One refused depth-command argument and the warning it must produce.

    Attributes
    ----------
    test_id : str
        Stable parametrize id.
    action_id : str
        Engine action naming the rung the bound was typed for.
    argument : str
        Raw slash remainder the user typed after the command token.
    message : str
        Warning the layout must show instead of starting a search.
    """

    test_id: str
    action_id: str
    argument: str
    message: str


_UNUSABLE_DEPTH_BOUNDS = (
    _DepthBoundCase(
        test_id="non-numeric",
        action_id="search.targeted",
        argument="lots",
        message="Deep search takes an optional conversation count, such as /deep 50.",
    ),
    _DepthBoundCase(
        test_id="not-positive",
        action_id="search.targeted",
        argument="0",
        message="Deep search takes an optional conversation count, such as /deep 50.",
    ),
    _DepthBoundCase(
        # Superscripts satisfy str.isdigit but raise in int(); a pasted one
        # must be refused, not crash the message pump.
        test_id="digit-but-not-decimal",
        action_id="search.targeted",
        argument="²",
        message="Deep search takes an optional conversation count, such as /deep 50.",
    ),
    _DepthBoundCase(
        test_id="bound-on-exhaustive",
        action_id="search.exhaustive",
        argument="50",
        message="Searching all conversations reads every one of them, so it takes no count.",
    ),
)


@pytest.mark.parametrize(
    "case",
    _UNUSABLE_DEPTH_BOUNDS,
    ids=[case.test_id for case in _UNUSABLE_DEPTH_BOUNDS],
)
def test_depth_command_refuses_an_unusable_conversation_bound(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _DepthBoundCase,
) -> None:
    """Say why a typed count was rejected instead of searching at another depth."""
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=_idle_query(),
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
    )
    layout_spec = registry.layout_spec("greplog")
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))
    layout._remember_active_search_text("missing")
    notices: list[str] = []
    monkeypatch.setattr(layout, "notify", lambda message, **_kwargs: notices.append(message))
    started: list[SearchQuery] = []
    monkeypatch.setattr(layout, "run_search", started.append)

    assert layout.run_next_action(case.action_id, case.argument) is False
    assert started == []
    assert notices == [case.message]


@pytest.mark.slow
@pytest.mark.parametrize("layout_name", registry.layout_names())
async def test_slash_deep_bounds_a_typed_draft_without_a_prior_run(
    tmp_path: pathlib.Path,
    layout_name: str,
) -> None:
    """Begin a bounded deep search from a typed draft, in either registered layout."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            tmp_path,
            _idle_query(),
            control=SearchControl(),
            layout=layout_name,
        ),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        assert layout._run_summary is None

        # The draft is narrowed, then cleared so ``/`` can take the box: the
        # escalation must carry the query as last typed, not the longer draft
        # it was edited down from.
        await pilot.press(*"deployment")
        await pilot.press(*("backspace",) * len("ment"))
        await pilot.press("ctrl+c")
        await pilot.press(*"/deep", "space", *"3", "enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert layout.search_query.terms == ("deploy",)
        summary = layout._run_summary
        assert summary is not None
        assert summary.requested_effort == "targeted"
        assert summary.request.conversation_limit == 3

        # Both the depth and its bound stay request-local: the next plain
        # edit is back at the launch policy.
        rebuilt = layout.build_query("other")
        assert rebuilt.effort == "prompt"
        assert rebuilt.conversation_limit is None


def test_tui_depth_commands_are_discoverable_without_clearing_query() -> None:
    """Expose both engine escalation levels in the shared slash menu."""
    deep = resolve_command("deep")
    exhaustive = resolve_command("exhaustive")

    assert deep is not None
    assert deep.clears_input is False
    assert deep.accepts_args is True
    assert deep.argument_hint == "[count]"
    assert exhaustive is not None
    assert exhaustive.clears_input is False
    assert exhaustive.argument_hint == ""


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (_summary_for_effort("prompt"), "No prompt matches"),
        (_summary_for_effort("targeted"), "No candidate conversations"),
        (
            _summary_for_effort("targeted", selected=1),
            "No matches in selected conversations",
        ),
        (
            _summary_for_effort("exhaustive"),
            "No matches in readable conversations",
        ),
    ],
)
def test_tui_empty_copy_reflects_engine_outcome(
    summary: t.Any,
    expected: str,
) -> None:
    """Keep empty-state wording tied to terminal evidence, not result count alone."""
    assert format_empty_outcome(summary) == expected


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (
            _summary_for_effort("prompt"),
            "Prompt history only; conversation bodies were not read",
        ),
        (
            _summary_for_effort("targeted"),
            "Prompt evidence selected no conversation to read",
        ),
        (
            _summary_for_effort("targeted", selected=1),
            "Only the conversations selected from prompt evidence were read",
        ),
        (
            _summary_for_effort("exhaustive"),
            "Every readable conversation was read",
        ),
    ],
)
def test_empty_panel_names_the_surface_that_was_read(
    summary: t.Any,
    expected: str,
) -> None:
    """Keep a shallow miss legible as one rung's miss, not a corpus negative."""
    assert format_empty_evidence(summary) == expected


def test_prompt_summary_advertises_bounded_followups() -> None:
    """Render the two request-local escalation choices in the empty panel."""
    hint = format_next_action_hint(_summary_for_effort("prompt"))

    assert hint == "Next: /deep selected conversations · /exhaustive all conversations"


def test_explicit_prompt_scope_requests_scope_change() -> None:
    """Do not advertise depth commands that the explicit scope rejects."""
    hint = format_next_action_hint(
        _summary_for_effort("prompt", scope_provenance="explicit"),
    )

    assert hint == "Next: change the explicit scope to all before searching conversations"


def test_denied_depth_command_restores_active_query(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace a denied non-clearing command with the query it would escalate."""
    query = SearchQuery(
        terms=("missing",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="prompt",
        scope_provenance="explicit",
    )
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=query,
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
        base_scope_provenance="explicit",
    )
    layout_spec = registry.layout_spec("greplog")
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))
    restored: list[str] = []
    layout._search_input = types.SimpleNamespace(
        load_query=restored.append,
        focus=lambda: None,
    )
    monkeypatch.setattr(layout, "notify", lambda *args, **kwargs: None)
    layout._run_summary = _summary_for_effort(
        "prompt",
        scope_provenance="explicit",
    )

    assert layout._dispatch_slash_text("/deep") is False
    assert restored == ["missing"]


def test_targeted_status_reports_selected_conversation_coverage() -> None:
    """Show the bounded routing denominator instead of implying corpus coverage."""
    status = format_run_status(_summary_for_effort("targeted", selected=1))

    assert status == "Targeted search: 0 matches; 1/1 selected conversations read"


def test_engine_invoker_emits_engine_owned_terminal_summary(
    tmp_path: pathlib.Path,
) -> None:
    """Keep the interactive terminal state on the canonical engine contract."""
    query = SearchQuery(
        terms=("missing",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="prompt",
    )
    events: list[object] = []

    EngineSearchInvoker(tmp_path).run(
        query,
        control=SearchControl(),
        emit=events.append,
    )

    finished = [event for event in events if isinstance(event, StreamingSearchFinished)]
    assert len(finished) == 1
    assert finished[0].summary is not None
    assert finished[0].summary.outcome == "no_prompt_match"


@pytest.mark.parametrize("layout_name", registry.layout_names())
@pytest.mark.parametrize(
    ("base_effort", "expected_effort"),
    [("prompt", "prompt"), ("exhaustive", "exhaustive")],
)
def test_plain_edit_resets_to_launch_effort(
    tmp_path: pathlib.Path,
    layout_name: str,
    base_effort: SearchEffort,
    expected_effort: SearchEffort,
) -> None:
    """Keep scope-derived depth transient while preserving explicit exhaustive effort."""
    query = SearchQuery(
        terms=("initial",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="targeted",
        conversation_limit=25,
        scope_provenance="explicit",
    )
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=query,
        control=SearchControl(),
        base_scope="prompts",
        base_effort=base_effort,
        base_scope_provenance="inferred",
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))

    if layout_name == "hud":
        rebuilt = layout._build_search_query("next")
    else:
        rebuilt = layout.build_query("next")

    assert rebuilt.scope == "prompts"
    assert rebuilt.effort == expected_effort
    assert rebuilt.conversation_limit is None
    assert rebuilt.scope_provenance == "inferred"


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_plain_edit_restores_custom_launch_conversation_limit(
    tmp_path: pathlib.Path,
    layout_name: str,
) -> None:
    """Restore a custom deep cap after a transient depth action changed it."""
    query = SearchQuery(
        terms=("initial",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="targeted",
        conversation_limit=25,
    )
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=query,
        control=SearchControl(),
        base_scope="all",
        base_effort="targeted",
        base_conversation_limit=7,
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))

    if layout_name == "hud":
        rebuilt = layout._build_search_query("next")
    else:
        rebuilt = layout.build_query("next")

    assert rebuilt.effort == "targeted"
    assert rebuilt.conversation_limit == 7


class _QueryErrorInput:
    """Minimal search-input class surface for the workflow error contract."""

    id = "search"

    def __init__(self, value: str) -> None:
        self.classes: set[str] = set()
        self.value = value
        self.focus_count = 0

    def add_class(self, *classes: str) -> None:
        """Record added Textual classes."""
        self.classes.update(classes)

    def remove_class(self, *classes: str) -> None:
        """Record removed Textual classes."""
        self.classes.difference_update(classes)

    def has_class(self, class_name: str) -> bool:
        """Return whether one Textual class is present."""
        return class_name in self.classes

    def focus(self) -> None:
        """Record explicit focus restoration."""
        self.focus_count += 1


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_targeted_prompt_query_stays_editable_without_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
) -> None:
    """Keep an invalid inline scope local to either registered search box."""
    query = SearchQuery(
        terms=("initial",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="targeted",
        conversation_limit=25,
    )
    control = SearchControl()
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=query,
        control=control,
        base_scope="all",
        base_effort="targeted",
        base_conversation_limit=25,
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    workflow = workflow_spec.loader()()
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow))
    invalid_text = "needle scope:prompts"
    search_input = _QueryErrorInput(invalid_text)
    layout._search_input = search_input
    if layout_name == "greplog":
        layout._status = types.SimpleNamespace(update=lambda _message: None)
        monkeypatch.setattr(layout, "_update_command_completion", lambda _value: False)
        monkeypatch.setattr(layout, "_hide_command_completion", lambda: None)
    else:
        monkeypatch.setattr(layout, "_update_search_dropdown", lambda _value: None)
        monkeypatch.setattr(layout, "_set_results_view", lambda _state: None)
    run_search = layout.run_search
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        layout,
        "notify",
        lambda message, *, title, severity: notices.append(
            (message, title, severity),
        ),
    )
    side_effects: list[str] = []
    monkeypatch.setattr(layout, "request_cancel", lambda: side_effects.append("cancel"))
    monkeypatch.setattr(
        layout,
        "record_history",
        lambda _text: side_effects.append("history"),
    )
    monkeypatch.setattr(
        layout,
        "run_search",
        lambda _query: side_effects.append("worker"),
    )

    workflow.on_query(layout, invalid_text)

    assert side_effects == []
    assert notices == [
        (
            "targeted effort requires conversation or all scope",
            "Invalid query",
            "error",
        ),
    ]
    assert search_input.has_class("-error")
    assert search_input.value == invalid_text
    assert search_input.focus_count == 1
    assert control.answer_now_requested() is False

    if layout_name == "hud":
        layout._apply_finished("complete", 0, 0.1, None)
        assert search_input.has_class("-error")
        started: list[SearchQuery] = []
        monkeypatch.setattr(layout, "_start_search_worker", started.append)
        valid_query = layout.build_query("needle")

        run_search(valid_query)

        assert layout._query_error_active is False
        assert started == [valid_query]

    search_input.value = "needle"
    layout.on_input_changed(
        types.SimpleNamespace(input=search_input, value="needle"),
    )

    assert not search_input.has_class("-error")
    assert search_input.value == "needle"


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_prompt_broad_scope_query_stays_editable_without_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
) -> None:
    """The symmetric contradiction (``depth:prompt`` + explicit broad scope) also blocks dispatch.

    Mirrors ``test_targeted_prompt_query_stays_editable_without_dispatch``:
    an explicitly-selected scope leaves ``resolve_request_modifiers``'
    prompt-narrowing reconciliation off, so this combination still reaches
    ``SearchWorkflow.on_query`` and must be rejected there instead of
    dispatched.
    """
    query = SearchQuery(
        terms=("initial",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="exhaustive",
        scope_provenance="explicit",
    )
    control = SearchControl()
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", object()),
        query=query,
        control=control,
        base_scope="all",
        base_effort="exhaustive",
        base_scope_provenance="explicit",
        base_conversation_limit=None,
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    workflow = workflow_spec.loader()()
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow))
    invalid_text = "needle depth:prompt"
    search_input = _QueryErrorInput(invalid_text)
    layout._search_input = search_input
    if layout_name == "greplog":
        layout._status = types.SimpleNamespace(update=lambda _message: None)
        monkeypatch.setattr(layout, "_update_command_completion", lambda _value: False)
        monkeypatch.setattr(layout, "_hide_command_completion", lambda: None)
    else:
        monkeypatch.setattr(layout, "_update_search_dropdown", lambda _value: None)
        monkeypatch.setattr(layout, "_set_results_view", lambda _state: None)
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        layout,
        "notify",
        lambda message, *, title, severity: notices.append(
            (message, title, severity),
        ),
    )
    side_effects: list[str] = []
    monkeypatch.setattr(layout, "request_cancel", lambda: side_effects.append("cancel"))
    monkeypatch.setattr(
        layout,
        "record_history",
        lambda _text: side_effects.append("history"),
    )
    monkeypatch.setattr(
        layout,
        "run_search",
        lambda _query: side_effects.append("worker"),
    )

    workflow.on_query(layout, invalid_text)

    assert side_effects == []
    assert notices == [
        (
            "prompt effort requires prompt scope",
            "Invalid query",
            "error",
        ),
    ]
    assert search_input.has_class("-error")
