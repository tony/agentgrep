"""Project-origin records, query fields, and self-context helpers."""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

import agentgrep
from agentgrep.query import compile_query, default_registry, parse_query


def _write_jsonl(path: pathlib.Path, rows: list[object]) -> None:
    """Write JSONL rows for adapter fixtures."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _source(
    path: pathlib.Path,
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "codex.sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    source_kind: agentgrep.SourceKind = "jsonl",
) -> agentgrep.SourceHandle:
    """Build a search source for parser tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=path,
        path_kind="session_file",
        source_kind=source_kind,
        search_root=None,
        mtime_ns=1,
    )


def test_search_record_origin_serialization_scrubs_path_like_values(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origin output and legacy path-like metadata use display-safe paths."""
    home = tmp_path / "home"
    project = home / "work" / "agentgrep"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="origin privacy",
        origin=agentgrep.RecordOrigin(
            cwd=str(project / "src"),
            repo=str(project),
            worktree=str(project),
            branch="project-context",
            remote="https://github.com/tony/agentgrep",
        ),
        metadata={"project": str(project)},
    )

    payload = agentgrep.serialize_search_record(record)

    assert payload["origin"] == {
        "cwd": "~/work/agentgrep/src/",
        "repo": "~/work/agentgrep/",
        "worktree": "~/work/agentgrep/",
        "branch": "project-context",
        "remote": "https://github.com/tony/agentgrep",
    }
    assert payload["metadata"]["project"] == "~/work/agentgrep/"
    assert str(home) not in json.dumps(payload)


def test_origin_query_fields_filter_records_and_expand_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwd/repo/worktree path fields and branch/project strings are queryable."""
    home = tmp_path / "home"
    project = home / "work" / "agentgrep"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="needle prompt",
        origin=agentgrep.RecordOrigin(
            cwd=str(project / "src"),
            repo=str(project),
            worktree=str(project),
            branch="project-context",
            cwd_hash="hash123",
        ),
    )
    query = (
        'cwd:"~/work/agentgrep/*" '
        'repo:"~/work/agentgrep" '
        "branch:project-context "
        "project:agentgrep "
        "cwd_hash:hash123 "
        "needle"
    )
    compiled = compile_query(parse_query(query, default_registry()), default_registry())

    assert compiled.source_predicate is not None
    assert compiled.record_predicate is not None
    assert compiled.source_predicate(
        _source(home / ".codex" / "sessions" / "rollout.jsonl"),
    )
    assert compiled.record_predicate(record)
    assert not compiled.record_predicate(
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=record.path,
            text="needle prompt",
            origin=agentgrep.RecordOrigin(
                cwd=str(home / "other"),
                repo=str(home / "other"),
                branch="main",
            ),
        ),
    )


