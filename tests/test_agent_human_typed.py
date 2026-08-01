"""Cross-agent human-vs-tool prompt classification.

Adapters whose store separates turns by role (Gemini, Cursor, Pi, OpenCode)
route through the shared :func:`agentgrep.candidate_is_human_typed` helper; the
result is surfaced on ``SearchRecord.metadata["human_typed"]`` exactly as the
Claude/Codex structural detectors do.
"""

from __future__ import annotations

import types
import typing as t

import pytest

import agentgrep


class RoleCase(t.NamedTuple):
    """One role → human-typed classification case."""

    test_id: str
    role: str | None
    expected: bool


ROLE_CASES: tuple[RoleCase, ...] = (
    RoleCase(test_id="user-is-human", role="user", expected=True),
    RoleCase(test_id="human-is-human", role="human", expected=True),
    RoleCase(test_id="mixed-case-user-is-human", role="User", expected=True),
    RoleCase(test_id="assistant-is-not-human", role="assistant", expected=False),
    RoleCase(test_id="gemini-is-not-human", role="gemini", expected=False),
    RoleCase(test_id="tool-is-not-human", role="tool", expected=False),
    RoleCase(test_id="none-is-not-human", role=None, expected=False),
)


@pytest.mark.parametrize("case", ROLE_CASES, ids=[c.test_id for c in ROLE_CASES])
def test_candidate_is_human_typed(case: RoleCase) -> None:
    """A user/human role is a typed turn; assistant/tool/None roles are not."""
    candidate = t.cast("t.Any", types.SimpleNamespace(role=case.role))
    assert agentgrep.candidate_is_human_typed(candidate) is case.expected
