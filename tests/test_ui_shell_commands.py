"""Mounted-shell regressions for Textual command handling."""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.command import CommandPalette
from textual.widgets import Footer, HelpPanel

from agentgrep.records import SearchRecord
from agentgrep.ui import theme as ui_theme
from agentgrep.ui._shell import ExplorerApp
from tests.test_agentgrep import _build_empty_ui_app


class ShellSizeCase(t.NamedTuple):
    """One terminal size whose shell state must survive Ctrl-P."""

    test_id: str
    size: tuple[int, int]


_SHELL_SIZE_CASES: tuple[ShellSizeCase, ...] = (
    ShellSizeCase(test_id="stacked-77x30", size=(77, 30)),
    ShellSizeCase(test_id="split-120x30", size=(120, 30)),
)


async def _mount_greplog(app: t.Any, pilot: t.Any) -> t.Any:
    """Push the grep-log layout with its normal search workflow."""
    from agentgrep.ui.layouts.greplog import GrepLogLayout
    from agentgrep.ui.workflows.search import SearchWorkflow

    layout = GrepLogLayout(app._ctx, SearchWorkflow())
    await app.push_screen(layout)
    await pilot.pause()
    return layout


async def _submit(pilot: t.Any, layout: t.Any, text: str) -> None:
    """Submit ``text`` through a mounted layout's real search input."""
    layout._search_input.value = text
    layout._search_input.cursor_position = len(text)
    layout._search_input.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def _capture_screenshot_callbacks(
    layout: t.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[t.Callable[[], None]]:
    """Retain only screenshot delivery callbacks without disturbing Textual."""
    callbacks: list[t.Callable[[], None]] = []
    original = layout.call_after_refresh

    def capture(
        callback: t.Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> bool:
        if getattr(callback, "__name__", "") == "_deliver_screenshot_after_refresh":
            callbacks.append(t.cast("t.Callable[[], None]", callback))
            return True
        return bool(original(callback, *args, **kwargs))

    monkeypatch.setattr(layout, "call_after_refresh", capture)
    return callbacks


def _zoom_record(tmp_path: pathlib.Path, index: int) -> SearchRecord:
    """Build one visible detail record for logical-zoom Pilot tests."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / f"zoom-{index}.jsonl",
        text=f"ZOOM DETAIL RECORD {index}",
    )


def _seed_zoom_layout(layout: t.Any, record: SearchRecord) -> None:
    """Populate one mounted HUD layout for logical-zoom focus tests."""
    layout._set_empty_state(empty=False)
    layout.all_records.append(record)
    layout.filtered_records = [record]
    layout._results.set_records([record])


def _assert_zoomed_focus(layout: t.Any, pane: str, target: t.Any) -> None:
    """Assert one focused content target is visible in the selected zoom."""
    other = "detail" if pane == "results" else "results"
    assert layout._zoomed_pane == pane
    assert layout._body.has_class(f"-zoom-{pane}")
    assert not layout._body.has_class(f"-zoom-{other}")
    assert layout.app.focused is target
    assert target.is_on_screen
    assert layout._search_input.is_on_screen


async def _type_command(pilot: t.Any, text: str) -> None:
    """Type and submit a slash command through the focused search input."""
    await pilot.press(*text, "enter")
    await pilot.pause()


def test_explorer_app_disables_textual_command_palette() -> None:
    """The shell disables Textual's palette and exposes no providers."""
    assert ExplorerApp.ENABLE_COMMAND_PALETTE is False
    assert not ExplorerApp.COMMANDS


@pytest.mark.parametrize(
    "case",
    _SHELL_SIZE_CASES,
    ids=[case.test_id for case in _SHELL_SIZE_CASES],
)
async def test_ctrl_p_preserves_mounted_shell_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ShellSizeCase,
) -> None:
    """Ctrl-P is inert across the stacked and split HUD layouts."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        body = app.screen.query_one("#body")
        search.value = "palette needle"
        search.cursor_position = len("palette")
        search.focus()
        await pilot.pause()

        screen_stack = tuple(app.screen_stack)
        focused = app.focused
        search_value = search.value
        search_cursor = search.cursor_position
        search_region = search.region
        body_region = body.region

        await pilot.press("ctrl+p")
        await pilot.pause()

        assert not CommandPalette.is_open(app)
        assert tuple(app.screen_stack) == screen_stack
        assert app.focused is focused
        assert search.value == search_value
        assert search.cursor_position == search_cursor
        assert search.region == search_region
        assert body.region == body_region


async def test_slash_keys_toggles_one_help_panel_without_notifications(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated ``/keys`` opens one help panel, then closes it cleanly."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        screen_stack = tuple(app.screen_stack)
        record = _zoom_record(tmp_path, 0)
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout._results.append_records((record,))
        query = layout.search_query
        results = layout._results
        assert layout.handle_maximize_command("results") is True

        await _submit(pilot, layout, "/keys")

        assert tuple(app.screen_stack) == screen_stack
        assert len(layout.query(HelpPanel)) == 1
        assert len(app._notifications) == 0
        assert layout._zoomed_pane == "results"
        assert layout.search_query is query
        assert layout._results is results
        assert results._records == [record]
        assert layout._search_input.value == ""
        assert app.focused is layout._search_input

        await _submit(pilot, layout, "/keys")

        assert len(layout.query(HelpPanel)) == 0
        assert len(app._notifications) == 0
        assert layout._zoomed_pane == "results"
        assert layout.search_query is query
        assert layout._results is results
        assert results._records == [record]
        assert app.focused is layout._search_input
        await pilot.press("u", "s", "a", "b", "l", "e")
        assert layout._search_input.value == "usable"


async def test_slash_theme_selects_and_toggles_agentgrep_themes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/theme`` changes only between agentgrep's dark and light themes."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        def forbidden(*_args: object, **_kwargs: object) -> t.NoReturn:
            message = "theme command opened a palette screen"
            raise AssertionError(message)

        monkeypatch.setattr(app, "search_themes", forbidden, raising=False)
        monkeypatch.setattr(app, "push_screen", forbidden)

        await _submit(pilot, app.screen, "/theme light")
        assert app.theme == ui_theme.LIGHT_THEME_NAME
        assert app.screen._search_input.value == ""

        await _submit(pilot, app.screen, "/theme")
        assert app.theme == ui_theme.DARK_THEME_NAME
        assert app.screen._search_input.value == ""


async def test_slash_screenshot_delivers_after_command_chrome_clears(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/screenshot`` captures the active layout without its command chrome."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        record = _zoom_record(tmp_path, 0)
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout._results.append_records((record,))
        query = layout.search_query
        control = layout.control
        all_records = layout.all_records
        filtered_records = layout.filtered_records
        results = layout._results
        result_records = results._records
        workers = tuple(app.workers)
        delivered: list[tuple[str, bool, bool, bool, bool, bool, bool, bool, bool, bool]] = []

        def deliver_screenshot() -> str:
            delivered.append(
                (
                    str(layout._search_input.value),
                    bool(layout._enum_dropdown.display),
                    layout._body.has_class("-zoom-results"),
                    layout.search_query is query,
                    layout.control is control,
                    layout.all_records is all_records,
                    layout.filtered_records is filtered_records,
                    layout._results is results,
                    results._records is result_records,
                    tuple(app.workers) == workers,
                ),
            )
            return "screenshot-key"

        monkeypatch.setattr(app, "deliver_screenshot", deliver_screenshot)
        layout._set_empty_state(empty=False)
        assert layout.handle_maximize_command("results") is True

        layout._search_input.value = "/screenshot"
        layout._search_input.cursor_position = len("/screenshot")
        layout._search_input.focus()
        await pilot.pause()
        assert layout._enum_dropdown.display is True
        await pilot.press("enter")
        await pilot.pause()

        assert delivered == [("", False, True, True, True, True, True, True, True, True)]
        assert app.screen is layout
        assert app.focused is layout._search_input
        assert control.answer_now_requested() is False
        assert result_records == [record]


async def test_screenshot_prefix_dispatches_highlighted_menu_command(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short prefix narrows the menu and Enter runs the highlighted command."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        queries: list[str] = []
        delivered: list[object] = []
        monkeypatch.setattr(layout.workflow, "on_query", lambda _host, text: queries.append(text))
        monkeypatch.setattr(app, "deliver_screenshot", lambda: delivered.append(object()))

        layout._search_input.value = "/scr"
        layout._search_input.cursor_position = len("/scr")
        layout._search_input.focus()
        await pilot.pause()

        assert [command.name for command in layout._command_matches] == ["screenshot"]
        await pilot.press("enter")
        await pilot.pause()

        assert queries == []
        assert len(delivered) == 1


async def test_slash_screenshot_retains_command_when_scheduling_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed refresh callback schedule leaves the command editable."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        original = layout.call_after_refresh
        delivered: list[object] = []

        def reject_screenshot(
            callback: t.Callable[..., object],
            *args: object,
            **kwargs: object,
        ) -> bool:
            if getattr(callback, "__name__", "") == "_deliver_screenshot_after_refresh":
                return False
            return bool(original(callback, *args, **kwargs))

        monkeypatch.setattr(layout, "call_after_refresh", reject_screenshot)
        monkeypatch.setattr(app, "deliver_screenshot", lambda: delivered.append(object()))

        await _submit(pilot, layout, "/screenshot")

        assert layout._search_input.value == "/screenshot"
        assert layout._enum_dropdown.display is True
        assert delivered == []


async def test_slash_screenshot_rejects_path_argument_as_search_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screenshot filenames stay Textual-owned in the initial command surface."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        queries: list[str] = []
        delivered: list[object] = []
        monkeypatch.setattr(layout.workflow, "on_query", lambda _host, text: queries.append(text))
        monkeypatch.setattr(app, "deliver_screenshot", lambda: delivered.append(object()))

        await _submit(pilot, layout, "/screenshot named.svg")

        assert queries == ["/screenshot named.svg"]
        assert delivered == []


async def test_slash_screenshot_drops_delivery_after_layout_switch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback retained by its origin cannot capture the next F2 layout."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        origin = app.screen
        callbacks = _capture_screenshot_callbacks(origin, monkeypatch)
        delivered: list[object] = []
        monkeypatch.setattr(app, "deliver_screenshot", lambda: delivered.append(object()))

        await _submit(pilot, origin, "/screenshot")
        assert len(callbacks) == 1
        await pilot.press("f2")
        await pilot.pause()
        assert app.screen is not origin

        callbacks[0]()

        assert delivered == []


async def test_slash_screenshot_drops_delivery_for_empty_stack(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred callback tolerates an empty stack during app teardown."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        origin = app.screen
        callbacks = _capture_screenshot_callbacks(origin, monkeypatch)
        delivered: list[object] = []
        monkeypatch.setattr(app, "deliver_screenshot", lambda: delivered.append(object()))

        await _submit(pilot, origin, "/screenshot")
        assert len(callbacks) == 1
        with monkeypatch.context() as context:
            context.setattr(type(app), "screen_stack", property(lambda _app: []))
            callbacks[0]()

        assert delivered == []


async def test_slash_screenshot_drops_delivery_after_origin_detaches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app-teardown layout's retained callback is a safe no-op."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    callbacks: list[t.Callable[[], None]] = []
    delivered: list[object] = []
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        origin = app.screen
        callbacks = _capture_screenshot_callbacks(origin, monkeypatch)
        monkeypatch.setattr(app, "deliver_screenshot", lambda: delivered.append(object()))

        await _submit(pilot, origin, "/screenshot")
        assert len(callbacks) == 1

    assert origin.is_attached is False
    callbacks[0]()

    assert delivered == []


async def test_invalid_slash_theme_remains_editable_and_does_not_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognized invalid theme warns without clearing or searching the text."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        queries: list[str] = []
        monkeypatch.setattr(layout, "notify", lambda *a, **k: notes.append((a, k)))
        monkeypatch.setattr(layout.workflow, "on_query", lambda _host, text: queries.append(text))

        await _submit(pilot, layout, "/theme sepia")

        assert app.theme == ui_theme.DARK_THEME_NAME
        assert layout._search_input.value == "/theme sepia"
        assert queries == []
        assert len(notes) == 1
        assert "dark or light" in str(notes[0][0][0]).lower()


async def test_common_commands_run_in_greplog(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grep-log exposes the same help and clear commands as the HUD."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(layout, "notify", lambda *a, **k: notes.append((a, k)))

        await _submit(pilot, layout, "/help")
        assert layout._search_input.value == ""
        assert len(notes) == 1
        assert "/theme" in str(notes[0][0][0])

        layout._records = [object()]
        old_control = layout.control
        await _submit(pilot, layout, "/clear")
        assert old_control.answer_now_requested() is True
        assert layout._records == []
        assert layout._search_input.value == ""


async def test_greplog_slash_menu_lists_filters_and_selects_at_77_columns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grep-log exposes the full shared slash menu in a narrow terminal."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(77, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)

        await pilot.press("/")
        await pilot.pause()

        dropdown = layout.query_one("#enum-dropdown")
        assert dropdown.display is True
        assert [command.name for command in layout._command_matches] == [
            "clear",
            "exit",
            "help",
            "keys",
            "screenshot",
            "theme",
            "maximize",
            "minimize",
        ]
        assert dropdown.option_count == len(layout._command_matches)

        await pilot.press("t", "h")
        await pilot.pause()

        assert [command.name for command in layout._command_matches] == ["theme"]
        assert dropdown.option_count == 1
        await pilot.press("enter")
        await pilot.pause()

        assert app.theme == ui_theme.LIGHT_THEME_NAME
        assert layout._search_input.value == ""
        assert dropdown.display is False


async def test_greplog_keys_theme_and_screenshot_match_hud_commands(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grep-log runs the direct keys, theme, and screenshot command forms."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(77, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        delivered: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            app,
            "deliver_screenshot",
            lambda: delivered.append(
                (
                    str(layout._search_input.value),
                    bool(layout._enum_dropdown.display),
                ),
            ),
        )

        await _submit(pilot, layout, "/keys")
        assert len(layout.query(HelpPanel)) == 1
        assert len(app._notifications) == 0
        assert layout._search_input.value == ""

        await _submit(pilot, layout, "/keys")
        assert len(layout.query(HelpPanel)) == 0
        assert len(app._notifications) == 0
        assert layout._search_input.value == ""

        await _submit(pilot, layout, "/theme light")
        assert app.theme == ui_theme.LIGHT_THEME_NAME
        assert layout._search_input.value == ""

        await _submit(pilot, layout, "/screenshot")
        assert delivered == [("", False)]
        assert layout._search_input.value == ""


@pytest.mark.parametrize("text", ("/exit", "/quit"))
async def test_exit_aliases_run_in_greplog(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    """Both exit spellings remain reachable from the grep-log input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        exits: list[object] = []
        monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append((a, k)))

        await _submit(pilot, layout, text)

        assert len(exits) == 1
        assert layout._search_input.value == ""


