"""The adapter x field coverage ledger.

One hand-written row per store, declaring what that store's records must carry:
the model slug, and which :class:`~agentgrep.RecordOrigin` fields are populated.
Every row is exercised through the *public search surface* —
:func:`~agentgrep.discover_sources_for_search` then
:func:`~agentgrep.search_sources`, the two calls the CLI and the MCP server both
make — so a row cannot pass by populating a field on a record no user can reach.

Four properties are asserted per row.

reachability
    The store's sources survive discovery at the row's documented scope, and
    emit at least one record.
population
    ``model`` equals the exact slug the fixture wrote, and every origin field in
    ``expect_origin`` carries a value. ``expect_model`` is a slug rather than a
    boolean because the values that fail here are *plausible*: Codex records the
    provider (``openai``) next to the model, and a boolean row is satisfied by
    either.
no fabrication
    No origin field in ``forbid_origin`` is ever set, and no origin value
    appears that the fixture never wrote. A superset check passes vacuously when
    the right answer is "nothing", which is exactly the answer for a lossy
    directory name that does not resolve and for a path segment that is not a
    digest.
pruning truth
    :attr:`~agentgrep.records.SourceOriginSummary.complete_fields` is a claim
    :mod:`agentgrep.query.evaluate` acts on by dropping whole sources before a
    byte is read. It is a claim about **values**, not names: a source that
    claims ``cwd`` completeness while one of its records carries a *different*
    ``cwd`` prunes away its own matching record and the search exits 0. So the
    assertion is value-level — every origin value a record carries for a claimed
    field must appear in the summary's own origins — and ``complete_fields``
    itself must stay within :data:`~agentgrep.origin.PRUNABLE_ORIGIN_FIELDS`.

A store with a known population gap carries a ``gap`` string and runs as a
strict xfail. Closing the gap means deleting that one string; ``xfail_strict``
turns a row that starts passing while still claiming a gap into a failure, so
the ledger cannot drift into telling the reader about a bug that is fixed.

:func:`test_searchable_stores_have_a_coverage_row` closes the table: every
``DEFAULT_SEARCH`` and ``INSPECTABLE`` store must have a row here or a named
entry in :data:`UNCOVERED_SEARCHABLE_STORES`. Adding a searchable store to the
catalogue without one fails this suite.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import pathlib
import sqlite3
import typing as t
import urllib.parse

import pytest

import agentgrep
from agentgrep.origin import (
    PRUNABLE_ORIGIN_FIELDS,
    origin_field_values,
    record_origin_field_values,
)
from agentgrep.records import SearchRecord, SourceHandle
from agentgrep.store_catalog import CATALOG
from agentgrep.stores import StoreCoverage

SeedStore = cabc.Callable[[pathlib.Path], None]

TERM = "ledgerterm"
"""The single term every seeded record's text contains."""

# Every seeded store points at one working directory, one branch, one remote.
# Rows assert the literal values, so a parser that recovers a *different* path
# fails loudly instead of merely looking populated.
PROJECT_DIR = "/work/agentgrep-demo"
BRANCH = "feat/origin-coverage"
REMOTE = "https://github.com/tony/agentgrep.git"

# Digest shapes are per store, taken from the real directories: Gemini names a
# project directory with a sha256, Cursor with an md5, Pi with a 16-hex prefix.
# The digest a store wrote is the only legal source of a ``cwd_hash``; agentgrep
# never hashes a recovered ``cwd`` to synthesize one.
GEMINI_PROJECT_DIGEST = "3f9a6c1d2e4b5a7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e"
CURSOR_WORKSPACE_DIGEST = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
CURSOR_CHATS_DIGEST = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
PI_PROJECT_DIGEST = "0badc0ffee123456"

# Root-override environment variables. A hermetic agent home has to clear every
# one, or a developer's own export silently points discovery at real history.
AGENT_ROOT_ENV_VARS: tuple[str, ...] = (
    "CODEX_HOME",
    "CODEX_SQLITE_HOME",
    "GEMINI_CLI_HOME",
    "GROK_HOME",
    "OPENCODE_DB",
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "XDG_DATA_HOME",
)


@pytest.fixture
def agent_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Return an empty, hermetic ``$HOME`` for agent-store discovery."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for name in AGENT_ROOT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return home


# --- Fixture writers -------------------------------------------------------


