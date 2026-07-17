"""Tests for the pi-lite theme, semantic tokens, and global stylesheet.

Pure tests cover the token maps and computed contrast offline; Pilot tests
confirm both themes register, the stylesheet parses *and applies*, theme
switching remains safe (including to a built-in theme without ``$ag-*`` tokens, which the
``get_theme_variable_defaults`` safety net must keep resolvable), and that
Rich-baked rows re-render against the new palette.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.color import Color
from textual.css.stylesheet import Stylesheet

from agentgrep.records import AGENT_CHOICES
from agentgrep.ui import theme
from agentgrep.ui.highlighter import QueryHighlighter
from agentgrep.ui.widgets import WELCOME_QUERY_INDEX_META
from tests.test_agentgrep import _build_empty_ui_app, _ui_record, load_agentgrep_module

_STYLESHEET = pathlib.Path(theme.__file__).with_name("styles.tcss")


def _set_records(results: t.Any, records: t.Iterable[t.Any]) -> None:
    """Adopt one test-prepared result model."""
    prepared = list(records)
    results.set_records(
        prepared,
        record_ids={id(record) for record in prepared},
    )


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
        | {"ag-canvas", "ag-canvas-text"}
        | {f"ag-state-{name}-bg" for name in ("user", "pending", "success", "error", "selected")}
        | {f"ag-on-{name}" for name in ("user", "pending", "success", "error", "selected")}
        | {"ag-match-search", "ag-match-filter-bg", "ag-match-filter-fg"}
        | {f"ag-brand-shine-{step}" for step in range(1, 6)}
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


@pytest.mark.parametrize("case", _THEME_CASES, ids=_THEME_IDS)
@pytest.mark.parametrize("step", range(1, 6))
def test_brand_shine_is_readable(case: ThemeCase, step: int) -> None:
    """Every wordmark color clears WCAG AA against its theme background."""
    built = case.builder()
    foreground = built.variables[f"ag-brand-shine-{step}"]
    ratio = _contrast_ratio(foreground, built.background)
    assert ratio >= 4.5, f"{case.test_id}/shine-{step} contrast {ratio:.2f} below 4.5"


@pytest.mark.parametrize("case", _THEME_CASES, ids=_THEME_IDS)
def test_query_highlighter_palette_is_readable(case: ThemeCase) -> None:
    """Every query-syntax foreground clears WCAG AA on its theme page."""
    built = case.builder()
    text = Text('-agent:codex OR model:gpt* timestamp:>2026-01-01 "exact phrase"')
    QueryHighlighter(dark=built.dark).highlight(text)

    for span in text.spans:
        color = Style.parse(str(span.style)).color
        assert color is not None
        triplet = color.get_truecolor()
        foreground = f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"
        ratio = _contrast_ratio(foreground, built.background)
        assert ratio >= 4.5, f"{case.test_id}/{span.style} contrast {ratio:.2f} below 4.5"


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


async def test_theme_switch_recolors_shared_query_highlighting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search, filter, and welcome examples repaint from one light palette."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen._search_input
        filter_input = app.screen._filter_input
        examples = app.screen.query_one("#empty-examples")
        search.value = "agent:claude"
        filter_input.value = "agent:claude"
        search.cursor_position = 5
        filter_input.cursor_position = 3
        await pilot.pause()

        assert any("color(79)" in str(span.style) for span in search._value.spans)
        assert any("rgb(95,215,175)" in str(span.style) for span in examples.render().spans)

        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()

        for widget in (search, filter_input):
            assert any("#006b75" in str(span.style) for span in widget._value.spans)
        assert any("rgb(0,107,117)" in str(span.style) for span in examples.render().spans)
        assert (search.value, search.cursor_position) == ("agent:claude", 5)
        assert (filter_input.value, filter_input.cursor_position) == ("agent:claude", 3)
        assert {
            span.style.meta[WELCOME_QUERY_INDEX_META]
            for span in examples.render().spans
            if not isinstance(span.style, str) and WELCOME_QUERY_INDEX_META in span.style.meta
        } == set(range(5))

        app.theme = theme.DARK_THEME_NAME
        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()
        assert any("#006b75" in str(span.style) for span in search._value.spans)
        assert not any("color(79)" in str(span.style) for span in search._value.spans)


async def test_themes_composite_queries_against_matching_canvases(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Light paints its canvas while dark restores the terminal background."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.animation_level = "none"

    async with app.run_test(size=(60, 22)) as pilot:
        await pilot.pause()
        search = app.screen._search_input
        search.value = "/theme light"
        search.focus()
        await pilot.press("enter")
        search.value = "agent:claude"
        await pilot.pause()

        def rendered_query_segments() -> list[Segment]:
            update = app.screen._compositor.render_full_update()
            return [
                segment
                for y, strips in enumerate(update.strips)
                if search.region.y <= y < search.region.bottom
                for item in strips
                for segment in ((item,) if isinstance(item, Segment) else item)
                if segment.text.strip() in {"agent", ":", "claude"}
            ]

        assert app.theme == theme.LIGHT_THEME_NAME
        assert app.ansi_color is True
        query_segments = rendered_query_segments()
        assert query_segments
        for segment in query_segments:
            assert segment.style is not None
            foreground = segment.style.color
            background = segment.style.bgcolor
            assert foreground is not None
            assert background is not None
            foreground_rgb = foreground.get_truecolor()
            background_rgb = background.get_truecolor()
            foreground_hex = (
                f"#{foreground_rgb.red:02x}{foreground_rgb.green:02x}{foreground_rgb.blue:02x}"
            )
            background_hex = (
                f"#{background_rgb.red:02x}{background_rgb.green:02x}{background_rgb.blue:02x}"
            )
            assert _contrast_ratio(foreground_hex, background_hex) >= 4.5

        search.value = "/theme dark"
        await pilot.press("enter")
        search.value = "agent:claude"
        await pilot.pause()

        assert app.theme == theme.DARK_THEME_NAME
        assert app.ansi_color is True
        dark_segments = rendered_query_segments()
        assert dark_segments
        assert all(
            segment.style is not None
            and segment.style.bgcolor is not None
            and segment.style.bgcolor.is_default
            for segment in dark_segments
        )


@pytest.mark.parametrize(
    ("theme_name", "field_style"),
    (("textual-dark", "color(79)"), ("textual-light", "#006b75")),
)
async def test_switch_to_builtin_theme_does_not_crash(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    theme_name: str,
    field_style: str,
) -> None:
    """Built-in themes use defaults plus the matching query palette."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # Built-in themes define none of our $ag-* tokens; without the
        # get_theme_variable_defaults fallback this would raise on re-style.
        app.screen._search_input.value = "agent:claude"
        app.theme = theme_name
        await pilot.pause()
        assert app.theme == theme_name
        assert any(field_style in str(span.style) for span in app.screen._search_input._value.spans)


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
        _set_records(app.screen._results, [record])

        def agent_span_styles() -> list[str]:
            row = app.screen._results._render_record(record)
            return [str(span.style) for span in row.spans]

        assert any("#00d7ff" in style for style in agent_span_styles())
        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()
        assert any("#0087af" in style for style in agent_span_styles())