async def test_unsupported_command_arguments_remain_greplog_search_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy command-plus-text forms still route through the grep workflow."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        queries: list[str] = []
        monkeypatch.setattr(layout.workflow, "on_query", lambda _host, text: queries.append(text))

        await _submit(pilot, layout, "/help find prompts")

        assert queries == ["/help find prompts"]


async def test_hud_zoom_help_names_its_logical_panes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HUD command metadata advertises results/detail rather than widgets."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        by_name = {command.name: command for command in app.screen.slash_commands}

        assert by_name["maximize"].argument_hint == "[results|detail]"
        assert by_name["minimize"].argument_hint == ""


@pytest.mark.parametrize("view", ["empty", "searching"])
@pytest.mark.parametrize("size", [(120, 30), (77, 30)], ids=["wide", "stacked"])
async def test_detail_zoom_search_states_keep_body_content_visible(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    view: str,
    size: tuple[int, int],
) -> None:
    """Results-hosted search states replace an incompatible detail zoom."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _zoom_record(tmp_path, 0)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        layout = app.screen
        _seed_zoom_layout(layout, record)
        await _submit(pilot, layout, "/maximize detail")
        body = layout.query_one("#body")
        results = layout.query_one("#results")

        if view == "empty":
            layout.reset_view()
            state_panel = layout.query_one("#empty-hint")
        else:
            layout._set_results_view("searching")
            state_panel = layout.query_one("#searching-panel")
        await pilot.pause()

        assert state_panel.is_on_screen
        assert layout.query_one("#results-column").region.width > 0
        assert layout.query_one("#results-column").region.height > 0
        assert layout._zoomed_pane is None
        assert not body.has_class("-zoom-results")
        assert not body.has_class("-zoom-detail")

        await layout._apply_records_batch((_zoom_record(tmp_path, 1),), 1)
        await pilot.pause()

        assert results.is_on_screen
        assert results.region.width > 0
        assert results.region.height > 0


async def test_wide_hud_zoom_keeps_shell_usable_and_restores_geometry(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide results zoom leaves slash input usable and restores the split."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        layout._set_empty_state(empty=False)
        await pilot.pause()
        search = layout.query_one("#search")
        dropdown = layout.query_one("#enum-dropdown")
        footer = layout.query_one(Footer)
        body = layout.query_one("#body")
        results_column = layout.query_one("#results-column")
        detail_column = layout.query_one("#detail-column")
        original = (
            body.region,
            results_column.region,
            detail_column.region,
            body.has_class("-stacked"),
            detail_column.has_class("-collapsed"),
        )
        assert layout.maximized is None

        await _submit(pilot, layout, "/maximize")

        assert layout.maximized is None
        assert body.has_class("-zoom-results")
        assert not body.has_class("-zoom-detail")
        assert results_column.region == body.region
        assert detail_column.region.width == 0
        assert search.region.height == 3
        assert footer.region.height > 0
        assert app.focused is search

        await pilot.press("/")
        await pilot.pause()
        assert search.value == "/"
        assert dropdown.display is True
        await _type_command(pilot, "minimize")

        assert layout.maximized is None
        assert not body.has_class("-zoom-results")
        assert not body.has_class("-zoom-detail")
        assert (
            body.region,
            results_column.region,
            detail_column.region,
            body.has_class("-stacked"),
            detail_column.has_class("-collapsed"),
        ) == original
        assert app.focused is search


async def test_wide_zoom_navigation_switches_to_visible_sibling(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Horizontal pane traversal switches zoom before moving focus."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _zoom_record(tmp_path, 0)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        _seed_zoom_layout(layout, record)

        await _submit(pilot, layout, "/maximize results")
        layout._results.focus()
        await pilot.pause()
        await pilot.press("ctrl+l")
        await pilot.pause()

        _assert_zoomed_focus(layout, "detail", layout._detail_scroll)

        await pilot.press("ctrl+h")
        await pilot.pause()

        _assert_zoomed_focus(layout, "results", layout._results)


async def test_stacked_zoom_navigation_switches_to_visible_sibling(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertical pane traversal switches zoom before moving focus."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _zoom_record(tmp_path, 0)
    async with app.run_test(size=(77, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        _seed_zoom_layout(layout, record)

        await _submit(pilot, layout, "/maximize results")
        layout._results.focus()
        await pilot.pause()
        await pilot.press("ctrl+j")
        await pilot.pause()

        _assert_zoomed_focus(layout, "detail", layout._detail_scroll)

        await pilot.press("ctrl+k")
        await pilot.pause()

        _assert_zoomed_focus(layout, "results", layout._results)


@pytest.mark.parametrize("size", [(120, 30), (77, 30)], ids=["wide", "stacked"])
async def test_detail_zoom_navigation_never_focuses_hidden_results_widgets(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    """Search-down and detail-up keep every focus target on-screen."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _zoom_record(tmp_path, 0)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        layout = app.screen
        _seed_zoom_layout(layout, record)

        await _submit(pilot, layout, "/maximize detail")
        await pilot.press("ctrl+j")
        await pilot.pause()

        _assert_zoomed_focus(layout, "results", layout._filter_input)

        await _submit(pilot, layout, "/maximize detail")
        layout._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()

        expected = layout._results if layout._stacked else layout._filter_input
        _assert_zoomed_focus(layout, "results", expected)


