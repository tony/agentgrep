"""Tests for Claude human-vs-tool prompt classification.

Claude Code records tool results and subagent output as ``type=user``
messages, so ``role`` alone cannot separate the user's typed asks from tool
noise. :func:`agentgrep.claude_event_is_human_authored` keys on the event
structure instead, and the result is surfaced on
``SearchRecord.metadata["human_typed"]``.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep


class HumanAuthoredCase(t.NamedTuple):
    """One classification case for the human-vs-tool detector."""

    test_id: str
    event: dict[str, object]
    expected: bool


HUMAN_AUTHORED_CASES: tuple[HumanAuthoredCase, ...] = (
    HumanAuthoredCase(
        test_id="string-prompt-is-human",
        event={"type": "user", "promptSource": "cli", "message": {"content": "do a deep dive"}},
        expected=True,
    ),
    HumanAuthoredCase(
        test_id="text-block-prompt-is-human",
        event={"type": "user", "message": {"content": [{"type": "text", "text": "commit it"}]}},
        expected=True,
    ),
    HumanAuthoredCase(
        test_id="tool-result-block-is-not-human",
        event={
            "type": "user",
            "toolUseResult": {"ok": True},
            "message": {"content": [{"type": "tool_result", "content": "Installed 11 packages"}]},
        },
        expected=False,
    ),
    HumanAuthoredCase(
        test_id="tool-use-block-is-not-human",
        event={"type": "user", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        expected=False,
    ),
    HumanAuthoredCase(
        test_id="sidechain-is-not-human",
        event={"type": "user", "isSidechain": True, "message": {"content": "subagent task"}},
        expected=False,
    ),
    HumanAuthoredCase(
        test_id="assistant-is-not-human",
        event={"type": "assistant", "message": {"content": [{"type": "text", "text": "Sure"}]}},
        expected=False,
    ),
    HumanAuthoredCase(
        test_id="local-command-stdout-is-not-human",
        event={
            "type": "user",
            "message": {"content": "<local-command-stdout>done</local-command-stdout>"},
        },
        expected=False,
    ),
    HumanAuthoredCase(
        test_id="interrupt-text-block-is-not-human",
        event={
            "type": "user",
            "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
        },
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case", HUMAN_AUTHORED_CASES, ids=[c.test_id for c in HUMAN_AUTHORED_CASES]
)
def test_claude_event_is_human_authored(case: HumanAuthoredCase) -> None:
    """The detector separates typed prompts from tool/agent turns by structure."""
    assert agentgrep.claude_event_is_human_authored(case.event) is case.expected


def test_non_dict_event_defaults_to_human() -> None:
    """A non-dict event is treated as human-authored (no marker to inspect)."""
    assert agentgrep.claude_event_is_human_authored("not a dict") is True


def _candidate(text: str) -> agentgrep.MessageCandidate:
    """Build a minimal user-role message candidate."""
    return agentgrep.MessageCandidate(role="user", text=text)


def test_build_search_record_tags_only_non_human() -> None:
    """Only the non-human record carries ``metadata["human_typed"]``."""
    source = agentgrep.SourceHandle(
        agent="claude",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        path=pathlib.Path("/x/session.jsonl"),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )
    human = agentgrep.build_search_record(source, _candidate("hi"))
    tool = agentgrep.build_search_record(source, _candidate("tool out"), human_typed=False)
    assert human.metadata == {}
    assert tool.metadata == {"human_typed": False}
