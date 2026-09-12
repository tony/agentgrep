"""Metadata enrichment contracts for records parsed from agent stores.

Each case feeds one redacted fixture from ``tests/samples/`` through its
adapter and checks the model, origin, or title the adapter now credits the
record with. The fixtures are structural: synthetic content in the real
key layout.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sqlite3
import typing as t

import pytest

from agentgrep.adapters.claude import parse_claude_project_file
from agentgrep.adapters.cursor_ide import parse_cursor_state_db
from agentgrep.adapters.grok import parse_grok_subagents
from agentgrep.origin import origin_cwd_hash
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


class CursorComposerCase(t.NamedTuple):
    """One ``state.vscdb`` shape and the title and origin its turn should carry."""

    test_id: str
    extra_sql: str
    expected_title: str | None
    expected_cwd: str | None


CURSOR_COMPOSER_CASES = (
    CursorComposerCase(
        "headers-give-workspace",
        "",
        "Refactor the parser",
        "/home/user/work/project",
    ),
    CursorComposerCase(
        "no-headers-table",
        "DROP TABLE composerHeaders;",
        "Refactor the parser",
        None,
    ),
    CursorComposerCase(
        "header-name-backs-up-document",
        "UPDATE cursorDiskKV SET value = '{}' WHERE key LIKE 'composerData:%';",
        "Refactor the parser",
        "/home/user/work/project",
    ),
)


@pytest.mark.parametrize(
    list(CursorComposerCase._fields),
    CURSOR_COMPOSER_CASES,
    ids=[case.test_id for case in CURSOR_COMPOSER_CASES],
)
def test_cursor_composer_turns_take_session_title_and_workspace(
    tmp_path: pathlib.Path,
    test_id: str,
    extra_sql: str,
    expected_title: str | None,
    expected_cwd: str | None,
) -> None:
    """A global-database turn is titled and placed by its composer's rows."""
    database = tmp_path / "globalStorage" / "state.vscdb"
    database.parent.mkdir()
    script = fixture_path("cursor-ide.state_vscdb", "composer-headers.sql").read_text(
        encoding="utf-8"
    )
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.executescript(script + extra_sql)
        connection.commit()
    source = SourceHandle(
        agent="cursor-ide",
        store="cursor-ide.state_vscdb",
        adapter_id="cursor_ide.state_vscdb_modern.v1",
        path=database,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=0,
    )
    (record,) = parse_cursor_state_db(source)
    assert record.title == expected_title
    assert (record.origin.cwd if record.origin else None) == expected_cwd
    if expected_cwd is not None:
        assert record.origin is not None
        assert record.origin.cwd_hash == origin_cwd_hash("0123456789abcdef0123456789abcdef")
