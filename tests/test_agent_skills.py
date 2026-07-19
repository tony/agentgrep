"""Tests for repo-local agent skill guidance."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

pytestmark = pytest.mark.setup

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKILLS_ROOT = _REPO_ROOT / ".agents" / "skills"


class AgentSkillCase(t.NamedTuple):
    """One repo-local skill file and required terms."""

    test_id: str
    filename: str
    required_terms: tuple[str, ...]


AGENT_SKILL_CASES: tuple[AgentSkillCase, ...] = (
    AgentSkillCase(
        test_id="benchmark-skill",
        filename="benchmark.md",
        required_terms=(
            "name: benchmark",
            "$benchmark <component>",
            "search-prompts",
            "grep-conversations",
            "find-prompts",
            "--commands profile-engine",
            "profile-engine-cursor-ide",
            "scripts/benchmark.py analyze",
            "agentgrep.benchmark.analysis",
            "artifact_kind",
            "profile_payload.profile.samples",
            "profile span groups",
            "search.collect.source",
        ),
    ),
    AgentSkillCase(
        test_id="profile-skill",
        filename="profile.md",
        required_terms=(
            "name: profile",
            "$profile <component>",
            "scripts/profile_engine.py",
            "search-prompts",
            "grep-conversations",
            "find-prompts",
            "cursor-ide",
            "schema_version",
            "--json",
            "--ndjson",
            "--top-spans 0",
            "find.filter.source",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    AGENT_SKILL_CASES,
    ids=[c.test_id for c in AGENT_SKILL_CASES],
)
def test_repo_agent_skills_document_component_argument(case: AgentSkillCase) -> None:
    """Repo-local agent skills expose the component argument and known components."""
    text = (_SKILLS_ROOT / case.filename).read_text(encoding="utf-8")

    assert text.startswith("---\n")
    for term in case.required_terms:
        assert term in text
