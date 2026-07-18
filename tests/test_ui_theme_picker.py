"""Focused contracts for owned theme profiles and their setup picker."""

from __future__ import annotations

import importlib
import json
import pathlib
import threading
import time
import typing as t

import pytest

from agentgrep.ui import theme
from tests.test_agentgrep import _build_empty_ui_app, load_agentgrep_module


def test_owned_theme_catalog_is_stable_and_complete() -> None:
    """The curated catalog owns stable names and complete semantic palettes."""
    profiles = theme.THEME_PROFILES
    names = tuple(profile.name for profile in profiles)

    assert names == (
        theme.DARK_THEME_NAME,
        theme.LIGHT_THEME_NAME,
        "agentgrep-tokyo-night",
    )
    assert isinstance(profiles, tuple)
    expected = set(theme.agentgrep_dark().variables)
    assert all(set(profile.build().variables) == expected for profile in profiles)


def test_theme_choice_round_trips_through_xdg_config(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selected owned theme is atomically persisted below XDG config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    preferences = importlib.import_module("agentgrep.ui.preferences")

    assert preferences.load_theme_name() is None
    assert preferences.save_theme_name("agentgrep-tokyo-night") is True

    assert preferences.load_theme_name() == "agentgrep-tokyo-night"
    config_path = preferences.theme_config_path()
    assert config_path == tmp_path / "config" / "agentgrep" / "preferences.json"
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert tuple(config_path.parent.glob("*.tmp")) == ()


def test_theme_choice_preserves_unrelated_preferences(tmp_path: pathlib.Path) -> None:
    """Updating the theme does not erase future or independently-owned keys."""
    preferences = importlib.import_module("agentgrep.ui.preferences")
    config_path = tmp_path / "agentgrep" / "preferences.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({"future": {"value": 1}, "ui": {"density": "compact"}}),
        encoding="utf-8",
    )

    assert preferences.save_theme_name(theme.LIGHT_THEME_NAME, config_path) is True

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload == {
        "future": {"value": 1},
        "ui": {"density": "compact", "theme": theme.LIGHT_THEME_NAME},
    }


def test_theme_preferences_use_supplied_home_and_tolerate_bad_data(
    tmp_path: pathlib.Path,
) -> None:
    """The factory home is respected and malformed state degrades to setup."""
    preferences = importlib.import_module("agentgrep.ui.preferences")
    home = tmp_path / "home"
    path = preferences.theme_config_path(environ={}, home=home)
    assert path == home / ".config" / "agentgrep" / "preferences.json"

    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xffnot-json")
    assert preferences.load_theme_name(path) is None

    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    assert preferences.save_theme_name("agentgrep-dark", blocked_parent / "x.json") is False


def test_oversized_preferences_are_never_silently_replaced(tmp_path: pathlib.Path) -> None:
    """An unreadable bounded document remains intact instead of losing future keys."""
    preferences = importlib.import_module("agentgrep.ui.preferences")
    path = tmp_path / "preferences.json"
    original = json.dumps({"future": "x" * (70 * 1024)}).encode()
    path.write_bytes(original)

    assert preferences.save_theme_name(theme.DARK_THEME_NAME, path) is False
    assert path.read_bytes() == original