async def test_theme_switch_invalidates_filtered_out_row_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows hidden during a theme switch recolor when filtering widens."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        records = [
            _ui_record(
                agentgrep,
                tmp_path / f"filtered-{index}.jsonl",
                f"codex prompt body {index}",
                f"filtered-{index}",
            )
            for index in range(2)
        ]
        results = app.screen._results
        _set_records(results, records)
        _set_records(results, records[:1])

        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()
        _set_records(results, records)

        for record in records:
            row = results._render_record(record)
            styles = [str(span.style) for span in row.spans]
            assert any("#0087af" in style for style in styles)
            assert not any("#00d7ff" in style for style in styles)


async def test_theme_switch_rebuilds_only_visible_rows(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large palette switch rebuilds no more than the visible viewport."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        records = [
            _ui_record(
                agentgrep,
                tmp_path / f"r{index}.jsonl",
                f"codex prompt body {index}",
                f"r{index}",
            )
            for index in range(401)
        ]
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        results = app.screen._results
        _set_records(results, records)
        await pilot.pause()
        built = 0
        original = results._build_row

        def count_build(record: t.Any) -> t.Any:
            nonlocal built
            built += 1
            return original(record)

        monkeypatch.setattr(results, "_build_row", count_build)
        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()

        assert 0 < built <= results.size.height
        assert results.option_count == len(records)
        styles = [str(segment.style) for segment in results.render_line(0)]
        assert any("#0087af" in style for style in styles)


async def test_rapid_theme_switch_renders_the_latest_palette(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-to-back palette switches leave visible rows on the latest theme."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        records = [
            _ui_record(
                agentgrep,
                tmp_path / f"rapid-{index}.jsonl",
                f"codex prompt body {index}",
                f"rapid-{index}",
            )
            for index in range(201)
        ]
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        _set_records(app.screen._results, records)
        await pilot.pause()

        app.theme = theme.LIGHT_THEME_NAME
        app.theme = theme.DARK_THEME_NAME
        await pilot.pause()

        assert app.screen._results.option_count == len(records)
        styles = [str(segment.style) for segment in app.screen._results.render_line(0)]
        assert any("#00d7ff" in style for style in styles)
        assert not any("#0087af" in style for style in styles)
