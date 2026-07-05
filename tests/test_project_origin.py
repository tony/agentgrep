"""Project-origin records, query fields, and self-context helpers."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import typing as t

import pytest

import agentgrep
from agentgrep.origin import (
    normalize_origin_path_text,
    origin_filter_nodes,
    record_matches_origin,
)
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


class OriginTrailingSlashCase(t.NamedTuple):
    """Parametrized case for display-style origin path predicates."""

    test_id: str
    query: str
    expected: bool


class OriginPathBoundaryCase(t.NamedTuple):
    """Parametrized case for non-glob origin path predicates."""

    test_id: str
    record_cwd: str
    expected: bool


class RepoOriginFallbackCase(t.NamedTuple):
    """Parametrized case for repo predicates against partial origin metadata."""

    test_id: str
    origin: agentgrep.RecordOrigin
    expected: bool


class OriginRemoteSerializationCase(t.NamedTuple):
    """Parametrized case for serialized origin remotes."""

    test_id: str
    remote: str
    expected_remote: str | None


ORIGIN_TRAILING_SLASH_CASES: tuple[OriginTrailingSlashCase, ...] = (
    OriginTrailingSlashCase(
        test_id="cwd-display-path-matches-stored-path",
        query='cwd:"~/work/notes/" tmux',
        expected=True,
    ),
    OriginTrailingSlashCase(
        test_id="negated-cwd-display-path-excludes-stored-path",
        query='tmux AND (NOT cwd:"~/work/notes/")',
        expected=False,
    ),
)

ORIGIN_PATH_BOUNDARY_CASES: tuple[OriginPathBoundaryCase, ...] = (
    OriginPathBoundaryCase(
        test_id="exact-path",
        record_cwd="/tmp/repo",
        expected=True,
    ),
    OriginPathBoundaryCase(
        test_id="descendant-path",
        record_cwd="/tmp/repo/src",
        expected=True,
    ),
    OriginPathBoundaryCase(
        test_id="sibling-prefix-path",
        record_cwd="/tmp/repo2",
        expected=False,
    ),
)

REPO_ORIGIN_FALLBACK_CASES: tuple[RepoOriginFallbackCase, ...] = (
    RepoOriginFallbackCase(
        test_id="explicit-repo",
        origin=agentgrep.RecordOrigin(repo="/workspace/agentgrep"),
        expected=True,
    ),
    RepoOriginFallbackCase(
        test_id="cwd-descendant",
        origin=agentgrep.RecordOrigin(cwd="/workspace/agentgrep/src"),
        expected=True,
    ),
    RepoOriginFallbackCase(
        test_id="worktree-descendant",
        origin=agentgrep.RecordOrigin(worktree="/workspace/agentgrep/docs"),
        expected=True,
    ),
    RepoOriginFallbackCase(
        test_id="cwd-sibling-prefix",
        origin=agentgrep.RecordOrigin(cwd="/workspace/agentgrep2/src"),
        expected=False,
    ),
)

ORIGIN_REMOTE_SERIALIZATION_CASES: tuple[OriginRemoteSerializationCase, ...] = (
    OriginRemoteSerializationCase(
        test_id="public-https",
        remote="https://github.com/tony/agentgrep",
        expected_remote="https://github.com/tony/agentgrep",
    ),
    OriginRemoteSerializationCase(
        test_id="credential-https",
        remote="https://secret-token@github.com/tony/agentgrep.git?x=1#frag",
        expected_remote="https://github.com/tony/agentgrep.git",
    ),
    OriginRemoteSerializationCase(
        test_id="scp-ssh",
        remote="git@github.com:tony/agentgrep.git",
        expected_remote="ssh://github.com/tony/agentgrep.git",
    ),
    OriginRemoteSerializationCase(
        test_id="local-path",
        remote="/home/private/repo",
        expected_remote=None,
    ),
    OriginRemoteSerializationCase(
        test_id="file-url",
        remote="file:///home/private/repo",
        expected_remote=None,
    ),
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


@pytest.mark.parametrize(
    "case",
    ORIGIN_REMOTE_SERIALIZATION_CASES,
    ids=[case.test_id for case in ORIGIN_REMOTE_SERIALIZATION_CASES],
)
def test_record_origin_remote_serialization_is_safe(
    case: OriginRemoteSerializationCase,
) -> None:
    """Serialized origin remotes do not leak credentials or local paths."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="origin privacy",
        origin=agentgrep.RecordOrigin(remote=case.remote),
    )

    payload = agentgrep.serialize_search_record(record)
    origin = payload["origin"]

    if case.expected_remote is None:
        assert origin is None or "remote" not in origin
    else:
        assert origin is not None
        assert origin["remote"] == case.expected_remote
        assert "secret-token" not in json.dumps(origin)


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