def test_unsupported_directory_sync_does_not_negate_replaced_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without directory fsync still reports a reloadable replacement."""
    preferences = importlib.import_module("agentgrep.ui.preferences")
    path = tmp_path / "agentgrep" / "preferences.json"

    def unsupported(_path: pathlib.Path) -> t.NoReturn:
        raise OSError

    monkeypatch.setattr(preferences, "_fsync_directory", unsupported)
    assert preferences.save_theme_name(theme.LIGHT_THEME_NAME, path) is True
    assert preferences.load_theme_name(path) == theme.LIGHT_THEME_NAME


def test_profile_variables_drive_query_highlighting() -> None:
    """Query spans consume the selected profile instead of dark/light globals."""
    from rich.text import Text

    from agentgrep.ui.highlighter import QueryHighlighter

    profile = theme.THEME_PROFILE_BY_NAME["agentgrep-tokyo-night"]
    built = profile.build()
    text = Text("agent:claude OR model:gpt*")
    QueryHighlighter(theme_variables=built.variables).highlight(text)

    field_spans = [span for span in text.spans if text.plain[span.start : span.end] == "agent"]
    assert len(field_spans) == 1
    assert str(field_spans[0].style) == built.variables["ag-query-field"]


def test_each_profile_colors_textual_widget_tokens_from_its_own_accent() -> None:
    """Cursor, footer, and scrollbar roles follow each profile, not polarity."""
    for profile in theme.THEME_PROFILES:
        built = profile.build()
        variables = built.variables
        assert variables["input-cursor-background"] == built.accent
        assert variables["footer-key-foreground"] == built.accent
        assert variables["scrollbar-hover"] == built.accent
        assert variables["scrollbar-background"] == built.surface


def test_each_profile_has_dedicated_find_highlights() -> None:
    """Find and current-find remain distinct from search/filter in every theme."""
    for profile in theme.THEME_PROFILES:
        variables = profile.build().variables
        assert variables["ag-match-find-bg"] != variables["ag-match-find-current-bg"]
        assert variables["ag-match-find-fg"]
        assert variables["ag-match-find-current-fg"]


async def test_picker_previews_and_escape_rolls_back(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Navigation previews a profile, while Escape restores the committed one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.open_theme_picker() is True
        await pilot.pause()
        assert app.screen.id == "theme-picker"
        from textual.widgets import OptionList

        options = app.screen.query_one("#theme-picker-options", OptionList)
        assert options.option_count == len(theme.THEME_PROFILES)

        await pilot.press("j")
        await pilot.pause()
        assert app.theme == theme.LIGHT_THEME_NAME

        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == theme.DARK_THEME_NAME


async def test_runtime_preview_coalesces_hidden_hud_repaint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapid picker previews rebuild hidden Rich surfaces once on commit."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        layout = app.screen
        refreshes: list[None] = []
        monkeypatch.setattr(layout._results, "refresh_theme", lambda: refreshes.append(None))

        app.open_theme_picker()
        await pilot.pause()
        await pilot.press("j", "j")
        await pilot.pause(0.08)
        assert app.theme == theme.TOKYO_NIGHT_THEME_NAME
        assert refreshes == []

        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.theme == theme.TOKYO_NIGHT_THEME_NAME
        assert app.screen is layout
        assert refreshes == [None]


async def test_rapid_preview_then_escape_cannot_reapply_stale_theme(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel invalidates a preview timer even after multiple quick moves."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_theme_picker()
        await pilot.pause()
        await pilot.press("j", "j", "escape")
        await pilot.pause(0.1)
        assert app.theme == theme.DARK_THEME_NAME


async def test_picker_enter_persists_selection_off_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter commits the preview and the shell worker persists it atomically."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.open_theme_picker()
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        preferences = importlib.import_module("agentgrep.ui.preferences")
        assert app.theme == theme.TOKYO_NIGHT_THEME_NAME
        assert preferences.load_theme_name() == theme.TOKYO_NIGHT_THEME_NAME


async def test_picker_fits_a_standard_terminal_and_keeps_plain_hints(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picker stays bounded at 80x24 and NO_COLOR never erases controls."""
    monkeypatch.setenv("NO_COLOR", "1")
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(80, 24)) as pilot:
        app.open_theme_picker()
        await pilot.pause()
        dialog = app.screen.query_one("#theme-picker-dialog")
        navigation = app.screen.query_one("#theme-picker-navigation")
        commit = app.screen.query_one("#theme-picker-commit")
        footer = app.screen.query_one("#theme-picker-footer")
        assert dialog.region.x >= 0
        assert dialog.region.y >= 0
        assert dialog.region.right <= 80
        assert dialog.region.bottom <= 24
        assert "Navigate" in str(navigation.render())
        assert "Enter" in str(commit.render())
        assert "Esc" in str(footer.render())