def test_origin_fields_fall_back_to_legacy_metadata() -> None:
    """Already-shipped metadata keys remain queryable during migration."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="opencode",
        store="opencode.db",
        adapter_id="opencode.db_sqlite.v1",
        path=pathlib.Path("/tmp/opencode.db"),
        text="streaming prompt",
        metadata={"directory": "/work/project"},
    )
    compiled = compile_query(
        parse_query("cwd:/work/project project:project streaming", default_registry()),
        default_registry(),
    )

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record)


def test_codex_session_origin_uses_session_meta_cwd_and_git(
    tmp_path: pathlib.Path,
) -> None:
    """Codex session_meta carries cwd plus git branch/remote onto records."""
    session = tmp_path / "rollout.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-session",
                    "cwd": "/workspace/agentgrep",
                    "git": {
                        "branch": "project-context",
                        "repository_url": "https://github.com/tony/agentgrep",
                    },
                },
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "codex origin"}],
                },
            },
        ],
    )

    records = list(agentgrep.parse_codex_session_file(_source(session)))

    assert len(records) == 1
    assert records[0].origin == agentgrep.RecordOrigin(
        cwd="/workspace/agentgrep",
        branch="project-context",
        remote="https://github.com/tony/agentgrep",
    )


def test_claude_project_origin_uses_per_record_cwd_and_branch(
    tmp_path: pathlib.Path,
) -> None:
    """Claude project JSONL turns retain their cwd and gitBranch."""
    session = tmp_path / "claude.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "user",
                "cwd": "/workspace/agentgrep",
                "gitBranch": "main",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "claude origin"}],
                },
            },
        ],
    )

    records = list(
        agentgrep.parse_claude_project_file(
            _source(
                session,
                agent="claude",
                store="claude.projects",
                adapter_id="claude.projects_jsonl.v1",
            ),
        ),
    )

    assert len(records) == 1
    assert records[0].origin == agentgrep.RecordOrigin(
        cwd="/workspace/agentgrep",
        branch="main",
    )


def test_pi_session_origin_keeps_cwd_separate_from_conversation_id(
    tmp_path: pathlib.Path,
) -> None:
    """Pi keeps legacy conversation_id while exposing cwd as origin."""
    session = tmp_path / "sess.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "session",
                "id": "pi-session",
                "cwd": "/workspace/pi-project",
            },
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "pi origin"},
            },
        ],
    )

    records = list(
        agentgrep.parse_pi_session_file(
            _source(
                session,
                agent="pi",
                store="pi.sessions",
                adapter_id="pi.sessions_jsonl.v1",
            ),
        ),
    )

    assert len(records) == 1
    assert records[0].conversation_id == "/workspace/pi-project"
    assert records[0].origin == agentgrep.RecordOrigin(cwd="/workspace/pi-project")


def test_gemini_chat_origin_exposes_project_hash(
    tmp_path: pathlib.Path,
) -> None:
    """Gemini chat metadata exposes the only available project signal."""
    session = tmp_path / "hash123" / "chats" / "chat.jsonl"
    _write_jsonl(
        session,
        [
            {"kind": "main", "sessionId": "gemini-session", "projectHash": "hash123"},
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "content": "gemini origin",
            },
        ],
    )

    records = list(
        agentgrep.parse_gemini_chat_file(
            _source(
                session,
                agent="gemini",
                store="gemini.tmp_chats",
                adapter_id="gemini.tmp_chats_jsonl.v1",
            ),
        ),
    )

    assert len(records) == 1
    assert records[0].origin == agentgrep.RecordOrigin(cwd_hash="hash123")


def test_grok_session_search_origin_uses_cwd_column(
    tmp_path: pathlib.Path,
) -> None:
    """Grok FTS records retain the indexed session cwd column."""
    db_path = tmp_path / "session_search.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE session_docs ("
            "session_id TEXT, cwd TEXT, updated_at INTEGER, title TEXT, "
            "content TEXT, content_hash TEXT)"
        )
        conn.execute(
            "INSERT INTO session_docs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "grok-session",
                "/workspace/grok-project",
                1770000000,
                "Grok title",
                "grok origin content",
                "abc",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    records = list(
        agentgrep.parse_grok_session_search_db(
            _source(
                db_path,
                agent="grok",
                store="grok.session_search",
                adapter_id="grok.session_search_sqlite.v1",
                source_kind="sqlite",
            ),
        ),
    )

    assert len(records) == 1
    assert records[0].origin == agentgrep.RecordOrigin(cwd="/workspace/grok-project")


def test_project_context_detects_git_branch_without_subprocess(
    tmp_path: pathlib.Path,
) -> None:
    """The stdlib detector reads .git/HEAD for an explicit project scope."""
    worktree = tmp_path / "repo"
    git_dir = worktree / ".git"
    git_dir.mkdir(parents=True)
    _ = (git_dir / "HEAD").write_text("ref: refs/heads/project-context\n", encoding="utf-8")
    child = worktree / "src" / "agentgrep"
    child.mkdir(parents=True)

    context = agentgrep.detect_project_context(child)

    assert context.cwd == child
    assert context.worktree == worktree
    assert context.repo == worktree
    assert context.branch == "project-context"
    assert context.origin == agentgrep.RecordOrigin(
        cwd=str(child),
        repo=str(worktree),
        worktree=str(worktree),
        branch="project-context",
    )


def test_ranking_can_boost_same_origin_records() -> None:
    """The opt-in --here boost reorders already-collected search results."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=pathlib.Path("/tmp/a.jsonl"),
            text="streaming parser notes",
            origin=agentgrep.RecordOrigin(cwd="/elsewhere"),
        ),
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=pathlib.Path("/tmp/b.jsonl"),
            text="streaming parser notes",
            origin=agentgrep.RecordOrigin(cwd="/workspace/agentgrep/src"),
        ),
    ]

    ranked = agentgrep.rank_search_records(
        records,
        "streaming parser",
        origin_boost=agentgrep.RecordOrigin(repo="/workspace/agentgrep"),
    )

    assert ranked[0][0].origin and ranked[0][0].origin.cwd == "/workspace/agentgrep/src"