class OriginIdentifierEqualityCase(t.NamedTuple):
    """Parametrized case for whole-value branch/project/cwd_hash matching."""

    test_id: str
    record_branch: str
    query: str
    expected: bool


ORIGIN_IDENTIFIER_EQUALITY_CASES: tuple[OriginIdentifierEqualityCase, ...] = (
    OriginIdentifierEqualityCase(
        test_id="branch-exact",
        record_branch="main",
        query="branch:main",
        expected=True,
    ),
    OriginIdentifierEqualityCase(
        test_id="branch-casefolded",
        record_branch="main",
        query="branch:MAIN",
        expected=True,
    ),
    OriginIdentifierEqualityCase(
        test_id="branch-superstring-miss",
        record_branch="maintenance",
        query="branch:main",
        expected=False,
    ),
    OriginIdentifierEqualityCase(
        test_id="branch-glob",
        record_branch="release/2026",
        query='branch:"release/*"',
        expected=True,
    ),
    OriginIdentifierEqualityCase(
        test_id="project-basename-exact",
        record_branch="main",
        query="project:agentgrep",
        expected=True,
    ),
    OriginIdentifierEqualityCase(
        test_id="project-substring-miss",
        record_branch="main",
        query="project:agent",
        expected=False,
    ),
    OriginIdentifierEqualityCase(
        test_id="cwd-hash-exact",
        record_branch="main",
        query="cwd_hash:hash123",
        expected=True,
    ),
    OriginIdentifierEqualityCase(
        test_id="cwd-hash-prefix-miss",
        record_branch="main",
        query="cwd_hash:hash",
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    ORIGIN_IDENTIFIER_EQUALITY_CASES,
    ids=[case.test_id for case in ORIGIN_IDENTIFIER_EQUALITY_CASES],
)
def test_origin_identifier_fields_match_whole_values(
    case: OriginIdentifierEqualityCase,
) -> None:
    """branch/project/cwd_hash predicates match whole values, not substrings."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="needle prompt",
        origin=agentgrep.RecordOrigin(
            cwd="/work/agentgrep/src",
            repo="/work/agentgrep",
            branch=case.record_branch,
            cwd_hash="hash123",
        ),
    )
    compiled = compile_query(parse_query(case.query, default_registry()), default_registry())

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record) is case.expected


def test_origin_boost_branch_matches_casefolded() -> None:
    """The boost's branch check uses the same whole-value casefolded rule."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="needle prompt",
        origin=agentgrep.RecordOrigin(branch="Main"),
    )

    assert record_matches_origin(record, agentgrep.RecordOrigin(branch="main")) is True
    assert record_matches_origin(record, agentgrep.RecordOrigin(branch="ma")) is False


@pytest.mark.parametrize(
    "case",
    ORIGIN_TRAILING_SLASH_CASES,
    ids=[case.test_id for case in ORIGIN_TRAILING_SLASH_CASES],
)
def test_origin_path_fields_match_display_trailing_slash(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: OriginTrailingSlashCase,
) -> None:
    """Display-style cwd predicates match stored origin paths without a slash."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
        path=home / ".claude" / "history.jsonl",
        text="tmux plugin notes",
        origin=agentgrep.RecordOrigin(cwd=str(home / "work" / "notes")),
    )
    compiled = compile_query(
        parse_query(case.query, default_registry()),
        default_registry(),
    )

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record) is case.expected


@pytest.mark.parametrize(
    "case",
    ORIGIN_PATH_BOUNDARY_CASES,
    ids=[case.test_id for case in ORIGIN_PATH_BOUNDARY_CASES],
)
def test_origin_path_fields_match_boundaries(case: OriginPathBoundaryCase) -> None:
    """Non-glob cwd predicates match exact paths and descendants only."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="needle prompt",
        origin=agentgrep.RecordOrigin(cwd=case.record_cwd),
    )
    compiled = compile_query(
        parse_query('cwd:"/tmp/repo" needle', default_registry()),
        default_registry(),
    )

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record) is case.expected