async def test_picker_preview_inherits_canvas_without_square_corner_fill(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rounded preview border does not sit over a rectangular card fill."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_theme_picker()
        await pilot.pause()
        preview = app.screen.query_one("#theme-picker-preview")
        assert preview.styles.background.a == 0


async def test_rapid_direct_selections_are_serialized_and_latest_wins(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow older write cannot overwrite a newer direct theme selection."""
    preferences = importlib.import_module("agentgrep.ui.preferences")
    real_save = preferences.save_theme_name
    active = 0
    max_active = 0
    lock = threading.Lock()

    def slow_save(theme_name: str, path: pathlib.Path | None = None) -> bool:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if theme_name == theme.LIGHT_THEME_NAME:
                time.sleep(0.05)
            return bool(real_save(theme_name, path))
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(preferences, "save_theme_name", slow_save)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        assert app.select_theme(theme.LIGHT_THEME_NAME) is True
        assert app.select_theme(theme.TOKYO_NIGHT_THEME_NAME) is True
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert max_active == 1
    assert preferences.load_theme_name(app._theme_config_path) == theme.TOKYO_NIGHT_THEME_NAME


async def test_save_failure_keeps_session_theme_and_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistence failure is visible without discarding the chosen session theme."""
    preferences = importlib.import_module("agentgrep.ui.preferences")
    monkeypatch.setattr(preferences, "save_theme_name", lambda *_args, **_kwargs: False)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    notices: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notices.append((args, kwargs)))

    async with app.run_test(size=(80, 24)) as pilot:
        app.open_theme_picker()
        await pilot.pause()
        await pilot.press("j", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.id != "theme-picker"
        assert app.theme == theme.LIGHT_THEME_NAME

    assert notices
    assert "session" in str(notices[-1][0][0]).lower()
    assert notices[-1][1]["severity"] == "warning"


async def test_save_exception_keeps_explorer_usable_and_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker exception is contained without taking down the explorer."""
    preferences = importlib.import_module("agentgrep.ui.preferences")

    def fail_save(*_args: object, **_kwargs: object) -> t.NoReturn:
        raise OSError

    monkeypatch.setattr(preferences, "save_theme_name", fail_save)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    notices: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notices.append((args, kwargs)))

    async with app.run_test(size=(80, 24)) as pilot:
        layout = app.screen
        assert app.select_theme(theme.LIGHT_THEME_NAME) is True
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is layout
        assert app.theme == theme.LIGHT_THEME_NAME

    assert notices
    assert "session" in str(notices[-1][0][0]).lower()
    assert notices[-1][1]["severity"] == "warning"


async def test_first_run_offer_opens_picker_and_saved_theme_loads_before_mount(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup is offered only without a saved profile; saved state wins pre-pump."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from agentgrep.ui.app import build_streaming_ui_app

    home = tmp_path / "home"
    home.mkdir()
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            home,
            query,
            control=agentgrep.SearchControl(),
            _offer_theme_setup=True,
        ),
    )
    assert app.theme == theme.DARK_THEME_NAME
    assert app.get_default_screen().id == "theme-picker"
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.screen.id == "theme-picker"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.id != "theme-picker"

    preferences = importlib.import_module("agentgrep.ui.preferences")
    assert preferences.load_theme_name(app._theme_config_path) is None
    preferences.save_theme_name(theme.TOKYO_NIGHT_THEME_NAME, app._theme_config_path)
    restored = t.cast(
        "t.Any",
        build_streaming_ui_app(
            home,
            query,
            control=agentgrep.SearchControl(),
            _offer_theme_setup=True,
        ),
    )
    assert restored.theme == theme.TOKYO_NIGHT_THEME_NAME
    assert restored.get_default_screen().id != "theme-picker"


async def test_first_run_selection_enters_explorer_then_persists(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup enters the explorer immediately, then saves for the next launch."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from agentgrep.ui.app import build_streaming_ui_app

    home = tmp_path / "home"
    home.mkdir()
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            home,
            query,
            control=agentgrep.SearchControl(),
            _offer_theme_setup=True,
        ),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("j", "j", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.id != "theme-picker"

    preferences = importlib.import_module("agentgrep.ui.preferences")
    assert preferences.load_theme_name(app._theme_config_path) == theme.TOKYO_NIGHT_THEME_NAME
    restored = t.cast(
        "t.Any",
        build_streaming_ui_app(
            home,
            query,
            control=agentgrep.SearchControl(),
            _offer_theme_setup=True,
        ),
    )
    assert restored.theme == theme.TOKYO_NIGHT_THEME_NAME
    assert restored.get_default_screen().id != "theme-picker"
