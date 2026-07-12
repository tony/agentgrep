"""Project-origin records, query fields, and self-context helpers."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import typing as t

import pytest

import agentgrep
from agentgrep import origin
from agentgrep.origin import (
    CWD_DIGEST_LENGTH,
    OriginEncoding,
    OriginMatcher,
    decode_project_dir,
    is_cwd_digest,
    normalize_origin_path_text,
    origin_cwd_hash,
    origin_filter_nodes,
    record_matches_origin,
)
from agentgrep.query import (
    compile_query,
    compose_query_ast,
    default_registry,
    parse_query,
)


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


class OriginRepoPathGateCase(t.NamedTuple):
    """Parametrized case for repo-like mapping values."""

    test_id: str
    value: str
    expected_repo: str | None


class LegacyMetadataSerializationCase(t.NamedTuple):
    """Parametrized case for legacy metadata display rewriting."""

    test_id: str
    key: str
    value: str
    expected: str


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
        test_id="scp-embedded-credential",
        remote="git@user:token@github.com:tony/agentgrep.git",
        expected_remote=None,
    ),
    OriginRemoteSerializationCase(
        test_id="scp-credential-shaped-host",
        remote="user@pass@host:path",
        expected_remote=None,
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

ORIGIN_REPO_PATH_GATE_CASES: tuple[OriginRepoPathGateCase, ...] = (
    OriginRepoPathGateCase(
        test_id="absolute-path",
        value="/workspace/agentgrep",
        expected_repo="/workspace/agentgrep",
    ),
    OriginRepoPathGateCase(
        test_id="relative-path",
        value="work/agentgrep",
        expected_repo="work/agentgrep",
    ),
    OriginRepoPathGateCase(
        test_id="https-remote",
        value="https://token@github.com/org/repo.git",
        expected_repo=None,
    ),
    OriginRepoPathGateCase(
        test_id="scp-remote",
        value="git@github.com:org/repo.git",
        expected_repo=None,
    ),
)

LEGACY_METADATA_SERIALIZATION_CASES: tuple[LegacyMetadataSerializationCase, ...] = (
    LegacyMetadataSerializationCase(
        test_id="branch-with-slash",
        key="branch",
        value="feature/foo",
        expected="feature/foo",
    ),
    LegacyMetadataSerializationCase(
        test_id="git-branch-with-slash",
        key="gitBranch",
        value="feature/foo",
        expected="feature/foo",
    ),
    LegacyMetadataSerializationCase(
        test_id="directory-path",
        key="directory",
        value="work/proj",
        expected="work/proj/",
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
    LEGACY_METADATA_SERIALIZATION_CASES,
    ids=[case.test_id for case in LEGACY_METADATA_SERIALIZATION_CASES],
)
def test_legacy_metadata_serialization_rewrites_paths_only(
    case: LegacyMetadataSerializationCase,
) -> None:
    """Legacy metadata serialization rewrites paths, not identifiers."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="metadata serialization",
        metadata={case.key: case.value},
    )

    payload = agentgrep.serialize_search_record(record)

    assert payload["metadata"][case.key] == case.expected


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


@pytest.mark.parametrize(
    "case",
    ORIGIN_REPO_PATH_GATE_CASES,
    ids=[case.test_id for case in ORIGIN_REPO_PATH_GATE_CASES],
)
def test_message_origin_repo_values_reject_remotes(
    case: OriginRepoPathGateCase,
) -> None:
    """Generic origin extraction treats repo as a path, not a remote URL."""
    candidates = list(
        agentgrep.iter_message_candidates(
            {"role": "user", "content": "repo origin", "repo": case.value},
        ),
    )

    assert len(candidates) == 1
    origin = candidates[0].origin
    if case.expected_repo is None:
        assert origin is None or origin.repo is None
    else:
        assert origin == agentgrep.RecordOrigin(repo=case.expected_repo)


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


class OriginMatcherFieldCase(t.NamedTuple):
    """Parametrized case for compiled origin field predicates."""

    test_id: str
    field: str
    value: str
    origin: agentgrep.RecordOrigin
    expected: bool


