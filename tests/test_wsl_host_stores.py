"""Windows-host and Claude desktop store discovery.

One synthetic tree stands in for a WSL machine: a Linux home plus a Windows
users directory reached through ``AGENTGREP_WSL_USERS_ROOT``. The cases pin
which files discovery reaches, which it must skip, and that no displayed path
names the Windows account.
"""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

from agentgrep import wsl
from agentgrep._text import format_display_path
from agentgrep.adapters import iter_source_records
from agentgrep.discovery import discover_sources
from agentgrep.records import AgentName, BackendSelection, SourceHandle

_NO_BACKENDS = BackendSelection(find_tool=None, grep_tool=None, json_tool=None)
_WINDOWS_USER = "windows-account-name"
_COWORK = "AppData/Roaming/Claude/local-agent-mode-sessions/acct/org/local_s1"


class HostTree(t.NamedTuple):
    """Synthetic WSL machine: the Linux home and one Windows profile."""

    home: pathlib.Path
    profile: pathlib.Path


def _write_transcript(path: pathlib.Path, session_id: str) -> None:
    """Write a one-prompt Claude Code style transcript."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "type": "user",
        "uuid": f"{session_id}-u1",
        "sessionId": session_id,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"role": "user", "content": "synthetic prompt about widgets"},
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


@pytest.fixture
def host_tree(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> HostTree:
    """Build the synthetic machine and point the WSL probes at it."""
    home = tmp_path / "home"
    users = tmp_path / "Users"
    profile = users / _WINDOWS_USER
    _write_transcript(home / ".claude/projects/-work-demo/local-1.jsonl", "local-1")
    _write_transcript(profile / ".claude/projects/ssh-a1/local-1.jsonl", "local-1")
    _write_transcript(profile / ".claude/projects/ssh-a1/remote-only.jsonl", "remote-only")
    _write_transcript(profile / ".claude/projects/C--Users-x-work/windows-1.jsonl", "windows-1")
    _write_transcript(profile / f"{_COWORK}/.claude/projects/-sessions-demo/cowork-1.jsonl", "c1")
    _write_transcript(profile / f"{_COWORK}/audit.jsonl", "audit")
    _write_transcript(
        home / "Library/Application Support/Claude/local-agent-mode-sessions/a/o/local_m"
        "/.claude/projects/-sessions-mac/cowork-mac.jsonl",
        "cowork-mac",
    )
    _write_transcript(
        profile / ".codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-abc.jsonl",
        "codex-1",
    )
    _write_transcript(
        profile / ".cursor/projects/p/agent-transcripts/cursor-1/cursor-1.jsonl",
        "cursor-1",
    )
    _write_transcript(users / "Public/.claude/projects/-x/public-1.jsonl", "public-1")
    session = profile / "AppData/Roaming/Claude/claude-code-sessions/acct/org/local_d1.json"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "sessionId": "local_d1",
                "cliSessionId": "windows-1",
                "title": "Synthetic session title",
                "cwd": "/work/demo",
                "model": "demo-model",
                "createdAt": 1767225600000,
                "lastActivityAt": 1767225660000,
                "completedTurns": 3,
                "wslConfig": {"distro": "Demo-Distro"},
                "accountName": "must not be copied",
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(wsl.USERS_ROOT_ENV, str(users))
    monkeypatch.setattr(wsl, "is_wsl", lambda proc_version=None: True)
    return HostTree(home=home, profile=profile)


def _discover(tree: HostTree, agent: AgentName) -> list[SourceHandle]:
    return discover_sources(tree.home, (agent,), _NO_BACKENDS, include_non_default=True)


class StoreCase(t.NamedTuple):
    """One file and whether discovery must reach it."""

    test_id: str
    agent: AgentName
    file_name: str
    store: str
    expected_count: int


STORE_CASES: tuple[StoreCase, ...] = (
    StoreCase("cowork-on-windows-host", "claude", "cowork-1.jsonl", "claude.projects", 1),
    StoreCase("cowork-on-macos", "claude", "cowork-mac.jsonl", "claude.projects", 1),
    StoreCase("cowork-audit-log-skipped", "claude", "audit.jsonl", "claude.projects", 0),
    StoreCase("desktop-session-index", "claude", "local_d1.json", "claude.desktop_sessions", 1),
)


@pytest.mark.parametrize("case", STORE_CASES, ids=[case.test_id for case in STORE_CASES])
def test_windows_host_store_discovery(host_tree: HostTree, case: StoreCase) -> None:
    """Discovery reaches Windows-side and desktop stores and skips copies and service profiles."""
    sources = _discover(host_tree, case.agent)
    matches = [s for s in sources if s.path.name == case.file_name and s.store == case.store]
    assert len(matches) == case.expected_count
    assert not any(_WINDOWS_USER in format_display_path(s.path) for s in sources)


def test_desktop_session_becomes_title_record(host_tree: HostTree) -> None:
    """A Code-tab session file yields one title record keyed by its transcript session."""
    source = next(s for s in _discover(host_tree, "claude") if s.store == "claude.desktop_sessions")
    (record,) = iter_source_records(source)
    assert (record.text, record.session_id, record.model) == (
        "Synthetic session title",
        "windows-1",
        "demo-model",
    )
    assert record.timestamp == "2026-01-01T00:01:00Z"
    assert record.metadata["wsl_distro"] == "Demo-Distro"
    assert "must not be copied" not in json.dumps(record.metadata)


def test_display_form_resolves_to_its_own_profile(host_tree: HostTree) -> None:
    """Two profiles' copies of one file share a display path but not a ref or fingerprint."""
    from agentgrep._engine.find import iter_find_events
    from agentgrep.events import FindRecordEmitted
    from agentgrep.mcp._library import FindRecordLike
    from agentgrep.mcp.refs import find_record_fingerprint, make_find_ref, parse_record_ref
    from agentgrep.query import compile_query, default_registry, parse_query

    session = "AppData/Roaming/Code/User/workspaceStorage/h1/chatSessions/s1.json"
    files = [host_tree.profile / session, host_tree.profile.parent / "second-account" / session]
    for path in files:
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
    registry = default_registry()
    displays = {format_display_path(path) for path in files}
    assert displays == {"%APPDATA%/Code/User/workspaceStorage/h1/chatSessions/s1.json"}
    compiled = compile_query(parse_query(f"path:{displays.pop()}", registry), registry)
    records = [
        t.cast("FindRecordLike", event.record)
        for event in iter_find_events(
            host_tree.home,
            ("vscode",),
            pattern=None,
            limit=None,
            backends=_NO_BACKENDS,
            compiled=compiled,
        )
        if isinstance(event, FindRecordEmitted)
    ]
    resolved = [parse_record_ref(make_find_ref(r), home=host_tree.home).path for r in records]
    assert sorted(resolved) == sorted(files)
    assert len({find_record_fingerprint(r) for r in records}) == 2
