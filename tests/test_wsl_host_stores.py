"""Windows-host and Claude desktop store discovery.

One synthetic tree stands in for a WSL machine: a Linux home plus a Windows
users directory reached through ``AGENTGREP_WSL_USERS_ROOT``. The cases pin
which files discovery reaches, which it must skip, and that no displayed path
names the Windows account.
"""

from __future__ import annotations

import json
import pathlib
import re
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
    _write_transcript(
        profile / f".claude/projects/C--Users-{_WINDOWS_USER}-work/windows-1.jsonl",
        "windows-1",
    )
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
    StoreCase(
        "local-transcript-not-duplicated-by-mirror", "claude", "local-1.jsonl", "claude.projects", 1
    ),
    StoreCase("mirror-of-other-host-kept", "claude", "remote-only.jsonl", "claude.projects", 1),
    StoreCase("windows-claude-code-session", "claude", "windows-1.jsonl", "claude.projects", 1),
    StoreCase("cowork-on-windows-host", "claude", "cowork-1.jsonl", "claude.projects", 1),
    StoreCase("cowork-on-macos", "claude", "cowork-mac.jsonl", "claude.projects", 1),
    StoreCase("cowork-audit-log-skipped", "claude", "audit.jsonl", "claude.projects", 0),
    StoreCase("service-profile-skipped", "claude", "public-1.jsonl", "claude.projects", 0),
    StoreCase("desktop-session-index", "claude", "local_d1.json", "claude.desktop_sessions", 1),
    StoreCase(
        "windows-codex-rollout",
        "codex",
        "rollout-2026-01-01T00-00-00-abc.jsonl",
        "codex.sessions",
        1,
    ),
    StoreCase(
        "windows-cursor-transcript", "cursor-cli", "cursor-1.jsonl", "cursor-cli.transcripts", 1
    ),
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
    assert record.origin is not None
    assert record.origin.cwd == "/work/demo"
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


def test_chatgpt_desktop_is_listed_but_never_searched(host_tree: HostTree) -> None:
    """Inventory lists ChatGPT desktop files as catalog-only; search discovery never does."""
    from agentgrep.discovery import CHATGPT_DESKTOP_PACKAGE
    from agentgrep.records import AGENT_CHOICES, INVENTORY_AGENTS
    from agentgrep.stores import StoreCoverage

    files = [
        host_tree.home / "Library/Application Support/com.openai.chat/conversations-v3-a/C1.data",
        host_tree.profile
        / f"AppData/Local/Packages/{CHATGPT_DESKTOP_PACKAGE}/LocalCache/Roaming/ChatGPT"
        / "IndexedDB/https_chatgpt.com_0.indexeddb.leveldb/000001.log",
    ]
    for path in files:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x00opaque")
    assert "chatgpt" in INVENTORY_AGENTS
    assert "chatgpt" not in AGENT_CHOICES
    listed = discover_sources(host_tree.home, ("chatgpt",), _NO_BACKENDS, include_non_default=True)
    assert sorted(s.path for s in listed) == sorted(files)
    assert {s.coverage for s in listed} == {StoreCoverage.CATALOG_ONLY}
    assert not any(_WINDOWS_USER in format_display_path(s.path) for s in listed)
    assert discover_sources(host_tree.home, ("chatgpt",), _NO_BACKENDS) == []


def test_encoded_folder_hides_the_account_and_resolves(host_tree: HostTree) -> None:
    """An encoded project folder shows %USERNAME% and maps back to the real file."""
    transcript = (
        host_tree.profile / f".claude/projects/C--Users-{_WINDOWS_USER}-work/windows-1.jsonl"
    )
    display = format_display_path(transcript)
    assert display == "%USERPROFILE%/.claude/projects/C--Users-%USERNAME%-work/windows-1.jsonl"
    assert wsl.host_display_candidates(display) == (transcript,)


