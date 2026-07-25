"""Detail-pane richtext contract: render, select, copy, raw toggle.

Exercises the four coupled behaviors through a real ``App.run_test`` pilot: a
>2 KiB markdown body renders as a styled (selectable) ``Text`` off the pump, the
body Static is mouse-selectable, the two copy commands populate the clipboard
with the bounded source vs the flattened text, and the ``alt+r`` toggle swaps the
pane to the raw source view.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from rich.text import Text
from textual.geometry import Offset
from textual.selection import Selection

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui


def _markdown_body() -> str:
    """Return a >2 KiB markdown body with headings, fenced code, and lists."""
    block = (
        "## Deploy notes\n"
        "\n"
        "The deployment pipeline runs three stages before promotion. Each\n"
        "stage records its own artifact and a checksum for later audit.\n"
        "\n"
        "- build the wheel\n"
        "- run the smoke suite\n"
        "- publish to the internal index\n"
        "\n"
        "```python\n"
        "def deploy(target: str) -> int:\n"
        '    print(f"deploying {target}")\n'
        "    return 0\n"
        "```\n"
        "\n"
    )
    body = "# Release checklist\n\nIntro paragraph explaining the release.\n\n"
    while len(body) < 3000:
        body += block
    return body


def _make_record(text: str) -> SearchRecord:
    """Build a prompt record whose body is ``text``."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions",
        path=pathlib.Path("/home/user/.codex/sessions/2026/07/22/demo.jsonl"),
        text=text,
    )


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


async def test_detail_pane_markdown_render_select_copy(tmp_path: pathlib.Path) -> None:
    """Rich render + selectable body + copy source/rendered + raw toggle."""
    md_body = _markdown_body()
    assert len(md_body) > 2048
    record = _make_record(md_body)
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
        # Markdown is offloaded: wait for the worker + its call_from_thread
        # present, then flush the pump.
        await app.workers.wait_for_complete()
        await pilot.pause()

        # (1) The body renders RICH (styled Text), not the plain >2048 fallback.
        rendered = layout._detail_rendered_renderable
        assert isinstance(rendered, Text)
        assert rendered.spans, "expected markdown styling spans on the body Text"
        # The fallback returned Text(body_text); a real render strips the ATX
        # markers and code fences, so .plain diverges from the raw source.
        assert layout._detail_rendered_plain != md_body
        assert "```" not in layout._detail_rendered_plain
        assert "# Release checklist" not in layout._detail_rendered_plain
        assert "Release checklist" in layout._detail_rendered_plain

        # (2) The body Static is natively selectable (Text -> Content visual).
        body_widget = layout._detail_body
        selection = Selection.from_offsets(Offset(0, 0), Offset(3, 6))
        assert body_widget.get_selection(selection) is not None

        # (3a) Copy source -> the bounded _detail_body_text (never a fresh
        # encode of the uncapped record.text; here they share a value because
        # the body is under the 64 KiB truncation floor).
        layout._detail_scroll.action_copy_source()
        await pilot.pause()
        assert app.clipboard == layout._detail_body_text
        assert app.clipboard == md_body

        # (3b) Copy rendered -> the flattened markdown text.
        layout._detail_scroll.action_copy_rendered()
        await pilot.pause()
        assert app.clipboard == layout._detail_rendered_plain
        assert "```" not in app.clipboard

        # (4) alt+r toggles to the raw source view.
        assert layout._detail_raw_mode is False
        layout._detail_scroll.action_toggle_raw()
        await pilot.pause()
        assert layout._detail_raw_mode is True
        raw_visual = str(body_widget._render())
        assert "```python" in raw_visual  # raw fences are visible again
        # Toggling back restores the rendered view.
        layout._detail_scroll.action_toggle_raw()
        await pilot.pause()
        assert layout._detail_raw_mode is False
        assert "```" not in str(body_widget._render())


def _python_body() -> str:
    """Return a Python source body (looks_like_code + Pygments-confident)."""
    return (
        '"""Module docstring for the deferred plan API."""\n'
        "from __future__ import annotations\n"
        "import typing as t\n"
        "from dataclasses import dataclass, field\n"
        "\n\n"
        "@dataclass\n"
        "class FakeResult:\n"
        '    """Small command result."""\n'
        "    stdout: list[str] = field(default_factory=list)\n"
        "    returncode: int = 0\n"
        "\n"
        "    def ok(self) -> bool:\n"
        "        # a trailing comment that would trip the ATX-heading rule\n"
        "        return self.returncode == 0\n"
    )


async def test_detail_pane_syntax_highlights_code(tmp_path: pathlib.Path) -> None:
    """A Python body renders as a styled (highlighted) Text off the pump.

    The ``# comment`` line would trip the markdown heading heuristic, so this
    also proves code detection takes precedence over the format heuristic.
    """
    record = _make_record(_python_body())
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

        rendered = layout._detail_rendered_renderable
        assert isinstance(rendered, Text)
        assert rendered.spans, "expected syntax-highlight spans on the code Text"
        assert "def ok(self)" in rendered.plain


async def test_detail_markdown_links_carry_osc8(tmp_path: pathlib.Path) -> None:
    """A markdown link renders with an OSC-8 hyperlink style (terminal-clickable).

    rich.markdown emits an OSC-8 ``link`` style, ``_flatten_markdown`` preserves
    it on the styled ``Text``, and Textual keeps it through
    ``Content.from_rich_text`` (``Style.link``) -- so links are ctrl-clickable in
    an OSC-8 terminal with no extra code. This locks that a flatten refactor can
    not silently drop the link.
    """
    url = "https://example.invalid/runbook"
    record = _make_record(f"# Doc\n\nSee [the runbook]({url}) for details.\n")
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

        rendered = layout._detail_rendered_renderable
        assert isinstance(rendered, Text)
        links = {span.style.link for span in rendered.spans if getattr(span.style, "link", None)}
        assert url in links
