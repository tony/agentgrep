"""Detail-pane resize contract: the width-baked body re-flows on a resize.

The rendered markdown/code body is flattened to a ``Text`` baked at the pane
width and the render width is part of the LRU cache key, so a terminal resize
that changes the width must re-run the off-pump build (via ``_after_resize`` ->
``show_detail``) rather than leave a stale-width render until the next record
switch.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui


def _empty_query() -> SearchQuery:
    """Build an idle search query (no terms)."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


def _make_record(text: str) -> SearchRecord:
    """Build a prompt record whose body is ``text``."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/home/user/.codex/history.jsonl"),
        text=text,
    )


async def test_detail_body_reflows_on_width_change(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resize that changes the pane width re-renders the baked markdown body."""
    record = _make_record("# Heading\n\nSome markdown body paragraph.\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        first_key = layout._presented_detail_cache_key
        assert first_key is not None
        wider = first_key[-1] + 20  # the 6th key element is the render width

        # Simulate a terminal resize to a wider pane, then run the debounced
        # resize handler (the timer callback) directly.
        monkeypatch.setattr(layout, "_detail_render_width", lambda: wider)
        layout._after_resize()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # The body was rebuilt at the new width (new cache key, width element
        # updated) rather than serving the stale-width render.
        assert layout._presented_detail_cache_key is not None
        assert layout._presented_detail_cache_key[-1] == wider
        assert layout._presented_detail_cache_key != first_key


async def test_resize_without_width_change_is_a_noop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resize that does not change the width does not rebuild the body."""
    record = _make_record("# Heading\n\nSome markdown body paragraph.\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        # Pin the width so the initial render and the resize agree; otherwise
        # the pre-layout fallback width would legitimately differ.
        monkeypatch.setattr(layout, "_detail_render_width", lambda: 100)
        await pilot.pause()
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        before = layout._presented_detail_cache_key
        assert before is not None
        assert before[-1] == 100
        # Width unchanged -> the guard skips the rebuild, key is untouched.
        layout._after_resize()
        await pilot.pause()
        assert layout._presented_detail_cache_key == before
