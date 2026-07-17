"""Tests for the interactive TUI welcome canvas."""

from __future__ import annotations

import pathlib

import pytest
from textual.widgets import Static

from agentgrep.query import compile_query, default_registry, parse_query
from agentgrep.ui.layouts.hud import _WELCOME_QUERIES, _welcome_query_examples
from agentgrep.ui.widgets import WelcomeQuerySelected
from tests.test_agentgrep import _build_empty_ui_app


def test_welcome_examples_share_query_highlighting_and_safe_metadata() -> None:
    """Examples retain syntax spans and expose only bounded integer metadata."""
    content = _welcome_query_examples()
    query_indexes = [
        span.style.meta["agentgrep_query_index"]
        for span in content.spans
        if not isinstance(span.style, str) and "agentgrep_query_index" in span.style.meta
    ]

    assert content.plain == (
        'agent:claude   model:gpt*   role:user\ntimestamp:>2026-01-01   "exact phrase"'
    )
    assert len(content.spans) > len(_WELCOME_QUERIES)
    assert query_indexes == list(range(len(_WELCOME_QUERIES)))
    assert all(
        isinstance(span.style, str) or "@click" not in span.style.meta for span in content.spans
    )


@pytest.mark.parametrize("query", _WELCOME_QUERIES)
def test_welcome_query_parses_and_compiles(query: str) -> None:
    """Every clickable example remains valid query-language input."""
    registry = default_registry()
    assert compile_query(parse_query(query, registry), registry) is not None


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
        assert 2 * welcome.region.x + welcome.region.width == (
            2 * examples.region.x + examples.region.width
        )
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


async def test_welcome_examples_wrap_and_click_at_24_columns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every example stays visible and clickable at the narrow supported edge."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(24, 20)) as pilot:
        await pilot.pause()
        layout = app.screen
        welcome = layout.query_one("#empty-welcome", Static)
        examples = layout.query_one("#empty-examples", Static)

        assert welcome.region.width <= 24
        assert examples.region.width <= 24
        targets: dict[int, tuple[int, int]] = {}
        for y in range(examples.region.height):
            x = 0
            for segment in examples.render_line(y):
                if segment.style is not None:
                    index = segment.style.meta.get("agentgrep_query_index")
                    if type(index) is int:
                        targets.setdefault(index, (x, y))
                x += segment.cell_length
        assert set(targets) == set(range(len(_WELCOME_QUERIES)))

        for index, query in enumerate(_WELCOME_QUERIES):
            assert await pilot.click(examples, offset=targets[index])
            await pilot.pause()
            assert layout._search_input.value == query


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
