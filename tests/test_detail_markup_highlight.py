"""Detail-pane markup highlighting: shared span lexer + structural gate.

The detail pane styles the structural tags of XML/markup-shaped prompt bodies
(``<EPHEMERAL_MESSAGE>``, ``<bash_command_reminder>``, closers) while leaving
the surrounding prose plain. The grammar is lexed once by
:func:`agentgrep.highlight_markup_spans` (mirroring the query-language lexer)
and applied by offset in the offload worker, so ``Text.plain`` never changes.
A paired-tag structural gate (:func:`agentgrep._text.looks_like_markup`) keeps
generics and comparisons (``List<int>``, ``a < b``) out.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from rich.text import Text

from agentgrep import highlight_markup_spans
from agentgrep._text import looks_like_markup
from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui


EPHEMERAL_BODY = (
    "The following is an <EPHEMERAL_MESSAGE> not actually sent by the user. It "
    "is provided by the system as reminders. Do NOT respond to this message, "
    "just act accordingly.\n"
    "\n"
    "<EPHEMERAL_MESSAGE>\n"
    "<bash_command_reminder>\n"
    "CRITICAL INSTRUCTION: Always prioritize the most specific tool for the "
    "task at hand. Never run cat inside a bash command to create a file. Prefer "
    "grep_search over running grep. Think before making tool calls and list "
    "related tools.\n"
    "</bash_command_reminder>\n"
    "</EPHEMERAL_MESSAGE>\n"
)

CONTROL_BODY = (
    "The parser builds a List<int> from the token stream. When a < b and x > y, "
    "the comparison holds. This is ordinary prose about generics and "
    "comparisons, no markup here."
)


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


def test_highlight_markup_spans_and_gate() -> None:
    """The lexer covers a tag end to end; the gate accepts markup, rejects prose."""
    spans = highlight_markup_spans("<a>x</a>")
    # End-to-end, in-order coverage: spans reconstruct the source exactly.
    assert "".join(token for _, _, token in spans) == "<a>x</a>"
    starts = [start for start, _, _ in spans]
    assert starts == sorted(starts)
    roles = {role for _, role, _ in spans}
    assert "tag-delim" in roles
    assert "tag-name" in roles

    # Structural gate: paired tags qualify, sparse prose generics do not.
    assert looks_like_markup(EPHEMERAL_BODY) is True
    assert looks_like_markup(CONTROL_BODY) is False

    # Tag-context aware: attribute roles fire only inside a tag. A prose
    # ``key = value`` (even inside a body the gate accepted) stays text, while
    # a real attribute is styled.
    assert {role for _, role, _ in highlight_markup_spans("key = value")} == {
        "text",
    }
    in_tag_roles = {role for _, role, _ in highlight_markup_spans('<t k="v">')}
    assert "attr-name" in in_tag_roles
    assert "attr-value" in in_tag_roles


async def test_detail_pane_styles_markup_tags(tmp_path: pathlib.Path) -> None:
    """A prose-heavy markup body renders styled tags with byte-identical plain."""
    record = _make_record(EPHEMERAL_BODY)
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
        # Style-only overlay: the flattened text is byte-identical to the body.
        assert rendered.plain == layout._detail_body_text
        assert rendered.plain == EPHEMERAL_BODY

        # A styling span covers a `<EPHEMERAL_MESSAGE>` occurrence.
        tag = "<EPHEMERAL_MESSAGE>"
        tag_start = EPHEMERAL_BODY.index(tag)
        tag_end = tag_start + len(tag)
        covering = [
            span
            for span in rendered.spans
            if span.start >= tag_start and span.end <= tag_end and str(span.style)
        ]
        assert covering, "expected non-empty style spans over the EPHEMERAL tag"


async def test_detail_pane_leaves_prose_unstyled(tmp_path: pathlib.Path) -> None:
    """A generics/comparison body stays plain: the structural gate holds."""
    record = _make_record(CONTROL_BODY)
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
        assert rendered.plain == CONTROL_BODY
        # Empty query -> no search/filter spans, and the gate suppressed markup.
        assert rendered.spans == []
