"""Tests for grep --style=pretty output.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.cli.parser import GrepArgs
from agentgrep.cli.render import (
    GrepSummary,
    format_grep_record,
    format_grep_record_pretty,
)

_NO_COLORS = agentgrep.AnsiColors(enabled=False)


def _make_grep_args(**overrides: t.Any) -> GrepArgs:
    defaults: dict[str, t.Any] = {
        "patterns": ("streaming",),
        "agents": ("codex", "claude", "cursor-cli", "gemini"),
        "scope": "prompts",
        "case_mode": "smart",
        "pattern_mode": "fixed",
        "invert_match": False,
        "count_only": False,
        "files_with_matches": False,
        "only_matching": False,
        "no_dedupe": False,
        "line_number": None,
        "heading": None,
        "limit": None,
        "vimgrep": False,
        "column": False,
        "output_mode": "text",
        "color_mode": "never",
        "progress_mode": "never",
        "style": "pretty",
    }
    defaults.update(overrides)
    return GrepArgs(**defaults)


def _make_record(**kwargs: t.Any) -> agentgrep.SearchRecord:
    defaults: dict[str, t.Any] = {
        "kind": "prompt",
        "agent": "claude",
        "store": "claude.projects",
        "adapter_id": "claude.project_jsonl.v1",
        "path": pathlib.Path("/home/user/.claude/sessions/abc.jsonl"),
        "text": "How do I implement streaming in Python?",
    }
    defaults.update(kwargs)
    return agentgrep.SearchRecord(**defaults)


# ---------------------------------------------------------------------------
# format_grep_record_pretty
# ---------------------------------------------------------------------------


class PrettyRecordCase(t.NamedTuple):
    """Parametrized case for pretty record formatting."""

    test_id: str
    record_kwargs: dict[str, t.Any]
    patterns: tuple[str, ...]
    expected_contains: tuple[str, ...]
    expected_not_contains: tuple[str, ...]


_PRETTY_CASES: tuple[PrettyRecordCase, ...] = (
    PrettyRecordCase(
        test_id="snippet-first-provenance-second",
        record_kwargs={
            "text": "streaming parser for JSONL",
            "timestamp": "2026-05-22T14:30:00Z",
        },
        patterns=("streaming",),
        expected_contains=("streaming parser for JSONL", "claude", "prompt"),
        expected_not_contains=(),
    ),
    PrettyRecordCase(
        test_id="no-timestamp-omitted",
        record_kwargs={"text": "some text"},
        patterns=(),
        expected_contains=("some text", "claude", "prompt"),
        expected_not_contains=("ago",),
    ),
    PrettyRecordCase(
        test_id="model-shown-when-present",
        record_kwargs={"text": "content", "model": "claude-sonnet-4"},
        patterns=(),
        expected_contains=("claude-sonnet-4",),
        expected_not_contains=(),
    ),
    PrettyRecordCase(
        test_id="long-text-truncated",
        record_kwargs={"text": "\n".join(f"line {i}" for i in range(1, 21))},
        patterns=("line 1",),
        expected_contains=("more lines",),
        expected_not_contains=(),
    ),
    PrettyRecordCase(
        test_id="empty-text-provenance-only",
        record_kwargs={"text": ""},
        patterns=(),
        expected_contains=("claude", "prompt"),
        expected_not_contains=("more lines",),
    ),
)


@pytest.mark.parametrize("case", _PRETTY_CASES, ids=[c.test_id for c in _PRETTY_CASES])
def test_format_grep_record_pretty(case: PrettyRecordCase) -> None:
    """Pretty formatter produces expected layout elements."""
    record = _make_record(**case.record_kwargs)
    args = _make_grep_args(patterns=case.patterns)
    colors = agentgrep.AnsiColors(enabled=False)
    result = format_grep_record_pretty(record, args, colors=colors)
    for expected in case.expected_contains:
        assert expected in result, f"Expected {expected!r} in result"
    for unexpected in case.expected_not_contains:
        assert unexpected not in result, f"Unexpected {unexpected!r}"


def test_pretty_content_before_provenance() -> None:
    """Content appears before provenance line in pretty output."""
    record = _make_record(
        text="first line of content",
        timestamp="2026-05-22T14:30:00Z",
    )
    args = _make_grep_args()
    result = format_grep_record_pretty(record, args, colors=_NO_COLORS)
    lines = result.split("\n")
    content_idx = next(i for i, line in enumerate(lines) if "first line of content" in line)
    provenance_idx = next(
        i for i, line in enumerate(lines) if "claude" in line and "prompt" in line
    )
    assert content_idx < provenance_idx


def test_pretty_provenance_indented() -> None:
    """Provenance line has 2-space indent."""
    record = _make_record(text="content")
    args = _make_grep_args()
    result = format_grep_record_pretty(record, args, colors=_NO_COLORS)
    provenance = [line for line in result.split("\n") if "claude" in line and "prompt" in line]
    assert provenance
    assert provenance[0].startswith("  ")


# ---------------------------------------------------------------------------
# Style dispatch
# ---------------------------------------------------------------------------


def test_style_default_uses_rg_format() -> None:
    """format_grep_record with style=default produces rg-faithful output."""
    record = _make_record(text="streaming is great")
    args = _make_grep_args(style="default")
    result = format_grep_record(record, args)
    assert "streaming" in result
    assert "  " not in result.split("\n")[-1] or "prompt" not in result


def test_style_pretty_dispatches_to_pretty_formatter() -> None:
    """format_grep_record with style=pretty uses snippet-first format."""
    record = _make_record(
        text="streaming is great",
        timestamp="2026-05-22T14:30:00Z",
    )
    args = _make_grep_args(style="pretty")
    result = format_grep_record(record, args)
    assert "streaming is great" in result
    lines = result.split("\n")
    provenance = [line for line in lines if "claude" in line and "prompt" in line]
    assert provenance
    assert provenance[0].startswith("  ")


def test_only_matching_overrides_pretty() -> None:
    """--only-matching takes precedence over --style=pretty."""
    record = _make_record(text="find the streaming needle here")
    args = _make_grep_args(style="pretty", only_matching=True, pattern_mode="fixed")
    result = format_grep_record(record, args)
    assert result == "streaming"


def test_vimgrep_overrides_pretty() -> None:
    """--vimgrep takes precedence over --style=pretty."""
    record = _make_record(text="streaming is great")
    args = _make_grep_args(style="pretty", vimgrep=True, pattern_mode="fixed")
    result = format_grep_record(record, args)
    assert ":1:" in result


# ---------------------------------------------------------------------------
# GrepSummary
# ---------------------------------------------------------------------------


class SummaryCase(t.NamedTuple):
    """Parametrized case for summary formatting."""

    test_id: str
    agents: list[str]
    elapsed: float
    expected_contains: tuple[str, ...]


_SUMMARY_CASES: tuple[SummaryCase, ...] = (
    SummaryCase(
        test_id="single-agent",
        agents=["claude", "claude", "claude"],
        elapsed=0.5,
        expected_contains=("3 records", "3 claude", "0.5s"),
    ),
    SummaryCase(
        test_id="multi-agent",
        agents=["claude", "codex", "claude", "gemini"],
        elapsed=1.23,
        expected_contains=("4 records", "2 claude", "1 codex", "1 gemini", "1.2s"),
    ),
)


@pytest.mark.parametrize("case", _SUMMARY_CASES, ids=[c.test_id for c in _SUMMARY_CASES])
def test_grep_summary_format(case: SummaryCase) -> None:
    """GrepSummary.format produces expected footer content."""
    summary = GrepSummary()
    for agent in case.agents:
        record = _make_record(agent=agent)
        summary.add(record)
    summary.elapsed = case.elapsed
    result = summary.format(colors=_NO_COLORS)
    for expected in case.expected_contains:
        assert expected in result, f"Expected {expected!r} in summary"


def test_grep_summary_empty() -> None:
    """Summary with zero records returns empty string."""
    summary = GrepSummary()
    assert summary.format(colors=_NO_COLORS) == ""
