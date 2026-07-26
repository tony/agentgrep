"""Direct contracts for the Textual-free detail render core."""

from __future__ import annotations

import pytest
from rich.syntax import Syntax
from rich.text import Text

from agentgrep.ui._detail_render import DetailRenderRequest, build_detail_body

pytestmark = pytest.mark.tui


def _request(body_text: str, *, regex: bool = False) -> DetailRenderRequest:
    """Build a render request with explicit, contrasting match styles."""
    return DetailRenderRequest(
        body_text=body_text,
        query_terms=("alpha",),
        case_sensitive=False,
        regex=regex,
        filter_terms=("filter",),
        search_style="bold yellow",
        filter_style="bold black on cyan",
        syntax_theme="ansi_dark",
        render_width=80,
    )


def _span_signature(text: Text) -> list[tuple[int, int, str]]:
    """Return stable offsets and styles for a Rich ``Text``."""
    return [(span.start, span.end, str(span.style)) for span in text.spans]


def test_detail_render_plain_uses_snapshot_styles() -> None:
    """Plain rendering applies only the request's query and filter state."""
    result = build_detail_body(_request("Alpha needle FILTER alpha"))

    assert isinstance(result.renderable, Text)
    assert result.find_source == "Alpha needle FILTER alpha"
    assert result.rendered_plain == "Alpha needle FILTER alpha"
    assert _span_signature(result.renderable) == [
        (0, 5, "bold yellow"),
        (20, 25, "bold yellow"),
        (13, 19, "bold black on cyan"),
    ]


def test_detail_render_regex_omits_literal_query_overlay() -> None:
    """Regex mode does not re-run query expressions during presentation."""
    result = build_detail_body(_request("alpha FILTER", regex=True))

    assert isinstance(result.renderable, Text)
    assert _span_signature(result.renderable) == [
        (6, 12, "bold black on cyan"),
    ]


def test_detail_render_json_projects_pretty_source() -> None:
    """Small JSON uses Syntax while find and copy share its pretty text."""
    result = build_detail_body(_request('{"needle":"filter","alpha":1}'))

    expected = '{\n  "needle": "filter",\n  "alpha": 1\n}'
    assert isinstance(result.renderable, Syntax)
    assert result.find_source == expected
    assert result.rendered_plain == expected


def test_detail_render_markdown_preserves_link_style() -> None:
    """Markdown flattens into selectable text without losing OSC-8 links."""
    url = "https://example.invalid/runbook"
    result = build_detail_body(
        _request(f"# Deploy\n\nRead [the runbook]({url}) before release.\n"),
    )

    assert isinstance(result.renderable, Text)
    assert result.find_source.startswith("# Deploy")
    assert "# Deploy" not in result.rendered_plain
    assert "Deploy" in result.rendered_plain
    assert "Read the runbook before release." in result.rendered_plain
    assert url in {
        getattr(span.style, "link", None)
        for span in result.renderable.spans
        if getattr(span.style, "link", None)
    }