ORIGIN_MATCHER_FIELD_CASES: tuple[OriginMatcherFieldCase, ...] = (
    OriginMatcherFieldCase(
        test_id="branch-whole-value",
        field="branch",
        value="MAIN",
        origin=agentgrep.RecordOrigin(branch="main"),
        expected=True,
    ),
    OriginMatcherFieldCase(
        test_id="branch-substring-miss",
        field="branch",
        value="main",
        origin=agentgrep.RecordOrigin(branch="maintenance"),
        expected=False,
    ),
    OriginMatcherFieldCase(
        test_id="branch-wildcard",
        field="branch",
        value="release/*",
        origin=agentgrep.RecordOrigin(branch="release/2026"),
        expected=True,
    ),
    OriginMatcherFieldCase(
        test_id="project-basename",
        field="project",
        value="agentgrep",
        origin=agentgrep.RecordOrigin(repo="/work/agentgrep"),
        expected=True,
    ),
    OriginMatcherFieldCase(
        test_id="cwd-descendant-path",
        field="cwd",
        value="/work/agentgrep",
        origin=agentgrep.RecordOrigin(cwd="/work/agentgrep/src"),
        expected=True,
    ),
    OriginMatcherFieldCase(
        test_id="cwd-sibling-prefix-miss",
        field="cwd",
        value="/work/agentgrep",
        origin=agentgrep.RecordOrigin(cwd="/work/agentgrep2/src"),
        expected=False,
    ),
)


class OriginMatcherContextCase(t.NamedTuple):
    """Parametrized case for compiled origin context predicates."""

    test_id: str
    boost: agentgrep.RecordOrigin
    origin: agentgrep.RecordOrigin
    expected: bool


ORIGIN_MATCHER_CONTEXT_CASES: tuple[OriginMatcherContextCase, ...] = (
    OriginMatcherContextCase(
        test_id="repo-matches-record-cwd-descendant",
        boost=agentgrep.RecordOrigin(repo="/work/agentgrep"),
        origin=agentgrep.RecordOrigin(cwd="/work/agentgrep/src"),
        expected=True,
    ),
    OriginMatcherContextCase(
        test_id="worktree-matches-record-cwd-descendant",
        boost=agentgrep.RecordOrigin(worktree="/work/agentgrep"),
        origin=agentgrep.RecordOrigin(cwd="/work/agentgrep/src"),
        expected=True,
    ),
    OriginMatcherContextCase(
        test_id="combined-branch-mismatch",
        boost=agentgrep.RecordOrigin(repo="/work/agentgrep", branch="main"),
        origin=agentgrep.RecordOrigin(cwd="/work/agentgrep/src", branch="feature"),
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    ORIGIN_MATCHER_FIELD_CASES,
    ids=[case.test_id for case in ORIGIN_MATCHER_FIELD_CASES],
)
def test_origin_matcher_matches_field_predicates(case: OriginMatcherFieldCase) -> None:
    """Compiled origin field predicates preserve existing query semantics."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="needle prompt",
        origin=case.origin,
    )

    matcher = OriginMatcher.from_field_value(case.field, case.value)

    assert matcher.matches(record) is case.expected


@pytest.mark.parametrize(
    "case",
    ORIGIN_MATCHER_CONTEXT_CASES,
    ids=[case.test_id for case in ORIGIN_MATCHER_CONTEXT_CASES],
)
def test_origin_matcher_matches_origin_context(case: OriginMatcherContextCase) -> None:
    """Compiled origin context predicates preserve same-project boost semantics."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="needle prompt",
        origin=case.origin,
    )

    matcher = OriginMatcher.from_origin(case.boost)

    assert matcher.matches(record) is case.expected


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


class SymlinkOriginFilterCase(t.NamedTuple):
    """Parametrized case for symlinked cwd filters vs recorded path forms."""

    test_id: str
    record_form: t.Literal["logical", "physical"]


class RelativeOriginFilterCase(t.NamedTuple):
    """Parametrized case for relative origin filters from a symlinked cwd."""

    test_id: str
    value: str
    target_relative: pathlib.Path
    record_relative: pathlib.Path


SYMLINK_ORIGIN_FILTER_CASES: tuple[SymlinkOriginFilterCase, ...] = (
    SymlinkOriginFilterCase(test_id="logically-recorded-cwd", record_form="logical"),
    SymlinkOriginFilterCase(test_id="physically-recorded-cwd", record_form="physical"),
)

RELATIVE_ORIGIN_FILTER_CASES: tuple[RelativeOriginFilterCase, ...] = (
    RelativeOriginFilterCase(
        test_id="current-directory",
        value=".",
        target_relative=pathlib.Path(),
        record_relative=pathlib.Path("src"),
    ),
    RelativeOriginFilterCase(
        test_id="child-directory",
        value="src",
        target_relative=pathlib.Path("src"),
        record_relative=pathlib.Path("src/nested"),
    ),
)


