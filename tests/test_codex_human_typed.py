"""Tests for Codex human-vs-tool prompt classification.

Codex records tool calls, tool output, and reasoning as ``response_item``
events alongside the user's typed messages. ``role`` alone cannot separate
them, so :func:`agentgrep.codex_event_is_human_authored` keys on the
``payload.type``/``payload.role`` shape and the result is surfaced on
``SearchRecord.metadata["human_typed"]``.
"""

from __future__ import annotations

import typing as t

import pytest

import agentgrep


class CodexAuthoredCase(t.NamedTuple):
    """One classification case for the Codex human-vs-tool detector."""

    test_id: str
    payload: dict[str, object]
    expected: bool


CODEX_AUTHORED_CASES: tuple[CodexAuthoredCase, ...] = (
    CodexAuthoredCase(
        test_id="user-message-is-human",
        payload={"type": "message", "role": "user", "content": "deploy the app"},
        expected=True,
    ),
    CodexAuthoredCase(
        test_id="assistant-message-is-not-human",
        payload={"type": "message", "role": "assistant", "content": "On it"},
        expected=False,
    ),
    CodexAuthoredCase(
        test_id="function-call-is-not-human",
        payload={"type": "function_call", "name": "shell", "arguments": "{}"},
        expected=False,
    ),
    CodexAuthoredCase(
        test_id="function-call-output-is-not-human",
        payload={"type": "function_call_output", "output": "Installed 11 packages"},
        expected=False,
    ),
    CodexAuthoredCase(
        test_id="reasoning-is-not-human",
        payload={"type": "reasoning", "summary": "thinking"},
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case", CODEX_AUTHORED_CASES, ids=[c.test_id for c in CODEX_AUTHORED_CASES]
)
def test_codex_event_is_human_authored(case: CodexAuthoredCase) -> None:
    """The detector separates typed prompts from tool/reasoning turns by shape."""
    assert agentgrep.codex_event_is_human_authored(case.payload) is case.expected


def test_non_dict_payload_defaults_to_human() -> None:
    """A non-dict payload is treated as human-authored (no marker to inspect)."""
    assert agentgrep.codex_event_is_human_authored("not a dict") is True