@pytest.mark.parametrize(
    "case",
    REPO_ORIGIN_FALLBACK_CASES,
    ids=[case.test_id for case in REPO_ORIGIN_FALLBACK_CASES],
)
def test_repo_origin_field_matches_partial_origin_metadata(
    case: RepoOriginFallbackCase,
) -> None:
    """Repo predicates match cwd/worktree-only origins by path boundary."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="needle prompt",
        origin=case.origin,
    )
    compiled = compile_query(
        parse_query('repo:"/workspace/agentgrep" needle', default_registry()),
        default_registry(),
    )

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record) is case.expected


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


class OriginBoostDirectionCase(t.NamedTuple):
    """Parametrized case for same-project boost path direction."""

    test_id: str
    record_cwd: str
    expected: bool


ORIGIN_BOOST_DIRECTION_CASES: tuple[OriginBoostDirectionCase, ...] = (
    OriginBoostDirectionCase(
        test_id="equal-path",
        record_cwd="/home/user/work/proj",
        expected=True,
    ),
    OriginBoostDirectionCase(
        test_id="descendant-path",
        record_cwd="/home/user/work/proj/src",
        expected=True,
    ),
    OriginBoostDirectionCase(
        test_id="ancestor-path",
        record_cwd="/home/user",
        expected=False,
    ),
    OriginBoostDirectionCase(
        test_id="sibling-prefix-path",
        record_cwd="/home/user/work/proj2",
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    ORIGIN_BOOST_DIRECTION_CASES,
    ids=[case.test_id for case in ORIGIN_BOOST_DIRECTION_CASES],
)
def test_origin_boost_requires_equal_or_descendant_paths(
    case: OriginBoostDirectionCase,
) -> None:
    """The --here boost only accepts records at or under the target project."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="streaming parser notes",
        origin=agentgrep.RecordOrigin(cwd=case.record_cwd),
    )
    boost = agentgrep.RecordOrigin(repo="/home/user/work/proj")

    assert record_matches_origin(record, boost) is case.expected


class OriginBoostNormalizationCase(t.NamedTuple):
    """Parametrized case for boost path normalization parity with filters."""

    test_id: str
    record_cwd: str
    boost: agentgrep.RecordOrigin
    expected: bool


ORIGIN_BOOST_NORMALIZATION_CASES: tuple[OriginBoostNormalizationCase, ...] = (
    OriginBoostNormalizationCase(
        test_id="backslash-descendant",
        record_cwd="C:\\repo\\src",
        boost=agentgrep.RecordOrigin(repo="C:\\repo"),
        expected=True,
    ),
    OriginBoostNormalizationCase(
        test_id="root-target-matches-absolute",
        record_cwd="/etc",
        boost=agentgrep.RecordOrigin(cwd="/"),
        expected=True,
    ),
    OriginBoostNormalizationCase(
        test_id="trailing-slash-target",
        record_cwd="/home/user/work/proj/src",
        boost=agentgrep.RecordOrigin(repo="/home/user/work/proj/"),
        expected=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    ORIGIN_BOOST_NORMALIZATION_CASES,
    ids=[case.test_id for case in ORIGIN_BOOST_NORMALIZATION_CASES],
)
def test_origin_boost_shares_filter_path_normalization(
    case: OriginBoostNormalizationCase,
) -> None:
    """The boost applies the same boundary normalization as origin filters."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="streaming parser notes",
        origin=agentgrep.RecordOrigin(cwd=case.record_cwd),
    )

    assert record_matches_origin(record, case.boost) is case.expected


class BlankOriginFilterCase(t.NamedTuple):
    """Parametrized case for blank origin filter values."""

    test_id: str
    value: str


BLANK_ORIGIN_FILTER_CASES: tuple[BlankOriginFilterCase, ...] = (
    BlankOriginFilterCase(test_id="empty", value=""),
    BlankOriginFilterCase(test_id="spaces", value="   "),
    BlankOriginFilterCase(test_id="tab", value="\t"),
)


@pytest.mark.parametrize(
    "case",
    BLANK_ORIGIN_FILTER_CASES,
    ids=[case.test_id for case in BLANK_ORIGIN_FILTER_CASES],
)
def test_blank_origin_filter_values_are_absent(case: BlankOriginFilterCase) -> None:
    """A blank filter value is treated as absent, not as the process cwd."""
    assert normalize_origin_path_text(case.value) is None
    assert origin_filter_nodes(branch=case.value) == ()