@pytest.mark.parametrize(
    "case",
    SYMLINK_ORIGIN_FILTER_CASES,
    ids=[case.test_id for case in SYMLINK_ORIGIN_FILTER_CASES],
)
def test_cwd_filter_matches_across_symlinks(
    tmp_path: pathlib.Path,
    case: SymlinkOriginFilterCase,
) -> None:
    """A symlinked filter path matches logical and physical recorded cwds."""
    real = tmp_path / "real" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    logical = link / "proj"
    record_cwd = str(logical) if case.record_form == "logical" else str(real)

    filter_value = normalize_origin_path_text(str(logical))
    assert filter_value is not None
    registry = default_registry()
    ast, _user_ast = compose_query_ast((), origin_filter_nodes(cwd=filter_value), registry)
    compiled = compile_query(ast, registry)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="symlinked project notes",
        origin=agentgrep.RecordOrigin(cwd=record_cwd),
    )

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record)


@pytest.mark.parametrize(
    "case",
    RELATIVE_ORIGIN_FILTER_CASES,
    ids=[case.test_id for case in RELATIVE_ORIGIN_FILTER_CASES],
)
def test_relative_cwd_filter_preserves_logical_pwd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RelativeOriginFilterCase,
) -> None:
    """Relative origin filters use the logical symlink path from PWD."""
    real = tmp_path / "real" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    logical = link / "proj"
    logical_src = logical / "src"
    logical_src.mkdir()
    monkeypatch.chdir(logical)
    monkeypatch.setenv("PWD", str(logical))

    filter_value = normalize_origin_path_text(case.value)

    assert filter_value == str(logical / case.target_relative)
    registry = default_registry()
    ast, _user_ast = compose_query_ast((), origin_filter_nodes(cwd=filter_value), registry)
    compiled = compile_query(ast, registry)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="symlinked project notes",
        origin=agentgrep.RecordOrigin(cwd=str(logical / case.record_relative)),
    )

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(record)


class HistoryOriginGateCase(t.NamedTuple):
    """Parametrized case for path-likeness gating in history parsers."""

    test_id: str
    parser: str
    row: dict[str, object]
    expected_cwd: str | None


