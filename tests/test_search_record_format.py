"""Tests for format_search_record and SearchSummary.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import pathlib
import re
import typing as t

import pytest

import agentgrep
from agentgrep.cli.render import SearchSummary, format_search_record

_NO_COLORS = agentgrep.AnsiColors(enabled=False)


def _make_record(**kwargs: t.Any) -> agentgrep.SearchRecord:
    defaults: dict[str, t.Any] = {
        "kind": "prompt",
        "agent": "claude",
        "store": "claude.projects",
        "adapter_id": "claude.project_jsonl.v1",
        "path": pathlib.PurePosixPath("/home/user/.claude/sessions/abc.jsonl"),
        "text": "How do I implement streaming?",
    }
    defaults.update(kwargs)
    return agentgrep.SearchRecord(**defaults)


# ---------------------------------------------------------------------------
# format_search_record
# ---------------------------------------------------------------------------


class FormatRecordCase(t.NamedTuple):
    """Parametrized case for record formatting."""

    test_id: str
    record_kwargs: dict[str, t.Any]
    terms: tuple[str, ...]
    expected_contains: tuple[str, ...]
    expected_not_contains: tuple[str, ...]


_FORMAT_CASES: tuple[FormatRecordCase, ...] = (
    FormatRecordCase(
        test_id="basic-record-snippet-first",
        record_kwargs={"text": "streaming parser", "timestamp": "2026-05-22T14:30:00Z"},
        terms=("streaming",),
        expected_contains=("streaming parser", "claude", "prompt"),
        expected_not_contains=(),
    ),
    FormatRecordCase(
        test_id="no-timestamp",
        record_kwargs={"text": "some text"},
        terms=(),
        expected_contains=("some text", "claude", "prompt"),
        expected_not_contains=("ago",),
    ),
    FormatRecordCase(
        test_id="no-model",
        record_kwargs={"text": "some text", "model": None},
        terms=(),
        expected_contains=("some text",),
        expected_not_contains=(),
    ),
    FormatRecordCase(
        test_id="with-model",
        record_kwargs={"text": "some text", "model": "claude-sonnet-4"},
        terms=(),
        expected_contains=("claude-sonnet-4",),
        expected_not_contains=(),
    ),
    FormatRecordCase(
        test_id="empty-text-provenance-only",
        record_kwargs={"text": ""},
        terms=(),
        expected_contains=("claude", "prompt"),
        expected_not_contains=("more lines",),
    ),
    FormatRecordCase(
        test_id="long-text-truncated",
        record_kwargs={"text": "\n".join(f"line {i}" for i in range(1, 21))},
        terms=("line 1",),
        expected_contains=("more lines",),
        expected_not_contains=(),
    ),
)


@pytest.mark.parametrize("case", _FORMAT_CASES, ids=[c.test_id for c in _FORMAT_CASES])
def test_format_search_record(case: FormatRecordCase) -> None:
    """format_search_record produces expected layout elements."""
    from agentgrep.cli.parser import SearchArgs

    record = _make_record(**case.record_kwargs)
    args = SearchArgs(
        terms=case.terms,
        agents=("codex", "claude", "cursor", "gemini"),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
    )
    patterns = [re.compile(re.escape(t_)) for t_ in case.terms if ":" not in t_]
    result = format_search_record(record, args, colors=_NO_COLORS, patterns=patterns)
    for expected in case.expected_contains:
        assert expected in result, f"Expected {expected!r} in result"
    for unexpected in case.expected_not_contains:
        assert unexpected not in result, f"Did not expect {unexpected!r} in result"


def test_snippet_first_layout() -> None:
    """Content appears before provenance in the output."""
    from agentgrep.cli.parser import SearchArgs

    record = _make_record(text="first line of content", timestamp="2026-05-22T14:30:00Z")
    args = SearchArgs(
        terms=(),
        agents=("codex", "claude", "cursor", "gemini"),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
    )
    result = format_search_record(record, args, colors=_NO_COLORS, patterns=[])
    lines = result.split("\n")
    content_line = next(i for i, line in enumerate(lines) if "first line of content" in line)
    provenance_line = next(
        i for i, line in enumerate(lines) if "claude" in line and "prompt" in line
    )
    assert content_line < provenance_line


def test_provenance_indented() -> None:
    """The provenance line starts with 2-space indent."""
    from agentgrep.cli.parser import SearchArgs

    record = _make_record(text="content")
    args = SearchArgs(
        terms=(),
        agents=("codex", "claude", "cursor", "gemini"),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
    )
    result = format_search_record(record, args, colors=_NO_COLORS, patterns=[])
    provenance = [line for line in result.split("\n") if "claude" in line and "prompt" in line]
    assert provenance
    assert provenance[0].startswith("  ")


# ---------------------------------------------------------------------------
# SearchSummary
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
def test_search_summary_format(case: SummaryCase) -> None:
    """SearchSummary.format produces expected footer content."""
    summary = SearchSummary()
    for agent in case.agents:
        record = _make_record(agent=agent)
        summary.add(record)
    summary.elapsed = case.elapsed
    result = summary.format(colors=_NO_COLORS)
    for expected in case.expected_contains:
        assert expected in result, f"Expected {expected!r} in summary"


def test_summary_zero_records_empty() -> None:
    """Summary with zero records returns empty string."""
    summary = SearchSummary()
    assert summary.format(colors=_NO_COLORS) == ""


def test_summary_add_increments() -> None:
    """Each add() call increments total and per_agent."""
    summary = SearchSummary()
    summary.add(_make_record(agent="codex"))
    summary.add(_make_record(agent="codex"))
    summary.add(_make_record(agent="claude"))
    assert summary.total == 3
    assert summary.per_agent == {"codex": 2, "claude": 1}