def test_encoded_home_hides_the_linux_account_and_resolves(
    host_tree: HostTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project folder named after the home shows ``-~-`` and still resolves."""
    from agentgrep._engine.find import iter_find_events
    from agentgrep.events import FindRecordEmitted
    from agentgrep.mcp._library import FindRecordLike
    from agentgrep.mcp.refs import make_find_ref, parse_record_ref
    from agentgrep.query import compile_query, default_registry, parse_query

    monkeypatch.setenv("HOME", str(host_tree.home))
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(host_tree.home))
    transcript = host_tree.home / f".claude/projects/{encoded}-work/linux-1.jsonl"
    _write_transcript(transcript, "linux-1")
    windows_copy = (
        host_tree.profile / f".claude/projects/--wsl-localhost-demo{encoded}-work/s.jsonl"
    )
    display = format_display_path(transcript)
    assert display == "~/.claude/projects/-~-work/linux-1.jsonl"
    assert format_display_path(windows_copy) == (
        "%USERPROFILE%/.claude/projects/--wsl-localhost-demo-~-work/s.jsonl"
    )
    registry = default_registry()
    compiled = compile_query(parse_query(f"path:{display}", registry), registry)
    (record,) = [
        t.cast("FindRecordLike", event.record)
        for event in iter_find_events(
            host_tree.home,
            ("claude",),
            pattern=None,
            limit=None,
            backends=_NO_BACKENDS,
            compiled=compiled,
        )
        if isinstance(event, FindRecordEmitted)
    ]
    assert parse_record_ref(make_find_ref(record), home=host_tree.home).path == transcript


def test_discovery_stop_ends_a_windows_walk_early(
    host_tree: HostTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the stop predicate fires, discovery stops queuing Windows-side stats."""
    from agentgrep import discovery

    for index in range(40):
        _write_transcript(
            host_tree.profile / f".claude/projects/C--Users-x-bulk/bulk-{index}.jsonl",
            f"bulk-{index}",
        )
    calls: list[pathlib.Path] = []
    real_mtime = discovery.file_mtime_ns

    def counted(path: pathlib.Path) -> int:
        calls.append(path)
        return real_mtime(path)

    monkeypatch.setattr(discovery, "file_mtime_ns", counted)

    def stop() -> bool:
        return sum(1 for path in calls if path.name.startswith("bulk-")) >= 4

    with discovery.discovery_stop(stop):
        sources = discover_sources(
            host_tree.home, ("claude",), _NO_BACKENDS, include_non_default=True
        )
    bulk = [s for s in sources if s.path.name.startswith("bulk-")]
    assert bulk == []
    assert len([path for path in calls if path.name.startswith("bulk-")]) < 40


def test_file_listing_stops_mid_walk(tmp_path: pathlib.Path) -> None:
    """A stop predicate ends a listing walk partway through the tree."""
    from agentgrep.readers import list_files_matching

    for index in range(50):
        (tmp_path / f"d{index}").mkdir()
        (tmp_path / f"d{index}" / "s.jsonl").write_text("{}", encoding="utf-8")
    checks: list[int] = []

    def stop() -> bool:
        checks.append(1)
        return len(checks) > 5

    assert len(list_files_matching(tmp_path, "*.jsonl", None, should_stop=stop)) < 50
    assert len(list_files_matching(tmp_path, "*.jsonl", None)) == 50


def test_abandoned_search_stops_the_windows_walk(
    host_tree: HostTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search cancelled during discovery is abandoned and its Windows walk stops early."""
    import threading

    from agentgrep import discovery
    from agentgrep._engine import search as engine_search
    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery

    for index in range(64):
        _write_transcript(
            host_tree.profile / f".claude/projects/C--Users-x-bulk/bulk-{index}.jsonl",
            f"bulk-{index}",
        )
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls: list[pathlib.Path] = []
    real_mtime = discovery.file_mtime_ns

    def gated(path: pathlib.Path) -> int:
        if path.name.startswith("bulk-"):
            calls.append(path)
            started.set()
            release.wait(5)
        return real_mtime(path)

    real_discover = engine_search.discover_sources_for_search

    def tracked(*args: t.Any, **kwargs: t.Any) -> list[SourceHandle]:
        try:
            return real_discover(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(discovery, "file_mtime_ns", gated)
    monkeypatch.setattr(engine_search, "discover_sources_for_search", tracked)
    control = SearchControl()

    def cancel_once_walking() -> None:
        assert started.wait(5), "the Windows walk never started"
        control.request_answer_now(reason="caller_cancelled")
        release.set()

    canceller = threading.Thread(target=cancel_once_walking, daemon=True)
    canceller.start()
    query = SearchQuery(
        terms=("widget",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
        effort="exhaustive",
    )
    with pytest.raises(engine_search._DiscoveryAbandonedError):
        engine_search._discover_sources_abandoning_if_cancelled(
            host_tree.home,
            query,
            _NO_BACKENDS,
            control=control,
        )
    assert finished.wait(5), "the abandoned discovery never returned"
    assert len(calls) < 64


def test_stopped_listing_is_not_memoized(tmp_path: pathlib.Path) -> None:
    """A walk cut short by a stop is never kept as the pass's listing of that tree."""
    from agentgrep.discovery import discovery_stop, handles_from_discovery
    from agentgrep.records import StoreCoverage
    from agentgrep.stores import DiscoverySpec

    for index in range(50):
        _write_transcript(tmp_path / "projects" / f"p{index}" / "s.jsonl", f"s-{index}")
    spec = DiscoverySpec(
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        path_kind="session_file",
        source_kind="jsonl",
        home_subpath=("projects",),
        glob="*.jsonl",
    )
    checks: list[int] = []

    def stop() -> bool:
        checks.append(1)
        return len(checks) > 5

    listings: dict[tuple[pathlib.Path, str], list[pathlib.Path]] = {}
    with discovery_stop(stop):
        handles_from_discovery(
            spec,
            "claude",
            tmp_path,
            _NO_BACKENDS,
            StoreCoverage.DEFAULT_SEARCH,
            listings=listings,
        )
    assert listings == {}