HISTORY_ORIGIN_GATE_CASES: tuple[HistoryOriginGateCase, ...] = (
    HistoryOriginGateCase(
        test_id="claude-path-project-kept",
        parser="parse_claude_history_file",
        row={"display": "hi", "project": "/workspace/agentgrep"},
        expected_cwd="/workspace/agentgrep",
    ),
    HistoryOriginGateCase(
        test_id="claude-bare-token-project-dropped",
        parser="parse_claude_history_file",
        row={"display": "hi", "project": "a1b2-uuid"},
        expected_cwd=None,
    ),
    HistoryOriginGateCase(
        test_id="antigravity-path-workspace-kept",
        parser="parse_antigravity_cli_history_file",
        row={"display": "hi", "workspace": "/workspace/agentgrep"},
        expected_cwd="/workspace/agentgrep",
    ),
    HistoryOriginGateCase(
        test_id="antigravity-bare-token-workspace-dropped",
        parser="parse_antigravity_cli_history_file",
        row={"display": "hi", "workspace": "a1b2-uuid"},
        expected_cwd=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    HISTORY_ORIGIN_GATE_CASES,
    ids=[case.test_id for case in HISTORY_ORIGIN_GATE_CASES],
)
def test_history_parsers_gate_non_path_origin_values(
    tmp_path: pathlib.Path,
    case: HistoryOriginGateCase,
) -> None:
    """History workspace/project fields become origins only when path-like."""
    history = tmp_path / "history.jsonl"
    _write_jsonl(history, [case.row])
    parser = getattr(agentgrep, case.parser)

    records = list(parser(_source(history)))

    assert len(records) == 1
    origin = records[0].origin
    if case.expected_cwd is None:
        assert origin is None
    else:
        assert origin == agentgrep.RecordOrigin(cwd=case.expected_cwd)


def test_metadata_and_origin_agree_on_path_likeness() -> None:
    """One shared predicate decides path rewriting for origin and metadata."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="origin display parity",
        origin=agentgrep.RecordOrigin(cwd="work/proj"),
        metadata={"directory": "work/proj"},
    )

    payload = agentgrep.serialize_search_record(record)

    assert payload["origin"] is not None
    assert payload["origin"]["cwd"] == payload["metadata"]["directory"]


class ProjectDirDecodeCase(t.NamedTuple):
    """One store-written directory name and the path it must decode to."""

    test_id: str
    encoding: OriginEncoding
    #: Directories created under ``tmp_path`` before decoding.
    existing: tuple[str, ...]
    #: ``{root}`` expands to the ``tmp_path`` prefix the fixture ran under.
    name: str
    expected: str | None


PROJECT_DIR_DECODE_CASES: tuple[ProjectDirDecodeCase, ...] = (
    ProjectDirDecodeCase(
        test_id="url-decode-is-lossless",
        encoding=OriginEncoding.URL,
        existing=(),
        name="%2Fwork%2Fmy-proj",
        expected="/work/my-proj",
    ),
    ProjectDirDecodeCase(
        test_id="url-decode-refuses-a-non-path",
        encoding=OriginEncoding.URL,
        existing=(),
        name="session-1234",
        expected=None,
    ),
    ProjectDirDecodeCase(
        test_id="dash-resolves-a-unique-reconstruction",
        encoding=OriginEncoding.DASH,
        existing=("work/proj",),
        name="-{root}-work-proj",
        expected="{root}/work/proj",
    ),
    ProjectDirDecodeCase(
        test_id="dash-resolves-a-literal-dash-in-the-name",
        encoding=OriginEncoding.DASH,
        existing=("work/my-proj",),
        name="-{root}-work-my-proj",
        expected="{root}/work/my-proj",
    ),
    ProjectDirDecodeCase(
        test_id="dash-resolves-a-literal-double-dash",
        encoding=OriginEncoding.DASH,
        existing=("work/my--proj",),
        name="-{root}-work-my--proj",
        expected="{root}/work/my--proj",
    ),
    ProjectDirDecodeCase(
        test_id="dash-needs-no-leading-separator",
        encoding=OriginEncoding.DASH,
        existing=("work/proj",),
        name="{root}-work-proj",
        expected="{root}/work/proj",
    ),
    ProjectDirDecodeCase(
        test_id="dash-refuses-to-fabricate-a-missing-directory",
        encoding=OriginEncoding.DASH,
        existing=(),
        name="-{root}-work-proj",
        expected=None,
    ),
    ProjectDirDecodeCase(
        test_id="dash-refuses-an-ambiguous-reconstruction",
        encoding=OriginEncoding.DASH,
        existing=("work/my-proj", "work/my/proj"),
        name="-{root}-work-my-proj",
        expected=None,
    ),
)


@pytest.mark.parametrize(
    ProjectDirDecodeCase._fields,
    PROJECT_DIR_DECODE_CASES,
    ids=[case.test_id for case in PROJECT_DIR_DECODE_CASES],
)
def test_decode_project_dir(
    test_id: str,
    encoding: OriginEncoding,
    existing: tuple[str, ...],
    name: str,
    expected: str | None,
    tmp_path: pathlib.Path,
) -> None:
    """The decode seam recovers a path exactly, or refuses to name one.

    The dash cases carry the load: the encoding is lossy, so a reconstruction
    is trusted only when exactly one split lands on a directory that exists. A
    fabricated ``cwd`` would make repo-scoped filtering silently skip the
    user's own project, so ambiguity and non-existence must both stay ``None``.
    """
    _ = test_id
    for relative in existing:
        (tmp_path / relative).mkdir(parents=True)
    root = str(tmp_path).lstrip("/").replace("/", "-")

    decoded = decode_project_dir(name.format(root=root), encoding=encoding)

    assert decoded == (None if expected is None else expected.format(root=tmp_path))


class DashDecodeLimitCase(t.NamedTuple):
    """One bound on dash reconstruction, tightened until it must refuse."""

    test_id: str
    limit: str
    value: int


DASH_DECODE_LIMIT_CASES: tuple[DashDecodeLimitCase, ...] = (
    DashDecodeLimitCase(
        test_id="probe-budget-exhausted",
        limit="DASH_DECODE_PROBE_BUDGET",
        value=1,
    ),
    DashDecodeLimitCase(
        test_id="token-cap-exceeded",
        limit="DASH_DECODE_MAX_TOKENS",
        value=1,
    ),
)


@pytest.mark.parametrize(
    DashDecodeLimitCase._fields,
    DASH_DECODE_LIMIT_CASES,
    ids=[case.test_id for case in DASH_DECODE_LIMIT_CASES],
)
def test_decode_project_dir_refuses_past_its_limits(
    test_id: str,
    limit: str,
    value: int,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconstruction is bounded, and hitting a bound is a refusal.

    The walk is filesystem-directed, so a pathological name could otherwise fan
    out combinatorially. Both bounds resolve the same way a missing directory
    does — ``None`` — because a bounded search that gives up knows less, not
    more.
    """
    _ = test_id
    (tmp_path / "work" / "proj").mkdir(parents=True)
    root = str(tmp_path).lstrip("/").replace("/", "-")
    name = f"-{root}-work-proj"
    assert decode_project_dir(name, encoding=OriginEncoding.DASH) == str(tmp_path / "work" / "proj")
    assert getattr(origin, limit) > value

    monkeypatch.setattr(origin, limit, value)

    assert decode_project_dir(name, encoding=OriginEncoding.DASH) is None


def test_decode_project_dir_cache_is_owned_by_the_caller(tmp_path: pathlib.Path) -> None:
    """The decode memo is scoped to the caller, not to the process.

    A module-level ``functools.cache`` would outlive the filesystem it probed:
    the TUI and the MCP server run for hours, and a project created after the
    first miss would stay unresolvable for the life of the process. A caller
    that keeps no cache therefore always sees current directory state.
    """
    root = str(tmp_path).lstrip("/").replace("/", "-")
    name = f"-{root}-work-proj"
    cache: dict[str, str | None] = {}

    assert decode_project_dir(name, encoding=OriginEncoding.DASH, cache=cache) is None
    (tmp_path / "work" / "proj").mkdir(parents=True)

    # The caller's own cache still answers from the state it probed...
    assert decode_project_dir(name, encoding=OriginEncoding.DASH, cache=cache) is None
    # ...while an uncached decode sees the directory that now exists.
    assert decode_project_dir(name, encoding=OriginEncoding.DASH) == str(tmp_path / "work" / "proj")


class CwdDigestCase(t.NamedTuple):
    """One path segment and whether it may be labelled a ``cwd_hash``."""

    test_id: str
    segment: str
    length: int
    expected: str | None


CWD_DIGEST_CASES: tuple[CwdDigestCase, ...] = (
    CwdDigestCase(
        test_id="md5-workspace-digest-is-a-hash",
        segment="9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d",
        length=CWD_DIGEST_LENGTH,
        expected="9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d",
    ),
    CwdDigestCase(
        test_id="sibling-storage-directory-is-not-a-hash",
        segment="globalStorage",
        length=CWD_DIGEST_LENGTH,
        expected=None,
    ),
    CwdDigestCase(
        test_id="dotted-parent-directory-is-not-a-hash",
        segment=".cursor",
        length=CWD_DIGEST_LENGTH,
        expected=None,
    ),
    CwdDigestCase(
        test_id="uppercase-hex-is-not-the-written-shape",
        segment="9B2A1F0C4D3E5A6B7C8D9E0F1A2B3C4D",
        length=CWD_DIGEST_LENGTH,
        expected=None,
    ),
    CwdDigestCase(
        test_id="truncated-digest-is-not-a-hash",
        segment="9b2a1f0c4d3e5a6b",
        length=CWD_DIGEST_LENGTH,
        expected=None,
    ),
    CwdDigestCase(
        test_id="store-with-a-narrower-digest-names-its-own-length",
        segment="9b2a1f0c4d3e5a6b",
        length=16,
        expected="9b2a1f0c4d3e5a6b",
    ),
)


@pytest.mark.parametrize(
    CwdDigestCase._fields,
    CWD_DIGEST_CASES,
    ids=[case.test_id for case in CWD_DIGEST_CASES],
)
def test_origin_cwd_hash_guards_the_digest_shape(
    test_id: str,
    segment: str,
    length: int,
    expected: str | None,
) -> None:
    """A path segment becomes a ``cwd_hash`` only when it has a digest's shape."""
    _ = test_id

    assert origin_cwd_hash(segment, length=length) == expected
    assert is_cwd_digest(segment, length=length) is (expected is not None)


def test_cursor_legacy_state_db_reports_no_workspace_digest(tmp_path: pathlib.Path) -> None:
    """The legacy global database sits under ``.cursor``, which is not a digest.

    Without the shape guard the parent segment becomes the ``cwd_hash``, so
    ``cwd_hash:.cursor`` answers with records — a searchable identity no Cursor
    build ever wrote.
    """
    db_path = tmp_path / ".cursor" / "state.vscdb"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    _ = connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("aiService.prompts", json.dumps({"prompts": [{"text": "legacy prompt"}]})),
    )
    connection.commit()
    connection.close()

    records = list(
        agentgrep.parse_cursor_state_db(
            _source(
                db_path,
                agent="cursor-ide",
                store="cursor-ide.state_vscdb",
                adapter_id="cursor_ide.state_vscdb_legacy.v1",
                source_kind="sqlite",
            ),
        ),
    )

    assert [record.text for record in records] == ["legacy prompt"]
    assert records[0].origin is None
