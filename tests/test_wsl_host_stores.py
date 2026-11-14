"""Windows-host and Claude desktop store discovery.

One synthetic tree stands in for a WSL machine: a Linux home plus a Windows
users directory reached through ``AGENTGREP_WSL_USERS_ROOT``. The cases pin
which files discovery reaches, which it must skip, and that no displayed path
names the Windows account.
"""

from __future__ import annotations

import collections.abc as cabc
import json
import pathlib
import re
import typing as t

import pytest

from agentgrep import wsl
from agentgrep._text import format_display_path
from agentgrep.adapters import iter_source_records
from agentgrep.discovery import discover_sources
from agentgrep.records import AgentName, BackendSelection, DiscoveryVersionDetail, SourceHandle

if t.TYPE_CHECKING:
    from agentgrep.mcp._library import FindRecordLike

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
    memory = profile / ".codex/memories_v2/memory_summary.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# Summary\n\nsynthetic memory about widgets\n", encoding="utf-8")
    _write_transcript(users / "Public/.claude/projects/-x/public-1.jsonl", "public-1")
    session = profile / "AppData/Roaming/Claude/claude-code-sessions/acct/org/local_d1.json"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "sessionId": "local_d1",
                "cliSessionId": "windows-1",
                "title": "Synthetic session title",
                "previousTitles": ["Earlier user title"],
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
        test_id="local-transcript-not-duplicated-by-mirror",
        agent="claude",
        file_name="local-1.jsonl",
        store="claude.projects",
        expected_count=1,
    ),
    StoreCase(
        test_id="mirror-of-other-host-kept",
        agent="claude",
        file_name="remote-only.jsonl",
        store="claude.projects",
        expected_count=1,
    ),
    StoreCase(
        test_id="windows-claude-code-session",
        agent="claude",
        file_name="windows-1.jsonl",
        store="claude.projects",
        expected_count=1,
    ),
    StoreCase(
        test_id="cowork-on-windows-host",
        agent="claude",
        file_name="cowork-1.jsonl",
        store="claude.projects",
        expected_count=1,
    ),
    StoreCase(
        test_id="cowork-on-macos",
        agent="claude",
        file_name="cowork-mac.jsonl",
        store="claude.projects",
        expected_count=1,
    ),
    StoreCase(
        test_id="cowork-audit-log-skipped",
        agent="claude",
        file_name="audit.jsonl",
        store="claude.projects",
        expected_count=0,
    ),
    StoreCase(
        test_id="service-profile-skipped",
        agent="claude",
        file_name="public-1.jsonl",
        store="claude.projects",
        expected_count=0,
    ),
    StoreCase(
        test_id="desktop-session-index",
        agent="claude",
        file_name="local_d1.json",
        store="claude.desktop_sessions",
        expected_count=1,
    ),
    StoreCase(
        test_id="windows-codex-rollout",
        agent="codex",
        file_name="rollout-2026-01-01T00-00-00-abc.jsonl",
        store="codex.sessions",
        expected_count=1,
    ),
    StoreCase(
        test_id="windows-codex-memories-v2",
        agent="codex",
        file_name="memory_summary.md",
        store="codex.memories_v2",
        expected_count=1,
    ),
    StoreCase(
        test_id="windows-cursor-transcript",
        agent="cursor-cli",
        file_name="cursor-1.jsonl",
        store="cursor-cli.transcripts",
        expected_count=1,
    ),
)


@pytest.mark.parametrize(
    list(StoreCase._fields),
    STORE_CASES,
    ids=[case.test_id for case in STORE_CASES],
)
def test_windows_host_store_discovery(
    host_tree: HostTree,
    test_id: str,
    agent: AgentName,
    file_name: str,
    store: str,
    expected_count: int,
) -> None:
    """Discovery reaches Windows-side and desktop stores and skips copies and service profiles."""
    sources = _discover(host_tree, agent)
    matches = [s for s in sources if s.path.name == file_name and s.store == store]
    assert len(matches) == expected_count
    assert not any(_WINDOWS_USER in format_display_path(s.path) for s in sources)


