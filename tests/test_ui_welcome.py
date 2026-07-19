"""Tests for the interactive TUI welcome canvas."""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Static

from agentgrep.query import build_query_from_input, compile_query, default_registry, parse_query
from agentgrep.records import SearchQuery
from agentgrep.ui.widgets import WelcomeQuerySelected
from agentgrep.ui.widgets.welcome import (
    _WELCOME_QUERIES,
    _WELCOME_SHINE_INTERVAL,
    _welcome_query_examples,
    _welcome_wordmark,
)
from tests.test_agentgrep_tui import _build_empty_ui_app


def _welcome_click_targets(examples: Static) -> dict[int, tuple[int, int]]:
    """Map each rendered welcome-query index to a clickable cell."""
    targets: dict[int, tuple[int, int]] = {}
    for y in range(examples.region.height):
        x = 0
        for segment in examples.render_line(y):
            if segment.style is not None:
                index = segment.style.meta.get("agentgrep_query_index")
                if type(index) is int:
                    targets.setdefault(index, (x, y))
            x += segment.cell_length
    return targets


async def _wait_for_wordmark_change(
    pilot: t.Any,
    welcome: Static,
    before: tuple[str, ...],
) -> tuple[str, ...]:
    """Wait through a bounded number of timer skips for a changed frame."""
    current = before
    for _ in range(6):
        await pilot.pause(_WELCOME_SHINE_INTERVAL)
        rendered = t.cast("Content", welcome.render())
        current = tuple(str(span.style) for span in rendered.spans)
        if current != before:
            return current
    pytest.fail("welcome shine did not advance")


def test_welcome_examples_share_query_highlighting_and_safe_metadata() -> None:
    """Examples retain syntax spans and expose only bounded integer metadata."""
    content = _welcome_query_examples()
    query_indexes = [
        span.style.meta["agentgrep_query_index"]
        for span in content.spans
        if not isinstance(span.style, str) and "agentgrep_query_index" in span.style.meta
    ]

    assert content.plain == (
        'agent:claude   scope:all model:gpt*   role:user\ntimestamp:>2026-01-01   "exact phrase"'
    )
    assert len(content.spans) > len(_WELCOME_QUERIES)
    assert query_indexes == list(range(len(_WELCOME_QUERIES)))
    assert all(
        isinstance(span.style, str) or "@click" not in span.style.meta for span in content.spans
    )


def test_welcome_wordmark_uses_a_symmetric_brand_shine() -> None:
    """The brand starts as legible text with a restrained color ramp."""
    content = _welcome_wordmark()
    brand_spans = content.spans[-9:]

    assert content.plain == "Welcome to agentgrep"
    assert [span.end - span.start for span in brand_spans] == [1] * 9
    assert [str(span.style) for span in brand_spans] == [
        "bold $ag-brand-shine-1",
        "bold $ag-brand-shine-2",
        "bold $ag-brand-shine-3",
        "bold $ag-brand-shine-4",
        "bold $ag-brand-shine-5",
        "bold $ag-brand-shine-4",
        "bold $ag-brand-shine-3",
        "bold $ag-brand-shine-2",
        "bold $ag-brand-shine-1",
    ]


def test_welcome_wordmark_shifts_shine_without_changing_text() -> None:
    """Animation advances semantic colors without changing the message."""
    initial = _welcome_wordmark()
    shifted = _welcome_wordmark(1)

    assert shifted.plain == initial.plain
    assert [str(span.style) for span in shifted.spans] != [
        str(span.style) for span in initial.spans
    ]


