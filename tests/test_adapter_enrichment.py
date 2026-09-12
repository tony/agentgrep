"""Metadata enrichment contracts for records parsed from agent stores.

Each case feeds one redacted fixture from ``tests/samples/`` through its
adapter and checks the model, origin, or title the adapter now credits the
record with. The fixtures are structural: synthetic content in the real
key layout.
"""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

from agentgrep.adapters.claude import parse_claude_project_file
from agentgrep.adapters.grok import parse_grok_subagents
from agentgrep.records import SourceHandle

from .conftest import fixture_path


class GrokSubagentCase(t.NamedTuple):
    """One Grok subagent ``meta.json`` shape and the record it should yield."""

    test_id: str
    dropped_keys: tuple[str, ...]
    expected_model: str | None
    expected_cwd: str | None


GROK_SUBAGENT_CASES = (
    GrokSubagentCase(
        "child-cwd-and-model",
        (),
        "grok-code",
        "/home/user/work/project",
    ),
    GrokSubagentCase(
        "project-dir-fallback",
        ("child_cwd", "effective_model_id"),
        None,
        "/work/other",
    ),
)


@pytest.mark.parametrize(
    list(GrokSubagentCase._fields),
    GROK_SUBAGENT_CASES,
    ids=[case.test_id for case in GROK_SUBAGENT_CASES],
)
def test_grok_subagent_credits_child_model_and_cwd(
    tmp_path: pathlib.Path,
    test_id: str,
    dropped_keys: tuple[str, ...],
    expected_model: str | None,
    expected_cwd: str | None,
) -> None:
    """A dispatch record carries the child's model and working directory."""
    payload = json.loads(fixture_path("grok.subagents", "meta.json").read_text(encoding="utf-8"))
    for key in dropped_keys:
        payload.pop(key)
    meta = (
        tmp_path
        / "sessions"
        / "%2Fwork%2Fother"
        / "session-1"
        / "subagents"
        / "agent-1"
        / "meta.json"
    )
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps(payload), encoding="utf-8")
    source = SourceHandle(
        agent="grok",
        store="grok.subagents",
        adapter_id="grok.subagents_json.v1",
        path=meta,
        path_kind="session_file",
        source_kind="json",
        search_root=None,
        mtime_ns=0,
    )
    (record,) = parse_grok_subagents(source)
    assert record.model == expected_model
    assert (record.origin.cwd if record.origin else None) == expected_cwd


class ClaudeSubagentCase(t.NamedTuple):
    """One sidecar shape beside a Claude subagent transcript."""

    test_id: str
    dropped_keys: tuple[str, ...] | None
    expected_title: str | None


CLAUDE_SUBAGENT_CASES = (
    ClaudeSubagentCase("dispatch-description", (), "Map the example module"),
    ClaudeSubagentCase("name-only", ("description",), "example-mapper"),
    ClaudeSubagentCase("no-sidecar", None, None),
)


@pytest.mark.parametrize(
    list(ClaudeSubagentCase._fields),
    CLAUDE_SUBAGENT_CASES,
    ids=[case.test_id for case in CLAUDE_SUBAGENT_CASES],
)
def test_claude_subagent_records_take_the_sidecar_title(
    tmp_path: pathlib.Path,
    test_id: str,
    dropped_keys: tuple[str, ...] | None,
    expected_title: str | None,
) -> None:
    """Every record of a subagent transcript carries its dispatch title."""
    subagents = tmp_path / "projects" / "-work-example" / "session-1" / "subagents"
    subagents.mkdir(parents=True)
    transcript = subagents / "agent-1.jsonl"
    transcript.write_bytes(fixture_path("claude.projects.subagent", "example.jsonl").read_bytes())
    if dropped_keys is not None:
        sidecar = json.loads(
            fixture_path("claude.projects.subagent", "example.meta.json").read_text(
                encoding="utf-8"
            ),
        )
        for key in dropped_keys:
            sidecar.pop(key)
        (subagents / "agent-1.meta.json").write_text(json.dumps(sidecar), encoding="utf-8")
    source = SourceHandle(
        agent="claude",
        store="claude.projects_subagents",
        adapter_id="claude.projects_jsonl.v1",
        path=transcript,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )
    records = list(parse_claude_project_file(source))
    assert records
    assert {record.title for record in records} == {expected_title}