async def test_narrow_detail_zoom_renders_selection_without_losing_collapse(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 77-column detail zoom renders first, then restores collapsed geometry."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_zoom_record(tmp_path, index) for index in range(3)]
    async with app.run_test(size=(77, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        layout._set_empty_state(empty=False)
        layout.all_records.extend(records)
        layout.filtered_records = list(records)
        layout._results.append_records(records)
        layout._results._reactive_highlighted = 2
        layout._current_detail_record = records[0]
        layout._detail_opened = False
        layout._apply_responsive_layout()
        await pilot.pause()
        body = layout.query_one("#body")
        results_column = layout.query_one("#results-column")
        detail_column = layout.query_one("#detail-column")
        dropdown = layout.query_one("#enum-dropdown")
        footer = layout.query_one(Footer)
        original = (
            body.region,
            results_column.region,
            detail_column.region,
            body.has_class("-stacked"),
            detail_column.has_class("-collapsed"),
        )
        assert original[-2:] == (True, True)
        assert layout.maximized is None

        await _submit(pilot, layout, "/maximize detail")

        assert layout.maximized is None
        assert body.has_class("-zoom-detail")
        assert not body.has_class("-zoom-results")
        assert body.has_class("-stacked")
        assert detail_column.has_class("-collapsed")
        assert layout._detail_opened is False
        assert layout._current_detail_record is records[2]
        assert results_column.region.height == 0
        assert detail_column.region == body.region
        assert app.focused is layout._search_input
        assert "ZOOM&#160;DETAIL&#160;RECORD&#160;2" in app.export_screenshot(simplify=True)
        assert layout.query_one(Footer) is footer
        assert layout.query_one("#enum-dropdown") is dropdown
        assert footer.is_mounted and footer.region.height > 0
        assert dropdown.is_mounted

        await pilot.press("/")
        await pilot.pause()
        assert dropdown.display is True
        await _type_command(pilot, "minimize")

        assert layout.maximized is None
        assert not body.has_class("-zoom-detail")
        assert (
            body.region,
            results_column.region,
            detail_column.region,
            body.has_class("-stacked"),
            detail_column.has_class("-collapsed"),
        ) == original
        assert layout.query_one(Footer) is footer
        assert layout.query_one("#enum-dropdown") is dropdown
        assert footer.is_mounted and footer.region.height > 0
        assert dropdown.is_mounted


async def test_maximize_cached_small_detail_preserves_find_and_search_focus(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cached fast path preserves find state and returns focus to search."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "cached-small.jsonl",
        text="needle before needle after",
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        layout._set_empty_state(empty=False)
        layout.all_records.append(record)
        layout.filtered_records = [record]
        layout._results.append_records((record,))
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()
        layout.action_open_detail_find()
        layout._detail_find_input.load_query("needle")
        layout._run_detail_find("needle", reset_cursor=True)
        expected_matches = list(layout._detail_find_matches)
        layout._results._reactive_highlighted = 0

        await _submit(pilot, layout, "/maximize detail")

        assert layout._detail_body_is_cached(())
        assert layout._current_detail_record is record
        assert layout._detail_find_active is True
        assert layout._detail_find_query == "needle"
        assert layout._detail_find_matches == expected_matches
        assert layout._body.has_class("-zoom-detail")
        assert app.focused is layout._search_input


async def test_hud_bare_zoom_tracks_last_pane_and_explicit_target_selects(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare maximize toggles last-use; named targets switch without toggling."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _zoom_record(tmp_path, 0)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        layout._set_empty_state(empty=False)
        layout.all_records.append(record)
        layout.filtered_records = [record]
        layout._results.append_records((record,))
        layout.show_detail(record)
        layout._detail_scroll.focus()
        await pilot.pause()
        layout._search_input.focus()
        await pilot.pause()

        await _submit(pilot, layout, "/maximize")
        assert layout._body.has_class("-zoom-detail")

        await _submit(pilot, layout, "/maximize results")
        assert layout._body.has_class("-zoom-results")
        assert not layout._body.has_class("-zoom-detail")

        await _submit(pilot, layout, "/maximize results")
        assert layout._body.has_class("-zoom-results")

        await _submit(pilot, layout, "/maximize detail")
        assert layout._body.has_class("-zoom-detail")

        await _submit(pilot, layout, "/maximize")
        assert not layout._body.has_class("-zoom-detail")
        assert not layout._body.has_class("-zoom-results")
        assert layout.maximized is None


async def test_hud_filter_focus_makes_bare_zoom_target_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filter focus makes the results side the last-used content pane."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _zoom_record(tmp_path, 0)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        layout._set_empty_state(empty=False)
        layout.all_records.append(record)
        layout.filtered_records = [record]
        layout._results.append_records((record,))
        layout.show_detail(record)
        layout._detail_scroll.focus()
        await pilot.pause()
        assert layout._last_content_pane == "detail"

        layout._filter_input.focus()
        await pilot.pause()
        assert layout._last_content_pane == "results"

        await _submit(pilot, layout, "/maximize")
        assert layout._body.has_class("-zoom-results")


async def test_hud_empty_detail_zoom_warns_and_retains_command(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detail zoom without a record keeps layout and command text retryable."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(77, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(layout, "notify", lambda *a, **k: notes.append((a, k)))
        original = (
            layout._body.region,
            layout._body.has_class("-stacked"),
            layout._detail_column.has_class("-collapsed"),
        )

        await _submit(pilot, layout, "/maximize detail")

        assert layout._search_input.value == "/maximize detail"
        assert not layout._body.has_class("-zoom-detail")
        assert (
            layout._body.region,
            layout._body.has_class("-stacked"),
            layout._detail_column.has_class("-collapsed"),
        ) == original
        assert len(notes) == 1
        assert notes[0][1].get("severity") == "warning"
        assert "detail" in str(notes[0][0][0]).lower()
        assert layout.maximized is None