def test_desktop_session_becomes_title_record(host_tree: HostTree) -> None:
    """A Code-tab session file yields one title record keyed by its transcript session.

    The record text carries earlier titles too, so a renamed session is found by
    either name.
    """
    source = next(s for s in _discover(host_tree, "claude") if s.store == "claude.desktop_sessions")
    (record,) = iter_source_records(source)
    assert (record.title, record.text, record.session_id, record.model) == (
        "Synthetic session title",
        "Synthetic session title\nEarlier user title",
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


def test_chatgpt_unreadable_stores_are_listed_never_searched(host_tree: HostTree) -> None:
    """ChatGPT's encrypted and empty stores are listed as catalog-only; search never opens them."""
    from agentgrep._engine.find import iter_find_events
    from agentgrep.discovery import CHATGPT_DESKTOP_PACKAGE, discover_listed_sources
    from agentgrep.events import FindRecordEmitted
    from agentgrep.records import AGENT_CHOICES
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
    assert "chatgpt" in AGENT_CHOICES
    listed = discover_listed_sources(host_tree.home, ("chatgpt",), _NO_BACKENDS)
    assert sorted(s.path for s in listed) == sorted(files)
    assert {s.coverage for s in listed} == {StoreCoverage.CATALOG_ONLY}
    assert not any(_WINDOWS_USER in format_display_path(s.path) for s in listed)
    assert discover_sources(host_tree.home, ("chatgpt",), _NO_BACKENDS) == []
    found = [
        event.record
        for event in iter_find_events(
            host_tree.home,
            ("chatgpt",),
            pattern=None,
            limit=None,
            backends=_NO_BACKENDS,
        )
        if isinstance(event, FindRecordEmitted)
    ]
    assert {t.cast("FindRecordLike", record).metadata.get("coverage") for record in found} == {
        "catalog_only",
    }


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


def _listed_transcripts(root: pathlib.Path) -> int:
    """Count ``*.jsonl`` files the way discovery lists them, stop check included."""
    from agentgrep import discovery
    from agentgrep.readers import list_files_matching

    return len(list_files_matching(root, "*.jsonl", None, should_stop=discovery._active_stop()))


def _cowork_homes(root: pathlib.Path) -> int:
    """Count the Cowork session homes under a desktop app directory at ``root``."""
    from agentgrep.discovery import claude_cowork_roots

    return len(claude_cowork_roots((root,)))


def _windows_profiles(root: pathlib.Path) -> int:
    """Count the Windows profiles discovery lists under the users root."""
    from agentgrep import discovery

    return len(discovery._host_profiles())


def _profile_claude_homes(root: pathlib.Path) -> int:
    """Count the Windows profiles whose ``.claude`` home discovery finds."""
    from agentgrep import discovery

    return len(discovery._host_profile_dirs(".claude"))


def _desktop_app_dirs(root: pathlib.Path) -> int:
    """Count the Claude desktop app directories discovery finds."""
    from agentgrep.discovery import claude_desktop_dirs

    return len(claude_desktop_dirs(root / "home"))


class StopWalkCase(t.NamedTuple):
    """A walk over many entries that a stop must end partway through.

    ``stop_after`` is how many stop checks pass before the stop fires. The
    per-profile walks first list the 40 profiles (one check each), so their
    stop fires inside the walk under test, not inside the listing.
    """

    test_id: str
    entry: str
    count: cabc.Callable[[pathlib.Path], int]
    stop_after: int


STOP_WALK_CASES: tuple[StopWalkCase, ...] = (
    StopWalkCase(
        test_id="file-listing",
        entry="d{index}/s.jsonl",
        count=_listed_transcripts,
        stop_after=5,
    ),
    StopWalkCase(
        test_id="cowork-session-scan",
        entry="local-agent-mode-sessions/a/o/local_{index}/.claude/s.jsonl",
        count=_cowork_homes,
        stop_after=5,
    ),
    StopWalkCase(
        test_id="windows-profile-listing",
        entry="Users/u{index}/.claude/s.jsonl",
        count=_windows_profiles,
        stop_after=5,
    ),
    StopWalkCase(
        test_id="per-profile-claude-home",
        entry="Users/u{index}/.claude/s.jsonl",
        count=_profile_claude_homes,
        stop_after=45,
    ),
    StopWalkCase(
        test_id="per-desktop-candidate",
        entry="Users/u{index}/AppData/Roaming/Claude/config.json",
        count=_desktop_app_dirs,
        stop_after=45,
    ),
)


@pytest.mark.parametrize(
    list(StopWalkCase._fields),
    STOP_WALK_CASES,
    ids=[case.test_id for case in STOP_WALK_CASES],
)
def test_stop_ends_a_walk_partway(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    entry: str,
    count: cabc.Callable[[pathlib.Path], int],
    stop_after: int,
) -> None:
    """Under :func:`discovery_stop`, a walk ends once the predicate fires, and records it."""
    from agentgrep.discovery import discovery_stop

    monkeypatch.setenv(wsl.USERS_ROOT_ENV, str(tmp_path / "Users"))
    monkeypatch.setattr(wsl, "is_wsl", lambda proc_version=None: True)
    for index in range(40):
        path = tmp_path / entry.format(index=index)
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
    checks: list[int] = []

    def stop() -> bool:
        checks.append(1)
        return len(checks) > stop_after

    with discovery_stop(stop) as state:
        partial = count(tmp_path)
    assert state.stopped
    assert partial < 40
    assert count(tmp_path) == 40


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

    def tracked(
        home: pathlib.Path,
        query: SearchQuery,
        backends: BackendSelection,
        *,
        version_detail: DiscoveryVersionDetail = "none",
    ) -> list[SourceHandle]:
        try:
            return real_discover(home, query, backends, version_detail=version_detail)
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
    # Only the stats already in flight when the search was cancelled ran.
    assert len(calls) <= discovery._SLOW_MOUNT_STAT_WORKERS


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


class ProfileFilterCase(t.NamedTuple):
    """A ``path:`` filter on the Windows profile and the transcripts it keeps."""

    test_id: str
    query: str
    expected: frozenset[str]


_WINDOWS_SIDE = frozenset({"remote-only.jsonl", "windows-1.jsonl", "cowork-1.jsonl"})
PROFILE_FILTER_CASES = (
    ProfileFilterCase(
        test_id="glob-after-variable",
        query="path:%USERPROFILE%*",
        expected=_WINDOWS_SIDE,
    ),
    ProfileFilterCase(
        test_id="bare-variable",
        query="path:%USERPROFILE%",
        expected=_WINDOWS_SIDE,
    ),
    ProfileFilterCase(
        test_id="negated",
        query="NOT path:%USERPROFILE%",
        expected=frozenset({"local-1.jsonl", "cowork-mac.jsonl"}),
    ),
)


@pytest.mark.parametrize(
    list(ProfileFilterCase._fields),
    PROFILE_FILTER_CASES,
    ids=[case.test_id for case in PROFILE_FILTER_CASES],
)
def test_profile_filter_selects_the_windows_side(
    host_tree: HostTree,
    test_id: str,
    query: str,
    expected: frozenset[str],
) -> None:
    """``path:%USERPROFILE%`` covers %APPDATA% paths too, and NOT drops them all."""
    from agentgrep._engine.find import iter_find_events
    from agentgrep.events import FindRecordEmitted
    from agentgrep.query import compile_query, default_registry, parse_query

    registry = default_registry()
    compiled = compile_query(parse_query(query, registry), registry)
    names = {
        t.cast("FindRecordLike", event.record).path.name
        for event in iter_find_events(
            host_tree.home,
            ("claude",),
            pattern=None,
            limit=None,
            backends=_NO_BACKENDS,
            compiled=compiled,
        )
        if isinstance(event, FindRecordEmitted)
    }
    assert names & (_WINDOWS_SIDE | {"local-1.jsonl", "cowork-mac.jsonl"}) == expected


_FRONTENDS = ("agentgrep.cli", "agentgrep.ui", "agentgrep.mcp", "agentgrep.query")


class BoundaryCase(t.NamedTuple):
    """A core module and the import prefixes it must never reach (ADR 0010)."""

    test_id: str
    module: str
    forbidden: tuple[str, ...]


BOUNDARY_CASES: tuple[BoundaryCase, ...] = (
    BoundaryCase(test_id="wsl-is-a-leaf", module="wsl", forbidden=("agentgrep",)),
    BoundaryCase(
        test_id="chromium-storage-is-a-leaf",
        module="_chromium_storage",
        forbidden=("agentgrep",),
    ),
    BoundaryCase(test_id="text-below-frontends", module="_text", forbidden=_FRONTENDS),
    BoundaryCase(test_id="readers-below-frontends", module="readers", forbidden=_FRONTENDS),
    BoundaryCase(test_id="discovery-below-frontends", module="discovery", forbidden=_FRONTENDS),
    BoundaryCase(
        test_id="chatgpt-adapter-below-frontends",
        module="adapters/chatgpt",
        forbidden=_FRONTENDS,
    ),
    BoundaryCase(
        test_id="chatgpt-catalog-below-frontends",
        module="store_catalog/chatgpt",
        forbidden=_FRONTENDS,
    ),
)


@pytest.mark.parametrize(
    list(BoundaryCase._fields),
    BOUNDARY_CASES,
    ids=[case.test_id for case in BOUNDARY_CASES],
)
def test_core_module_stays_headless(
    test_id: str,
    module: str,
    forbidden: tuple[str, ...],
) -> None:
    """Windows-host support lives in the headless core, below every frontend."""
    import ast

    source = (pathlib.Path(wsl.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [name for name in imported if name.startswith(forbidden)]


async def test_cancelled_discovery_thread_stops_the_windows_walk(
    host_tree: HostTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the task awaiting ``discover_in_thread`` stops the walk it started.

    The MCP discovery tools run through this helper, so a cancelled ``find``
    leaves at most the in-flight Windows-side stats running, not the whole walk.
    """
    import asyncio
    import threading

    from agentgrep import discovery

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

    def walk() -> list[SourceHandle]:
        try:
            return _discover(host_tree, "claude")
        finally:
            finished.set()

    monkeypatch.setattr(discovery, "file_mtime_ns", gated)
    task = asyncio.create_task(discovery.discover_in_thread(walk))
    assert await asyncio.to_thread(started.wait, 5), "the Windows walk never started"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 5), "the cancelled walk never returned"
    assert len(calls) <= discovery._SLOW_MOUNT_STAT_WORKERS


class FindStatCase(t.NamedTuple):
    """A find query and whether discovery must stat Windows-side files for it."""

    test_id: str
    query: str | None
    stats_windows: bool


FIND_STAT_CASES: tuple[FindStatCase, ...] = (
    FindStatCase(test_id="no-source-predicate", query=None, stats_windows=False),
    FindStatCase(test_id="mtime-predicate", query="mtime:>2000-01-01", stats_windows=True),
)


@pytest.mark.parametrize(
    list(FindStatCase._fields),
    FIND_STAT_CASES,
    ids=[case.test_id for case in FIND_STAT_CASES],
)
def test_find_skips_mtime_stats_it_never_uses(
    host_tree: HostTree,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    query: str | None,
    stats_windows: bool,
) -> None:
    """Find skips the per-file stat on the Windows mount unless a predicate needs mtime."""
    from agentgrep import discovery
    from agentgrep._engine.find import iter_find_events
    from agentgrep.events import FindRecordEmitted
    from agentgrep.query import compile_query, default_registry, parse_query

    calls: list[pathlib.Path] = []
    real_mtime = discovery.file_mtime_ns

    def counted(path: pathlib.Path) -> int:
        if wsl.is_host_path(path):
            calls.append(path)
        return real_mtime(path)

    monkeypatch.setattr(discovery, "file_mtime_ns", counted)
    registry = default_registry()
    compiled = None if query is None else compile_query(parse_query(query, registry), registry)
    found = [
        event
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
    assert found
    assert bool(calls) is stats_windows


class EncodedAccountCase(t.NamedTuple):
    """A Windows profile name agents re-encode, and one folder name that carries it."""

    test_id: str
    profile: str
    folder: str


ENCODED_ACCOUNT_CASES: tuple[EncodedAccountCase, ...] = (
    EncodedAccountCase(
        test_id="space-dashed", profile="John Smith", folder="C--Users-John-Smith-w"
    ),
    EncodedAccountCase(test_id="dot-dashed", profile="jo.hn", folder="C--Users-jo-hn-w"),
    EncodedAccountCase(
        test_id="space-quoted-windows-grok",
        profile="John Smith",
        folder="C%3A%5CUsers%5CJohn%20Smith%5Cw",
    ),
)


@pytest.mark.parametrize(
    list(EncodedAccountCase._fields),
    ENCODED_ACCOUNT_CASES,
    ids=[case.test_id for case in ENCODED_ACCOUNT_CASES],
)
def test_reencoded_account_name_is_hidden_and_resolves(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    profile: str,
    folder: str,
) -> None:
    """An account name with spaces or dots never prints, however an agent encoded it."""
    from agentgrep._text import redact_encoded_paths, restore_encoded_paths

    users = tmp_path / "Users"
    transcript = users / profile / ".claude" / "projects" / folder / "s.jsonl"
    _write_transcript(transcript, "s")
    monkeypatch.setenv(wsl.USERS_ROOT_ENV, str(users))
    monkeypatch.setattr(wsl, "is_wsl", lambda proc_version=None: True)
    assert wsl.host_user_profiles()
    display = format_display_path(transcript)
    assert "%USERNAME%" in display
    assert profile not in display
    assert wsl.encoded_account_forms(profile)[0] not in display
    if "%5C" not in folder:
        assert wsl.host_display_candidates(display) == (transcript,)
    linux_side = f"/home/u/.grok/sessions/{folder}/h.jsonl"
    redacted = redact_encoded_paths(linux_side, "/home/u")
    assert "%USERNAME%" in redacted
    assert linux_side in restore_encoded_paths(redacted, "/home/u")