def _write_text(path: pathlib.Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")


def _write_json(path: pathlib.Path, payload: object) -> None:
    """Write one JSON document, creating parent directories."""
    _write_text(path, json.dumps(payload))


def _write_jsonl(path: pathlib.Path, rows: cabc.Sequence[object]) -> None:
    """Write JSONL rows, creating parent directories."""
    _write_text(path, "".join(f"{json.dumps(row)}\n" for row in rows))


def _write_sqlite(
    path: pathlib.Path,
    schema: cabc.Sequence[str],
    rows: cabc.Sequence[tuple[str, cabc.Sequence[object]]],
) -> None:
    """Create a SQLite store fixture from DDL plus ``(sql, parameters)`` inserts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        for statement in schema:
            _ = connection.execute(statement)
        for statement, parameters in rows:
            _ = connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _protobuf_string(text: str) -> bytes:
    r"""Encode ``text`` as one length-delimited protobuf string field.

    The Antigravity CLI and Cursor CLI blob stores publish no schema, so
    agentgrep walks the wire format generically. A fixture blob therefore has to
    be real wire format, not a JSON stand-in.

    Examples
    --------
    >>> _protobuf_string("hi")
    b'\n\x02hi'
    """
    raw = text.encode("utf-8")
    length = len(raw)
    varint = bytearray()
    while True:
        chunk = length & 0x7F
        length >>= 7
        if length:
            varint.append(chunk | 0x80)
            continue
        varint.append(chunk)
        break
    return b"\x0a" + bytes(varint) + raw


def _dash_encode(path: str) -> str:
    """Encode an absolute path the way Cursor CLI names a project directory."""
    return path.replace("/", "-")


def _resolvable_project(home: pathlib.Path) -> pathlib.Path:
    """Return the project directory the dash decode is expected to recover.

    The dash reconstruction is filesystem-directed — it accepts a name only when
    exactly one split names a directory that exists — so the fixture has to
    create the project it encodes.
    """
    return home / "src" / "demo"


def _cursor_ide_user_dir(home: pathlib.Path) -> pathlib.Path:
    """Return the Cursor IDE ``User/`` directory for the platform tests run on."""
    return home / ".config" / "Cursor" / "User"


def _gemini_project_dir(home: pathlib.Path) -> pathlib.Path:
    """Return the Gemini per-project scratch directory."""
    return home / ".gemini" / "tmp" / GEMINI_PROJECT_DIGEST


def _grok_project_dir(home: pathlib.Path) -> pathlib.Path:
    """Return the Grok session directory, whose name URL-encodes the project."""
    return home / ".grok" / "sessions" / urllib.parse.quote(PROJECT_DIR, safe="")


# --- Store seeds -----------------------------------------------------------
#
# One seed per row. Each writes exactly the store its row covers, so a row
# cannot pass on another store's records.


def seed_codex_sessions(home: pathlib.Path) -> None:
    """Codex rollout: the model slug lives in ``turn_context`` (#99).

    ``session_meta`` carries only ``model_provider``. Reading it as the model
    labels every Codex record ``openai``.
    """
    _write_jsonl(
        home / ".codex" / "sessions" / "2026" / "07" / "11" / "rollout-ledger.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-session-1",
                    "cwd": PROJECT_DIR,
                    "model_provider": "openai",
                    "git": {"branch": BRANCH},
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.4-codex", "cwd": PROJECT_DIR},
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-11T10:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"{TERM} codex rollout"}],
                },
            },
        ],
    )


def seed_codex_state_db(home: pathlib.Path) -> None:
    """Codex state DB: model, cwd, and git columns sit on the ``threads`` row.

    The column names mirror the shipped ``state_5.sqlite`` schema. The second
    row predates those columns and keeps NULLs, so the missing-value path is
    exercised alongside the populated one.
    """
    _write_sqlite(
        home / ".codex" / "state_5.sqlite",
        (
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, first_user_message TEXT, preview TEXT, title TEXT, "
            "updated_at_ms INTEGER, model TEXT, model_provider TEXT, cwd TEXT, "
            "git_branch TEXT, git_sha TEXT, git_origin_url TEXT)",
        ),
        (
            (
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "codex-thread-1",
                    f"{TERM} codex thread first prompt",
                    f"{TERM} codex thread preview",
                    "Ledger thread",
                    1_783_000_000_000,
                    "gpt-5.4",
                    "openai",
                    PROJECT_DIR,
                    BRANCH,
                    "0" * 40,
                    REMOTE,
                ),
            ),
            (
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "codex-thread-legacy",
                    f"{TERM} codex legacy thread",
                    None,
                    None,
                    1_782_000_000_000,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ),
        ),
    )


def seed_claude_projects_session(home: pathlib.Path) -> None:
    """Claude project session — the in-repo reference for model, cwd, and branch."""
    _write_jsonl(
        home / ".claude" / "projects" / "-work-agentgrep-demo" / "claude-session-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "claude-session-1",
                "cwd": PROJECT_DIR,
                "gitBranch": BRANCH,
                "timestamp": "2026-07-11T10:00:00Z",
                "message": {"role": "user", "content": f"{TERM} claude prompt"},
            },
            {
                "type": "assistant",
                "sessionId": "claude-session-1",
                "cwd": PROJECT_DIR,
                "gitBranch": BRANCH,
                "timestamp": "2026-07-11T10:00:05Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": f"{TERM} claude answer"}],
                },
            },
        ],
    )


def seed_cursor_ide_state_vscdb(home: pathlib.Path) -> None:
    """Cursor IDE global state: the composer names its model and its worktree.

    The turns carry a numeric ``type`` and no ``role`` — the shape a real
    ``cursorDiskKV`` writes. A reader that only knows ``role`` walks past every
    one of them, so this fixture is what tells a passing row apart from a fix
    that emits nothing at all.

    ``globalStorage`` is not a digest, so this store must never report a
    ``cwd_hash`` — a fabricated digest answers ``cwd_hash:`` predicates with a
    value no Cursor build ever wrote.
    """
    composer = {
        "composerId": "composer-1",
        "modelConfig": {"modelName": "claude-4.5-sonnet"},
        "gitWorktree": {"worktreePath": PROJECT_DIR, "branchName": BRANCH},
        "conversation": [
            {"type": 1, "text": f"{TERM} cursor ide composer prompt"},
            {"type": 2, "text": f"{TERM} cursor ide composer reply"},
        ],
    }
    bubble = {
        "type": 1,
        "text": f"{TERM} cursor ide bubble prompt",
        "modelInfo": {"modelName": "claude-4.5-sonnet"},
    }
    _write_sqlite(
        _cursor_ide_user_dir(home) / "globalStorage" / "state.vscdb",
        ("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)",),
        (
            (
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                ("composerData:composer-1", json.dumps(composer)),
            ),
            (
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                ("bubbleId:composer-1:bubble-1", json.dumps(bubble)),
            ),
        ),
    )


def seed_cursor_ide_workspace_state(home: pathlib.Path) -> None:
    """Cursor IDE workspace state: the folder sits in the sibling ``workspace.json``.

    The database's own directory name is the workspace digest, so this store is
    the one place a ``cwd`` and a ``cwd_hash`` are both recoverable.
    """
    workspace = _cursor_ide_user_dir(home) / "workspaceStorage" / CURSOR_WORKSPACE_DIGEST
    _write_sqlite(
        workspace / "state.vscdb",
        ("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)",),
        (
            (
                "INSERT INTO ItemTable VALUES (?, ?)",
                (
                    "aiService.prompts",
                    json.dumps([{"text": f"{TERM} cursor workspace prompt", "commandType": 4}]),
                ),
            ),
        ),
    )
    _write_json(workspace / "workspace.json", {"folder": f"file://{PROJECT_DIR}"})


def _cursor_cli_transcript(home: pathlib.Path, project: str, session: str) -> pathlib.Path:
    """Return the transcript path for one dash-encoded Cursor CLI project."""
    return (
        home
        / ".cursor"
        / "projects"
        / _dash_encode(project)
        / "agent-transcripts"
        / session
        / f"{session}.jsonl"
    )


def seed_cursor_cli_transcripts(home: pathlib.Path) -> None:
    """Cursor CLI transcript under a dash-encoded name that resolves on disk."""
    project = _resolvable_project(home)
    project.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        _cursor_cli_transcript(home, str(project), "cursor-session-1"),
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": f"{TERM} cursor cli prompt"}]},
            },
        ],
    )


def seed_cursor_cli_transcripts_unresolvable(home: pathlib.Path) -> None:
    """Cursor CLI transcript whose dash-encoded name has no on-disk answer.

    Nothing under ``/`` matches, so every candidate split is a guess. The naive
    inverse would emit ``/some/repo/with-dashes`` (or three other paths); the
    ledger requires ``origin.cwd`` to stay ``None``, because a fabricated cwd
    does not merely omit a result — it makes repo-scoped filtering silently skip
    the user's own project.
    """
    _write_jsonl(
        _cursor_cli_transcript(home, "/some/repo/with-dashes", "cursor-session-2"),
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": f"{TERM} cursor cli unresolved"}]},
            },
        ],
    )


def seed_cursor_cli_subagent_transcripts(home: pathlib.Path) -> None:
    """Cursor CLI subagent transcript — the same dash-encoded project name."""
    project = _resolvable_project(home)
    project.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        home
        / ".cursor"
        / "projects"
        / _dash_encode(str(project))
        / "agent-transcripts"
        / "cursor-session-1"
        / "subagents"
        / "subagent-1.jsonl",
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": f"{TERM} cursor cli subagent"}]},
            },
        ],
    )


def seed_cursor_cli_chats(home: pathlib.Path) -> None:
    """Cursor CLI chats: ``lastUsedModel`` sits hex-encoded in the ``meta`` row.

    The workspace identity here is a digest directory and nothing else — the
    literal path exists only as unstructured bytes inside the blobs. So this
    store gets a ``cwd_hash`` and must never get a ``cwd``.
    """
    meta_value = json.dumps({"lastUsedModel": "claude-4.5-sonnet"}).encode("utf-8").hex()
    _write_sqlite(
        home / ".config" / "cursor" / "chats" / CURSOR_CHATS_DIGEST / "cursor-chat-1" / "store.db",
        (
            "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)",
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
        ),
        (
            (
                "INSERT INTO blobs VALUES (?, ?)",
                ("blob-1", _protobuf_string(f"{TERM} cursor cli chat blob text")),
            ),
            ("INSERT INTO meta VALUES (?, ?)", ("0", meta_value)),
        ),
    )


def seed_gemini_tmp_chats(home: pathlib.Path) -> None:
    """Gemini chat session: the cwd is ``directories[0]`` on the metadata line."""
    _write_jsonl(
        _gemini_project_dir(home) / "chats" / "session-ledger.jsonl",
        [
            {
                "kind": "main",
                "sessionId": "gemini-session-1",
                "projectHash": GEMINI_PROJECT_DIGEST,
                "directories": [PROJECT_DIR],
            },
            {
                "id": "1",
                "type": "user",
                "content": f"{TERM} gemini prompt",
                "timestamp": "2026-07-11T10:00:00Z",
            },
            {
                "id": "2",
                "type": "gemini",
                "content": f"{TERM} gemini answer",
                "model": "gemini-3-pro",
                "timestamp": "2026-07-11T10:00:05Z",
            },
        ],
    )


def seed_gemini_tmp_chats_legacy(home: pathlib.Path) -> None:
    """Legacy Gemini chat: only ``projectHash`` in-record; the cwd is a sibling file."""
    project = _gemini_project_dir(home)
    _write_text(project / ".project_root", f"{PROJECT_DIR}\n")
    _write_json(
        project / "chats" / "session-legacy.json",
        {
            "sessionId": "gemini-legacy-1",
            "projectHash": GEMINI_PROJECT_DIGEST,
            "messages": [
                {
                    "id": "1",
                    "type": "user",
                    "content": f"{TERM} gemini legacy prompt",
                    "timestamp": "2026-07-11T10:00:00Z",
                },
            ],
        },
    )


def seed_gemini_tmp_logs(home: pathlib.Path) -> None:
    """Gemini prompt log: the cwd is the sibling ``.project_root`` file."""
    project = _gemini_project_dir(home)
    _write_text(project / ".project_root", f"{PROJECT_DIR}\n")
    _write_json(
        project / "logs.json",
        [
            {
                "sessionId": "gemini-session-1",
                "type": "user",
                "message": f"{TERM} gemini log entry",
                "timestamp": "2026-07-11T10:00:00Z",
            },
        ],
    )


def seed_grok_sessions(home: pathlib.Path) -> None:
    """Grok transcript: ``model_id`` in-record, cwd URL-encoded in the directory name."""
    _write_jsonl(
        _grok_project_dir(home) / "grok-session-1" / "chat_history.jsonl",
        [
            {
                "type": "user",
                "content": f"{TERM} grok prompt",
                "timestamp": "2026-07-11T10:00:00Z",
            },
            {
                "type": "assistant",
                "content": f"{TERM} grok answer",
                "model_id": "grok-4-fast",
                "timestamp": "2026-07-11T10:00:05Z",
            },
        ],
    )


def seed_grok_prompt_history(home: pathlib.Path) -> None:
    """Grok prompt history: the cwd is the URL-encoded parent directory."""
    _write_jsonl(
        _grok_project_dir(home) / "prompt_history.jsonl",
        [
            {
                "timestamp": "2026-07-11T10:00:00Z",
                "session_id": "grok-session-1",
                "prompt": f"{TERM} grok history",
                "is_bash": False,
            },
        ],
    )


def seed_grok_session_search(home: pathlib.Path) -> None:
    """Grok FTS index — the in-repo reference for a store that records its own cwd."""
    _write_sqlite(
        home / ".grok" / "sessions" / "session_search.sqlite",
        (
            "CREATE TABLE session_docs ("
            "session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, updated_at INTEGER NOT NULL, "
            "title TEXT, content TEXT, content_hash TEXT)",
        ),
        (
            (
                "INSERT INTO session_docs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "grok-session-1",
                    PROJECT_DIR,
                    1_783_000_000,
                    "Ledger session",
                    f"{TERM} grok indexed body",
                    "content-hash-1",
                ),
            ),
        ),
    )


def seed_pi_sessions(home: pathlib.Path) -> None:
    """Pi session transcript — the in-repo reference for model and cwd."""
    _write_jsonl(
        home / ".pi" / "agent" / "sessions" / "pi-session-1.jsonl",
        [
            {"type": "session", "id": "pi-session-1", "cwd": PROJECT_DIR},
            {
                "type": "message",
                "timestamp": "2026-07-11T10:00:00Z",
                "message": {
                    "role": "user",
                    "content": f"{TERM} pi transcript",
                    "model": "pi-sonnet",
                },
            },
        ],
    )


def seed_pi_context_mode_db(home: pathlib.Path) -> None:
    """Pi context mode: ``project_dir`` sits in the row beside the hashed file name."""
    _write_sqlite(
        home / ".pi" / "context-mode" / "sessions" / f"{PI_PROJECT_DIGEST}.db",
        (
            "CREATE TABLE session_events ("
            "id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, "
            "data TEXT NOT NULL, created_at TEXT, project_dir TEXT NOT NULL DEFAULT '')",
        ),
        (
            (
                "INSERT INTO session_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    1,
                    "pi-session-1",
                    "decision",
                    f"{TERM} pi context-mode event",
                    "2026-07-11T10:00:00Z",
                    PROJECT_DIR,
                ),
            ),
        ),
    )


def seed_antigravity_cli_conversations(home: pathlib.Path) -> None:
    """Antigravity CLI conversation: the model slug is in ``gen_metadata``, not ``steps``.

    ``gemini-pro-agent`` is the slug the shipped databases carry; the steps blob
    holds the conversation text and no model at all.
    """
    _write_sqlite(
        home / ".gemini" / "antigravity-cli" / "conversations" / "conversation-1.db",
        (
            "CREATE TABLE steps ("
            "idx INTEGER PRIMARY KEY, step_payload BLOB, step_format INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE gen_metadata ("
            "idx INTEGER PRIMARY KEY, data BLOB, size INTEGER NOT NULL DEFAULT 0)",
        ),
        (
            (
                "INSERT INTO steps VALUES (?, ?, ?)",
                (1, _protobuf_string(f"{TERM} antigravity conversation step"), 1),
            ),
            (
                "INSERT INTO gen_metadata VALUES (?, ?, ?)",
                (1, _protobuf_string("gemini-pro-agent"), 32),
            ),
        ),
    )


def seed_antigravity_cli_history(home: pathlib.Path) -> None:
    """Antigravity CLI prompt history — the in-repo reference for its cwd."""
    _write_jsonl(
        home / ".gemini" / "antigravity-cli" / "history.jsonl",
        [
            {
                "display": f"{TERM} antigravity history",
                "timestamp": 1_783_000_000_000,
                "type": "prompt",
                "workspace": PROJECT_DIR,
                "conversationId": "antigravity-1",
            },
        ],
    )


# --- The ledger ------------------------------------------------------------


class AdapterCoverageCase(t.NamedTuple):
    """One store's declared model and origin coverage."""

    test_id: str

    store_id: str
    """The *catalogue* store id. Six rows discover under a different runtime key."""

    agent: agentgrep.AgentName
    scope: agentgrep.SearchScope
    """The documented scope this store is reachable at."""

    seed: SeedStore
    expect_model: str | None
    """The exact model slug, or ``None`` when the store records no model."""

    expect_origin: frozenset[str]
    forbid_origin: frozenset[str] = frozenset()
    """Origin fields that must stay unset — the answer a lossy name has no right to."""

    gap: str | None = None
    """A known population gap. Present rows run as a strict xfail."""


ADAPTER_COVERAGE_CASES: tuple[AdapterCoverageCase, ...] = (
    # -- Codex ---------------------------------------------------------------
    AdapterCoverageCase(
        test_id="codex-sessions",
        store_id="codex.sessions",
        agent="codex",
        scope="conversations",
        seed=seed_codex_sessions,
        expect_model="gpt-5.4-codex",
        expect_origin=frozenset({"cwd", "branch"}),
        gap="model read from session_meta.model_provider, not turn_context.model (#99)",
    ),
    AdapterCoverageCase(
        test_id="codex-state-db",
        store_id="codex.state_db",
        agent="codex",
        scope="conversations",
        seed=seed_codex_state_db,
        expect_model="gpt-5.4",
        expect_origin=frozenset({"cwd", "branch", "remote"}),
    ),
    # -- Claude --------------------------------------------------------------
    AdapterCoverageCase(
        test_id="claude-projects-session",
        store_id="claude.projects.session",
        agent="claude",
        scope="conversations",
        seed=seed_claude_projects_session,
        expect_model="claude-opus-4-8",
        expect_origin=frozenset({"cwd", "branch"}),
    ),
    # -- Cursor IDE ----------------------------------------------------------
    AdapterCoverageCase(
        test_id="cursor-ide-state-vscdb",
        store_id="cursor-ide.state_vscdb",
        agent="cursor-ide",
        scope="conversations",
        seed=seed_cursor_ide_state_vscdb,
        expect_model="claude-4.5-sonnet",
        expect_origin=frozenset({"cwd", "branch"}),
        forbid_origin=frozenset({"cwd_hash"}),
    ),
    AdapterCoverageCase(
        test_id="cursor-ide-workspace-state",
        store_id="cursor-ide.workspace_state",
        agent="cursor-ide",
        scope="conversations",
        seed=seed_cursor_ide_workspace_state,
        expect_model=None,
        expect_origin=frozenset({"cwd", "cwd_hash"}),
    ),
    # -- Cursor CLI ----------------------------------------------------------
    AdapterCoverageCase(
        test_id="cursor-cli-transcripts",
        store_id="cursor-cli.transcripts",
        agent="cursor-cli",
        scope="conversations",
        seed=seed_cursor_cli_transcripts,
        expect_model=None,
        expect_origin=frozenset({"cwd"}),
    ),
    AdapterCoverageCase(
        test_id="cursor-cli-transcripts-unresolvable",
        store_id="cursor-cli.transcripts",
        agent="cursor-cli",
        scope="conversations",
        seed=seed_cursor_cli_transcripts_unresolvable,
        expect_model=None,
        expect_origin=frozenset(),
        forbid_origin=frozenset({"cwd", "cwd_hash"}),
    ),
    AdapterCoverageCase(
        test_id="cursor-cli-subagent-transcripts",
        store_id="cursor-cli.subagent_transcripts",
        agent="cursor-cli",
        scope="conversations",
        seed=seed_cursor_cli_subagent_transcripts,
        expect_model=None,
        expect_origin=frozenset({"cwd"}),
    ),
    AdapterCoverageCase(
        test_id="cursor-cli-chats",
        store_id="cursor-cli.chats",
        agent="cursor-cli",
        scope="conversations",
        seed=seed_cursor_cli_chats,
        expect_model="claude-4.5-sonnet",
        expect_origin=frozenset({"cwd_hash"}),
        forbid_origin=frozenset({"cwd"}),
    ),
    # -- Gemini --------------------------------------------------------------
    AdapterCoverageCase(
        test_id="gemini-tmp-chats",
        store_id="gemini.tmp.chats",
        agent="gemini",
        scope="conversations",
        seed=seed_gemini_tmp_chats,
        expect_model="gemini-3-pro",
        expect_origin=frozenset({"cwd", "cwd_hash"}),
    ),
    AdapterCoverageCase(
        test_id="gemini-tmp-chats-legacy",
        store_id="gemini.tmp.chats_legacy",
        agent="gemini",
        scope="conversations",
        seed=seed_gemini_tmp_chats_legacy,
        expect_model=None,
        expect_origin=frozenset({"cwd", "cwd_hash"}),
    ),
    AdapterCoverageCase(
        test_id="gemini-tmp-logs",
        store_id="gemini.tmp.logs",
        agent="gemini",
        scope="prompts",
        seed=seed_gemini_tmp_logs,
        expect_model=None,
        expect_origin=frozenset({"cwd", "cwd_hash"}),
    ),
    # -- Grok ----------------------------------------------------------------
    AdapterCoverageCase(
        test_id="grok-sessions",
        store_id="grok.sessions",
        agent="grok",
        scope="conversations",
        seed=seed_grok_sessions,
        expect_model="grok-4-fast",
        expect_origin=frozenset({"cwd"}),
    ),
    AdapterCoverageCase(
        test_id="grok-prompt-history",
        store_id="grok.prompt_history",
        agent="grok",
        scope="prompts",
        seed=seed_grok_prompt_history,
        expect_model=None,
        expect_origin=frozenset({"cwd"}),
    ),
    AdapterCoverageCase(
        test_id="grok-session-search",
        store_id="grok.session_search",
        agent="grok",
        scope="conversations",
        seed=seed_grok_session_search,
        expect_model=None,
        expect_origin=frozenset({"cwd"}),
    ),
    # -- Pi ------------------------------------------------------------------
    AdapterCoverageCase(
        test_id="pi-sessions",
        store_id="pi.sessions",
        agent="pi",
        scope="conversations",
        seed=seed_pi_sessions,
        expect_model="pi-sonnet",
        expect_origin=frozenset({"cwd"}),
    ),
    AdapterCoverageCase(
        test_id="pi-context-mode-db",
        store_id="pi.context_mode_db",
        agent="pi",
        scope="conversations",
        seed=seed_pi_context_mode_db,
        expect_model=None,
        expect_origin=frozenset({"cwd", "cwd_hash"}),
    ),
    # -- Antigravity ---------------------------------------------------------
    AdapterCoverageCase(
        test_id="antigravity-cli-conversations",
        store_id="antigravity-cli.conversations",
        agent="antigravity-cli",
        scope="conversations",
        seed=seed_antigravity_cli_conversations,
        expect_model="gemini-pro-agent",
        expect_origin=frozenset(),
        gap="the gen_metadata table is never opened",
    ),
    AdapterCoverageCase(
        test_id="antigravity-cli-history",
        store_id="antigravity-cli.history",
        agent="antigravity-cli",
        scope="prompts",
        seed=seed_antigravity_cli_history,
        expect_model=None,
        expect_origin=frozenset({"cwd"}),
    ),
)

UNCOVERED_SEARCHABLE_STORES: frozenset[str] = frozenset(
    {
        # No model, cwd, or branch on disk in any shipped format. There is
        # nothing for a row to claim; the adapters are covered elsewhere.
        "claude.history",
        "codex.history",
        "cursor-cli.prompt_history",
        "vscode.inline_history",
        # Correct today and covered by their own adapter tests. A row here would
        # add a fixture without adding a claim.
        "claude.projects.subagent",
        "cursor-cli.ai_tracking",
        "grok.subagents",
        "opencode.db",
        "vscode.chat_sessions",
        # Opt-in inventory surfaces: skills, rules, memories, plans, todos,
        # usage counters, tool output. They are content agentgrep can show, not
        # agent turns with a model and a working directory.
        "antigravity-cli.brain",
        "antigravity-cli.transcript",
        "antigravity-ide.brain",
        "antigravity-ide.brain_resolved",
        "antigravity-ide.skills",
        "claude.commands",
        "claude.memory_files",
        "claude.plans",
        "claude.plugins_cache",
        "claude.project_instructions",
        "claude.projects.memory",
        "claude.projects.session_memory",
        "claude.projects.workflows",
        "claude.skills",
        "claude.store_db",
        "claude.tasks",
        "claude.teams",
        "claude.todos",
        "claude.usage_data",
        "codex.goals_db",
        "codex.instructions",
        "codex.memories",
        "codex.memories_db",
        "codex.plugin_marketplace",
        "codex.plugins",
        "codex.project_skills",
        "codex.rules",
        "codex.session_index",
        "codex.skills",
        "cursor-cli.agent_tools",
        "cursor-cli.skills",
        "cursor-cli.uploads",
        "gemini.memory",
        "gemini.tool_outputs",
        "grok.memory",
        "grok.plans",
    },
)
"""Searchable stores with no coverage row, each excused on purpose.

Shrinking this set is the point. Growing it silently is what
:func:`test_searchable_stores_have_a_coverage_row` prevents.
"""

ORIGIN_FIELDS: tuple[str, ...] = tuple(
    field.name for field in dataclasses.fields(agentgrep.RecordOrigin)
)


def _runtime_store_keys(store_id: str) -> frozenset[str]:
    """Return the runtime store keys a catalogue row discovers under.

    :attr:`~agentgrep.stores.StoreDescriptor.store_id` and
    :attr:`~agentgrep.stores.DiscoverySpec.store` are not the same string for
    every row — ``claude.projects.session`` discovers as ``claude.projects`` —
    and records carry the runtime key.
    """
    return frozenset(spec.store for spec in CATALOG.by_id(store_id).discovery)


def _populated_origin_fields(records: cabc.Iterable[SearchRecord]) -> frozenset[str]:
    """Return every origin field name carrying a value on at least one record."""
    return frozenset(
        name
        for record in records
        if record.origin is not None
        for name in ORIGIN_FIELDS
        if getattr(record.origin, name)
    )


def _populated_origin_values(records: cabc.Iterable[SearchRecord]) -> frozenset[str]:
    """Return every origin value carried by any of ``records``."""
    return frozenset(
        value
        for record in records
        if record.origin is not None
        for name in ORIGIN_FIELDS
        if (value := t.cast("str | None", getattr(record.origin, name)))
    )


def _legal_origin_values(home: pathlib.Path) -> frozenset[str]:
    """Return every origin value the fixtures actually wrote.

    Anything else on a record is invented: a dash decode that guessed, or a
    sibling directory name mistaken for a digest.
    """
    return frozenset(
        {
            PROJECT_DIR,
            BRANCH,
            REMOTE,
            GEMINI_PROJECT_DIGEST,
            CURSOR_WORKSPACE_DIGEST,
            CURSOR_CHATS_DIGEST,
            PI_PROJECT_DIGEST,
            str(_resolvable_project(home)),
        },
    )


def _search(
    home: pathlib.Path,
    agent: agentgrep.AgentName,
    scope: agentgrep.SearchScope,
) -> tuple[list[SourceHandle], list[SearchRecord]]:
    """Run one real search through the public discovery and execution surface."""
    backends = agentgrep.BackendSelection(None, None, None)
    query = agentgrep.SearchQuery(
        terms=(TERM,),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(agent,),
        limit=None,
        dedupe=False,
    )
    sources = agentgrep.discover_sources_for_search(home, query, backends)
    return sources, agentgrep.search_sources(query, sources, backends)


def _assert_summary_claims_only_values_it_emits(
    source: SourceHandle,
    records: cabc.Sequence[SearchRecord],
) -> None:
    """Assert a source's pruning claim holds at the level pruning acts on: values.

    ``complete_fields`` tells :mod:`agentgrep.query.evaluate` that
    ``summary.origins`` enumerates *every* value the source's records carry for
    that field. A record carrying a value the summary never lists is not a
    cosmetic drift — a query for that value evaluates the summary to "no", the
    source is dropped before it is opened, and the matching record disappears
    from a search that reports success.
    """
    summary = source.origin_summary
    if summary is None:
        return
    overreach = summary.complete_fields - PRUNABLE_ORIGIN_FIELDS
    assert not overreach, (
        f"{source.store} claims complete origin fields {sorted(overreach)} outside "
        f"PRUNABLE_ORIGIN_FIELDS; only a property of the source's own location can be "
        "claimed complete"
    )
    from_source = [record for record in records if record.path == source.path]
    for field in sorted(summary.complete_fields):
        claimed = {
            value for origin in summary.origins for value in origin_field_values(origin, field)
        }
        emitted = {
            value for record in from_source for value in record_origin_field_values(record, field)
        }
        unclaimed = sorted(emitted - claimed)
        assert not unclaimed, (
            f"{source.store} claims {field!r} complete but emits {unclaimed}, which its "
            f"summary never lists ({sorted(claimed)}); source pruning would delete those "
            "records from a matching search"
        )


@pytest.mark.parametrize(
    AdapterCoverageCase._fields,
    [
        pytest.param(
            *case,
            marks=(
                ()
                if case.gap is None
                else pytest.mark.xfail(strict=True, reason=f"population gap: {case.gap}")
            ),
        )
        for case in ADAPTER_COVERAGE_CASES
    ],
    ids=[case.test_id for case in ADAPTER_COVERAGE_CASES],
)
def test_adapter_coverage(
    test_id: str,
    store_id: str,
    agent: agentgrep.AgentName,
    scope: agentgrep.SearchScope,
    seed: SeedStore,
    expect_model: str | None,
    expect_origin: frozenset[str],
    forbid_origin: frozenset[str],
    gap: str | None,
    agent_home: pathlib.Path,
) -> None:
    """Each covered store is reachable and carries the fields its row declares."""
    _ = gap
    seed(agent_home)
    store_keys = _runtime_store_keys(store_id)
    sources, all_records = _search(agent_home, agent, scope)
    store_sources = [source for source in sources if source.store in store_keys]
    records = [record for record in all_records if record.store in store_keys]

    # Reachability: through the search surface, never by calling the parser.
    assert store_sources, f"{test_id}: {store_id} is undiscovered at scope={scope}"
    assert records, f"{test_id}: {store_id} yielded no records at scope={scope}"

    # Hermeticity: a row that reaches a store outside the seeded home is being
    # answered by the developer's own history — Cursor and VS Code probe the
    # Windows host under WSL from a path that no ``$HOME`` override can move.
    strays = sorted(
        str(source.path) for source in store_sources if not source.path.is_relative_to(agent_home)
    )
    assert not strays, f"{test_id}: {store_id} discovered sources outside the seeded home: {strays}"

    # Population.
    models = {record.model for record in records if record.model}
    if expect_model is None:
        assert not models, f"{test_id}: {store_id} invented a model: {sorted(models)}"
    else:
        assert expect_model in models, (
            f"{test_id}: {store_id} model {sorted(models)} does not include {expect_model!r}"
        )
    populated = _populated_origin_fields(records)
    missing = sorted(expect_origin - populated)
    assert not missing, f"{test_id}: {store_id} never populates origin fields {missing}"

    # No fabrication: neither a forbidden field nor an invented value.
    fabricated = sorted(forbid_origin & populated)
    assert not fabricated, f"{test_id}: {store_id} set origin fields {fabricated} it cannot know"
    invented = sorted(_populated_origin_values(records) - _legal_origin_values(agent_home))
    assert not invented, f"{test_id}: {store_id} emitted origin values nothing wrote: {invented}"

    # Pruning truth.
    for source in store_sources:
        _assert_summary_claims_only_values_it_emits(source, records)


def test_searchable_stores_have_a_coverage_row() -> None:
    """Every searchable store is either covered here or explicitly excused.

    Search opens the ``DEFAULT_SEARCH`` and ``INSPECTABLE`` tiers
    (:data:`~agentgrep.stores.SEARCHABLE_COVERAGE`). Adding a store to either
    without a ledger row fails here rather than shipping unclaimed.
    """
    searchable = {
        descriptor.store_id
        for descriptor in CATALOG.stores
        if descriptor.coverage_level in {StoreCoverage.DEFAULT_SEARCH, StoreCoverage.INSPECTABLE}
    }
    covered = {case.store_id for case in ADAPTER_COVERAGE_CASES}

    unclaimed = sorted(searchable - covered - UNCOVERED_SEARCHABLE_STORES)
    assert not unclaimed, (
        f"searchable stores with no coverage row: {unclaimed}. Add an AdapterCoverageCase, "
        "or name each in UNCOVERED_SEARCHABLE_STORES with a reason."
    )

    stale = sorted(UNCOVERED_SEARCHABLE_STORES - searchable)
    assert not stale, f"UNCOVERED_SEARCHABLE_STORES names non-searchable stores: {stale}"

    redundant = sorted(UNCOVERED_SEARCHABLE_STORES & covered)
    assert not redundant, f"stores both covered and excused: {redundant}"


def test_coverage_rows_name_real_catalog_stores() -> None:
    """Each row names a catalogue store that ships a discovery spec to reach."""
    for case in ADAPTER_COVERAGE_CASES:
        descriptor = CATALOG.by_id(case.store_id)

        assert descriptor.agent == case.agent, f"{case.test_id}: agent disagrees with the catalogue"
        assert descriptor.discovery, f"{case.test_id}: {case.store_id} has no discovery spec"


def test_origin_fields_are_all_declarable() -> None:
    """The ledger's field vocabulary is the record's, so a new origin field is visible.

    A field added to :class:`~agentgrep.RecordOrigin` that no row can name would
    let a whole dimension of coverage go unclaimed.
    """
    declared = {
        field
        for case in ADAPTER_COVERAGE_CASES
        for field in (case.expect_origin | case.forbid_origin)
    }

    assert declared <= set(ORIGIN_FIELDS), f"rows name unknown origin fields: {sorted(declared)}"
