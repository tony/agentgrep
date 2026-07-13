"""Tests for the pi-lite theme, semantic tokens, and global stylesheet.

Pure tests cover the token maps and computed contrast offline; Pilot tests
confirm both themes register, the stylesheet parses *and applies*, the palette
switches (including to a built-in theme without ``$ag-*`` tokens, which the
``get_theme_variable_defaults`` safety net must keep resolvable), and that
Rich-baked rows re-render against the new palette.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.color import Color
from textual.css.stylesheet import Stylesheet

from agentgrep.records import AGENT_CHOICES
from agentgrep.ui import theme
from tests.test_agentgrep import _build_empty_ui_app, _ui_record, load_agentgrep_module

_STYLESHEET = pathlib.Path(theme.__file__).with_name("styles.tcss")


class ThemeCase(t.NamedTuple):
    """A registered pi-lite theme paired with a readable id."""

    test_id: str
    builder: t.Callable[[], t.Any]


_THEME_CASES: tuple[ThemeCase, ...] = (
    ThemeCase("dark", theme.agentgrep_dark),
    ThemeCase("light", theme.agentgrep_light),
)
_THEME_IDS = [case.test_id for case in _THEME_CASES]


def _relative_luminance(hex6: str) -> float:
    """Return the WCAG relative luminance of a ``#rrggbb`` color."""
    raw = hex6.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG contrast ratio between two ``#rrggbb`` colors."""
    light = _relative_luminance(foreground)
    dark = _relative_luminance(background)
    high, low = max(light, dark), min(light, dark)
    return (high + 0.05) / (low + 0.05)


# --- token-map shape -------------------------------------------------------


def test_agent_token_map_covers_every_agent() -> None:
    """Every searchable agent has a hue token (and no extras)."""
    assert set(theme.AGENT_TOKEN_BY_NAME) == set(AGENT_CHOICES)


def test_theme_builders_use_expected_names() -> None:
    """The builders return themes under the documented names."""
    assert theme.agentgrep_dark().name == theme.DARK_THEME_NAME
    assert theme.agentgrep_light().name == theme.LIGHT_THEME_NAME


def test_resolve_handles_missing_and_present() -> None:
    """``resolve`` yields ``""`` for absent names and the hex for present ones."""
    variables = {"ag-agent-codex": "#00d7ff"}
    assert theme.resolve(variables, None) == ""
    assert theme.resolve(variables, "ag-agent-unknown") == ""
    assert theme.resolve(variables, "ag-agent-codex") == "#00d7ff"


def test_known_agent_kind_hues_preserved() -> None:
    """The previously-styled agents/kinds keep their hue family in dark mode."""
    variables = theme.agentgrep_dark().variables
    assert variables["ag-agent-codex"] == "#00d7ff"
    assert variables["ag-kind-prompt"] == "#b5bd68"
    assert variables["ag-kind-history"] == "#5f87ff"


# --- token presence + parseability per theme -------------------------------


@pytest.mark.parametrize("case", _THEME_CASES, ids=_THEME_IDS)
def test_every_ag_token_present_and_parseable(case: ThemeCase) -> None:
    """All ``$ag-*`` tokens exist and parse as colors in each theme."""
    variables = case.builder().variables
    expected = (
        set(theme.AGENT_TOKEN_BY_NAME.values())
        | set(theme.KIND_TOKEN_BY_NAME.values())
        | {"ag-muted", "ag-dim", "ag-faint", "ag-model"}
        | {f"ag-state-{name}-bg" for name in ("user", "pending", "success", "error", "selected")}
        | {f"ag-on-{name}" for name in ("user", "pending", "success", "error", "selected")}
        | {"ag-match-search", "ag-match-filter-bg", "ag-match-filter-fg"}
    )
    missing = expected - set(variables)
    assert not missing, f"{case.test_id} theme missing tokens: {sorted(missing)}"
    for name in expected:
        Color.parse(variables[name])  # raises ColorParseError on a bad value


@pytest.mark.parametrize("case", _THEME_CASES, ids=_THEME_IDS)
def test_stylesheet_parses_with_theme_variables(case: ThemeCase) -> None:
    """The global stylesheet resolves every token reference in each theme."""
    built = case.builder()
    variables = {**built.to_color_system().generate(), **built.variables}
    stylesheet = Stylesheet(variables=variables)
    source = _STYLESHEET.read_text(encoding="utf-8")
    stylesheet.add_source(source, read_from=(str(_STYLESHEET), str(_STYLESHEET)))
    stylesheet.parse()
    assert stylesheet.rules


# --- computed contrast -----------------------------------------------------


_STATE_TINTS = ("user", "pending", "success", "error", "selected")


@pytest.mark.parametrize("case", _THEME_CASES, ids=_THEME_IDS)
@pytest.mark.parametrize("tint", _STATE_TINTS)
def test_state_tint_foreground_is_readable(case: ThemeCase, tint: str) -> None:
    """Each state tint's computed ``$ag-on-*`` clears WCAG AA (4.5:1)."""
    variables = case.builder().variables
    ratio = _contrast_ratio(variables[f"ag-on-{tint}"], variables[f"ag-state-{tint}-bg"])
    assert ratio >= 4.5, f"{case.test_id}/{tint} contrast {ratio:.2f} below 4.5"


@pytest.mark.parametrize("case", _THEME_CASES, ids=_THEME_IDS)
def test_filter_match_foreground_is_readable(case: ThemeCase) -> None:
    """The filter-match background/foreground clears AA for bold text (3:1)."""
    variables = case.builder().variables
    ratio = _contrast_ratio(variables["ag-match-filter-fg"], variables["ag-match-filter-bg"])
    assert ratio >= 3.0, f"{case.test_id} filter-match contrast {ratio:.2f} below 3.0"


# --- live app: registration, application, switching ------------------------


async def test_pi_themes_registered_and_active(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both pi themes register and the dark theme is active on launch."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert theme.DARK_THEME_NAME in app.available_themes
        assert theme.LIGHT_THEME_NAME in app.available_themes
        assert app.theme == theme.DARK_THEME_NAME


async def test_stylesheet_applies_accent_token(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token-styled widget resolves to the active theme's value, not just parses."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        header = app.screen.query_one("#filter-header")
        # The filter header resolves $accent for its inline search-status
        # spans; the dark accent is the pi teal.
        assert header._c_accent.lower() == "#8abeb7"


async def test_switch_to_light_theme_succeeds(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching to the light theme applies cleanly."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()
        assert app.theme == theme.LIGHT_THEME_NAME
        header = app.screen.query_one("#filter-header")
        # The theme switch re-resolves the filter header's payload hexes.
        assert header._c_accent.lower() == "#5a8080"


async def test_switch_to_builtin_theme_does_not_crash(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built-in theme without ``$ag-*`` still resolves via the defaults net."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # textual-dark defines none of our $ag-* tokens; without the
        # get_theme_variable_defaults fallback this would raise on re-style.
        app.theme = "textual-dark"
        await pilot.pause()
        assert app.theme == "textual-dark"


async def test_theme_switch_rerenders_rows(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rich-baked result rows recolor when the palette switches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        record = _ui_record(agentgrep, tmp_path / "r.jsonl", "codex prompt body", "r")
        app.screen._results._rebuild_options([record])

        def agent_span_styles() -> list[str]:
            option = app.screen._results.get_option_at_index(0)
            return [str(span.style) for span in option.prompt.spans]

        assert any("#00d7ff" in style for style in agent_span_styles())
        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()
        assert any("#0087af" in style for style in agent_span_styles())