async def test_welcome_wordmark_animates_only_on_empty_canvas(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warm shine pauses off-canvas and resumes with the welcome state."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.animation_level = "full"

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        layout = app.screen
        welcome = layout.query_one("#empty-welcome", Static)

        before = tuple(str(span.style) for span in welcome.render().spans)
        await _wait_for_wordmark_change(pilot, welcome, before)

        layout._set_results_view("results")
        paused = tuple(str(span.style) for span in welcome.render().spans)
        layout._animate_welcome_wordmark()
        assert tuple(str(span.style) for span in welcome.render().spans) == paused
        await pilot.pause(_WELCOME_SHINE_INTERVAL * 2)
        assert tuple(str(span.style) for span in welcome.render().spans) == paused

        layout._set_results_view("empty")
        await _wait_for_wordmark_change(pilot, welcome, paused)


async def test_welcome_wordmark_pauses_under_a_covering_screen(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A covered welcome canvas does not spend idle repaint budget."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.animation_level = "full"

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        layout = app.screen
        welcome = layout.query_one("#empty-welcome", Static)
        await app.push_screen(Screen())
        await pilot.pause()

        covered = tuple(str(span.style) for span in welcome.render().spans)
        await pilot.pause(_WELCOME_SHINE_INTERVAL * 2)
        assert tuple(str(span.style) for span in welcome.render().spans) == covered

        app.pop_screen()
        await _wait_for_wordmark_change(pilot, welcome, covered)


@pytest.mark.parametrize("animation_level", ["none", "basic"])
async def test_welcome_wordmark_respects_reduced_animation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    animation_level: t.Literal["none", "basic"],
) -> None:
    """Reduced-animation modes keep the decorative wordmark still."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.animation_level = animation_level

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        layout = app.screen
        welcome = layout.query_one("#empty-welcome", Static)
        before = tuple(str(span.style) for span in welcome.render().spans)

        layout._animate_welcome_wordmark()
        await pilot.pause(_WELCOME_SHINE_INTERVAL * 2)
        assert tuple(str(span.style) for span in welcome.render().spans) == before


async def test_welcome_wordmark_skips_frames_while_app_is_blurred(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inactive terminal pane does not repaint decorative frames."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.animation_level = "full"

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        layout = app.screen
        welcome = layout.query_one("#empty-welcome", Static)
        app.app_focus = False
        before = tuple(str(span.style) for span in welcome.render().spans)

        layout._animate_welcome_wordmark()
        assert tuple(str(span.style) for span in welcome.render().spans) == before

        app.app_focus = True
        layout._animate_welcome_wordmark()
        assert tuple(str(span.style) for span in welcome.render().spans) != before


@pytest.mark.parametrize("query", _WELCOME_QUERIES)
def test_welcome_query_parses_and_compiles(query: str) -> None:
    """Every clickable example remains valid query-language input."""
    registry = default_registry()
    assert compile_query(parse_query(query, registry), registry) is not None


def test_welcome_model_example_widens_prompt_discovery() -> None:
    """The model example opts into stores whose records actually carry models."""
    model_query = next(query for query in _WELCOME_QUERIES if "model:" in query)
    base = SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )

    result = build_query_from_input(model_query, base, default_registry())

    assert result.error is None
    assert result.query is not None
    assert result.query.scope == "all"


@pytest.mark.parametrize("size", [(100, 28), (40, 20)], ids=["wide", "narrow"])
async def test_welcome_example_click_loads_without_searching(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    """Clicking a highlighted example fills and focuses the explicit input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    spawned: list[tuple[object, ...]] = []

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        layout = app.screen
        monkeypatch.setattr(
            layout,
            "run_worker",
            lambda *args, **_kwargs: spawned.append(args),
        )
        welcome = layout.query_one("#empty-welcome", Static)
        examples = layout.query_one("#empty-examples", Static)

        assert welcome.render().plain == "Welcome to agentgrep"
        assert examples.render().plain.startswith("agent:claude")
        welcome_center = 2 * welcome.region.x + welcome.region.width
        examples_center = 2 * examples.region.x + examples.region.width
        assert abs(welcome_center - examples_center) <= 1
        syntax_colors = {
            str(segment.style.color)
            for segment in examples.render_line(0)
            if segment.text.strip() and segment.style is not None
        }
        assert len(syntax_colors) >= 4
        assert await pilot.click(examples, offset=(2, 0))
        await pilot.pause()

        assert layout._search_input.value == "agent:claude"
        assert layout._search_input.cursor_position == len("agent:claude")
        assert app.focused is layout._search_input
        assert layout._body.has_class("-empty")
        assert spawned == []
        assert layout._search_input.border_subtitle == (
            "Press [bold $accent]Enter[/bold $accent] ↵"
        )

        await pilot.press("enter")
        await pilot.pause()
        search_workers = [
            args for args in spawned if getattr(args[0], "__name__", "") == "_run_search"
        ]
        assert len(search_workers) == 1


@pytest.mark.parametrize(
    "size",
    [(24, 20), (30, 12), (20, 18), (16, 20)],
    ids=["narrow-width", "short-height", "boundary", "compact-both"],
)
async def test_welcome_examples_fit_and_click_at_compact_sizes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    """Every example stays visible and clickable at compact supported edges."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        layout = app.screen
        welcome = layout.query_one("#empty-welcome", Static)
        examples = layout.query_one("#empty-examples", Static)

        assert welcome.region.width <= size[0]
        assert examples.region.width <= size[0]
        targets = _welcome_click_targets(examples)
        assert set(targets) == set(range(len(_WELCOME_QUERIES)))

        for index, query in enumerate(_WELCOME_QUERIES):
            assert await pilot.click(examples, offset=targets[index])
            await pilot.pause()
            assert layout._search_input.value == query


async def test_welcome_compact_classes_follow_live_resize(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compact welcome geometry follows both dimensions and restores on resize."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        layout = app.screen
        assert not layout.has_class("-compact-width")
        assert not layout.has_class("-compact-height")

        for width, height, compact_class in (
            (30, 12, "-compact-height"),
            (20, 18, "-compact-height"),
            (16, 20, "-compact-width"),
        ):
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.1)
            assert layout.has_class(compact_class)
            examples = layout.query_one("#empty-examples", Static)
            targets = _welcome_click_targets(examples)
            assert set(targets) == set(range(len(_WELCOME_QUERIES)))
            for index, query in enumerate(_WELCOME_QUERIES):
                assert await pilot.click(examples, offset=targets[index])
                await pilot.pause()
                assert layout._search_input.value == query

        await pilot.resize_terminal(100, 28)
        await pilot.pause(0.1)
        assert not layout.has_class("-compact-width")
        assert not layout.has_class("-compact-height")


async def test_welcome_example_rejects_invalid_index(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed broker action cannot select outside the fixed examples."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.pause()
        layout = app.screen
        layout._search_input.load_query("draft")

        layout.on_welcome_query_selected(WelcomeQuerySelected(-1))
        layout.on_welcome_query_selected(WelcomeQuerySelected(len(_WELCOME_QUERIES)))

        assert layout._search_input.value == "draft"
