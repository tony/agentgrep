"""Tests for engine-only profiling helpers."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import typing as t

import pytest

import agentgrep
import agentgrep._engine.orchestration as _rm_orch
import agentgrep._engine.profiling as _rm_profiling
import agentgrep._engine.scanning as _rm_scanning
from agentgrep._engine import orchestration
from agentgrep._engine.profiling import (
    EnginePhaseSample,
    EngineProfile,
    EngineProfiler,
    profile_find_query,
    profile_search_query,
    use_engine_profiler,
)


def _write_codex_session(
    home: pathlib.Path,
    *,
    name: str,
    text: str,
) -> pathlib.Path:
    """Write a synthetic Codex session-jsonl file the engine can parse."""
    path = home / ".codex" / "sessions" / "2026" / "05" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "response_item", "payload": {"role": "user", "content": text}}
    path.write_text(json.dumps(payload) + "\n")
    return path


def _write_claude_project_session(
    home: pathlib.Path,
    *,
    name: str,
    text: str,
) -> pathlib.Path:
    """Write a synthetic Claude project session the engine can parse."""
    path = home / ".claude" / "projects" / "-synthetic-project" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "user",
        "sessionId": "session-1",
        "message": {"role": "user", "content": text},
    }
    path.write_text(json.dumps(payload) + "\n")
    return path


def _make_query(
    *,
    limit: int | None = 10,
    scope: agentgrep.SearchScope = "prompts",
    match_surface: agentgrep.SearchMatchSurface = "haystack",
    agents: tuple[agentgrep.AgentName, ...] = ("codex",),
) -> agentgrep.SearchQuery:
    """Build a narrow search query for profiling fixtures."""
    return agentgrep.SearchQuery(
        terms=("tmux",),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=limit,
        dedupe=True,
        match_surface=match_surface,
    )


class ProfilePhaseCase(t.NamedTuple):
    """Expected phase names for one engine profiling helper."""

    test_id: str
    helper: str
    expected_phases: tuple[str, ...]


PROFILE_PHASE_CASES: tuple[ProfilePhaseCase, ...] = (
    ProfilePhaseCase(
        test_id="search-query",
        helper="search",
        expected_phases=("search.discover", "search.plan", "search.collect"),
    ),
    ProfilePhaseCase(
        test_id="find-query",
        helper="find",
        expected_phases=("find.discover", "find.filter"),
    ),
)


class ProfileSampleCase(t.NamedTuple):
    """Expected sample attributes for one source-level profile span."""

    test_id: str
    sample_name: str
    expected_attributes: dict[str, object]


SEARCH_SOURCE_SAMPLE_CASES: tuple[ProfileSampleCase, ...] = (
    ProfileSampleCase(
        test_id="discovery-group",
        sample_name="search.discover.group",
        expected_attributes={
            "agentgrep_agent": "codex",
            "agentgrep_adapter_id": "codex.sessions_jsonl.v1",
            "agentgrep_source_count": 1,
        },
    ),
    ProfileSampleCase(
        test_id="collect-source",
        sample_name="search.collect.source",
        expected_attributes={
            "agentgrep_agent": "codex",
            "agentgrep_adapter_id": "codex.sessions_jsonl.v1",
            "agentgrep_records_seen": 1,
            "agentgrep_matches_seen": 1,
        },
    ),
)


class SearchPlanSampleCase(t.NamedTuple):
    """Expected search-planning profile sample."""

    test_id: str
    sample_kind: t.Literal["prefilter-root", "direct-source"]
    sample_name: str
    expected_attributes: dict[str, object]


SEARCH_PLAN_SAMPLE_CASES: tuple[SearchPlanSampleCase, ...] = (
    SearchPlanSampleCase(
        test_id="prefilter-root",
        sample_kind="prefilter-root",
        sample_name="search.plan.prefilter_root",
        expected_attributes={
            "agentgrep_source_count": 1,
            "agentgrep_matched_source_count": 1,
            "agentgrep_unknown": False,
        },
    ),
    SearchPlanSampleCase(
        test_id="direct-source",
        sample_kind="direct-source",
        sample_name="search.plan.direct_source",
        expected_attributes={
            "agentgrep_agent": "codex",
            "agentgrep_adapter_id": "codex.sessions_jsonl.v1",
            "agentgrep_matched": True,
        },
    ),
)


class ProfilePhysicalPlanCase(t.NamedTuple):
    """Expected physical strategy observed by one profiled search query."""

    test_id: str
    scope: agentgrep.SearchScope
    match_surface: agentgrep.SearchMatchSurface
    expected_strategy: str


PROFILE_PHYSICAL_PLAN_CASES: tuple[ProfilePhysicalPlanCase, ...] = (
    ProfilePhysicalPlanCase(
        test_id="grep-conversations-jsonl-bounded-raw-prefilter",
        scope="conversations",
        match_surface="text",
        expected_strategy="jsonl_bounded_reverse_raw_text_prefilter",
    ),
)


class ProfileStrategyGroupCase(t.NamedTuple):
    """Expected physical strategy group observed by one profiled search query."""

    test_id: str
    match_surface: agentgrep.SearchMatchSurface
    expected_strategy: str


PROFILE_STRATEGY_GROUP_CASES: tuple[ProfileStrategyGroupCase, ...] = (
    ProfileStrategyGroupCase(
        test_id="grep-jsonl-bounded-raw-prefilter",
        match_surface="text",
        expected_strategy="jsonl_bounded_reverse_raw_text_prefilter",
    ),
    ProfileStrategyGroupCase(
        test_id="search-jsonl-bounded-haystack-prefilter",
        match_surface="haystack",
        expected_strategy="jsonl_bounded_reverse_haystack_raw_text_prefilter",
    ),
)


def _samples_named(profile: EngineProfile, name: str) -> tuple[EnginePhaseSample, ...]:
    """Return profile samples matching ``name``."""
    return tuple(sample for sample in profile.samples if sample.name == name)


@pytest.mark.parametrize(
    "case",
    PROFILE_PHASE_CASES,
    ids=[c.test_id for c in PROFILE_PHASE_CASES],
)
def test_profile_helpers_report_engine_phase_counts(
    case: ProfilePhaseCase,
    tmp_path: pathlib.Path,
) -> None:
    """Engine profiling reports stable phase names and source/result counts."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")

    if case.helper == "search":
        profiled = profile_search_query(tmp_path, _make_query())
        assert profiled.result_count == 1
        assert profiled.discovered_source_count == 1
        assert profiled.planned_source_count == 1
    else:
        profiled = profile_find_query(
            tmp_path,
            ("codex",),
            pattern="match",
            limit=10,
        )
        assert profiled.result_count == 1
        assert profiled.discovered_source_count == 1

    sample_names = tuple(sample.name for sample in profiled.profile.samples)
    for expected in case.expected_phases:
        assert expected in sample_names
    assert all(sample.duration_seconds >= 0 for sample in profiled.profile.samples)


@pytest.mark.parametrize(
    "case",
    SEARCH_SOURCE_SAMPLE_CASES,
    ids=[c.test_id for c in SEARCH_SOURCE_SAMPLE_CASES],
)
def test_profile_search_query_reports_source_level_samples(
    case: ProfileSampleCase,
    tmp_path: pathlib.Path,
) -> None:
    """Search profiling identifies hot source groups without paths or prompt text."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux private-token")

    profiled = profile_search_query(tmp_path, _make_query())

    samples = _samples_named(profiled.profile, case.sample_name)
    assert len(samples) == 1
    sample = samples[0]
    for key, expected in case.expected_attributes.items():
        assert sample.attributes[key] == expected

    payload = json.dumps(profiled.to_payload(), sort_keys=True)
    assert str(tmp_path) not in payload
    assert "private-token" not in payload


@pytest.mark.parametrize(
    "case",
    PROFILE_PHYSICAL_PLAN_CASES,
    ids=[c.test_id for c in PROFILE_PHYSICAL_PLAN_CASES],
)
def test_profile_search_query_preserves_physical_source_strategy(
    case: ProfilePhysicalPlanCase,
    tmp_path: pathlib.Path,
) -> None:
    """Search profiling measures physical-plan execution strategies."""
    _ = _write_claude_project_session(tmp_path, name="match.jsonl", text="tmux prompt")
    query = _make_query(
        scope=case.scope,
        match_surface=case.match_surface,
        agents=("claude",),
    )

    profiled = profile_search_query(tmp_path, query)

    samples = _samples_named(profiled.profile, "search.collect.source")
    assert len(samples) == 1
    assert samples[0].attributes["agentgrep_source_strategy"] == case.expected_strategy


@pytest.mark.parametrize(
    "case",
    PROFILE_STRATEGY_GROUP_CASES,
    ids=[c.test_id for c in PROFILE_STRATEGY_GROUP_CASES],
)
def test_profile_search_query_reports_physical_strategy_groups(
    case: ProfileStrategyGroupCase,
    tmp_path: pathlib.Path,
) -> None:
    """Search profiling summarizes physical strategies without source paths."""
    _ = _write_claude_project_session(tmp_path, name="match.jsonl", text="tmux private-token")
    query = _make_query(
        scope="conversations",
        match_surface=case.match_surface,
        agents=("claude",),
    )

    profiled = profile_search_query(
        tmp_path,
        query,
        backends=agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    samples = _samples_named(profiled.profile, "search.plan.strategy_group")
    assert len(samples) == 1
    sample = samples[0]
    assert sample.attributes["agentgrep_agent"] == "claude"
    assert sample.attributes["agentgrep_adapter_id"] == "claude.projects_jsonl.v1"
    assert sample.attributes["agentgrep_source_kind"] == "jsonl"
    assert sample.attributes["agentgrep_source_strategy"] == case.expected_strategy
    assert sample.attributes["agentgrep_source_count"] == 1

    payload = json.dumps(profiled.to_payload(), sort_keys=True)
    assert str(tmp_path) not in payload
    assert "private-token" not in payload


def test_profile_search_query_reports_planner_decisions(
    tmp_path: pathlib.Path,
) -> None:
    """Search profiling records privacy-safe planner decision summaries."""
    _ = _write_claude_project_session(tmp_path, name="match.jsonl", text="tmux private-token")

    profiled = profile_search_query(
        tmp_path,
        _make_query(scope="conversations", match_surface="text", agents=("claude",)),
        backends=agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    samples = _samples_named(profiled.profile, "search.plan.decision")
    decisions = {sample.attributes["agentgrep_planner_decision"]: sample for sample in samples}
    assert decisions["scope_prune"].attributes["agentgrep_source_count"] == 1
    assert decisions["root_prefilter_skipped"].attributes["agentgrep_source_count"] == 1
    assert (
        decisions["root_prefilter_skipped"].attributes["agentgrep_planner_detail"]
        == "bounded_append_only_jsonl"
    )

    payload = json.dumps(profiled.to_payload(), sort_keys=True)
    assert str(tmp_path) not in payload
    assert "private-token" not in payload


def test_profile_find_query_reports_filter_source_samples(
    tmp_path: pathlib.Path,
) -> None:
    """Find profiling reports per-source filter outcomes without source paths."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")

    profiled = profile_find_query(
        tmp_path,
        ("codex",),
        pattern="match",
        limit=10,
    )

    samples = _samples_named(profiled.profile, "find.filter.source")
    assert len(samples) == 1
    sample = samples[0]
    assert sample.attributes["agentgrep_agent"] == "codex"
    assert sample.attributes["agentgrep_adapter_id"] == "codex.sessions_jsonl.v1"
    assert sample.attributes["agentgrep_matched"] is True

    payload = json.dumps(profiled.to_payload(), sort_keys=True)
    assert str(tmp_path) not in payload


class ProfileFindTypeDiscoveryCase(t.NamedTuple):
    """Expected discovery role narrowing for one profiled find type filter."""

    test_id: str
    type_filter: str
    expected_store_roles: frozenset[agentgrep.StoreRole] | None


PROFILE_FIND_TYPE_DISCOVERY_CASES: tuple[ProfileFindTypeDiscoveryCase, ...] = (
    ProfileFindTypeDiscoveryCase(
        test_id="all-keeps-default-discovery",
        type_filter="all",
        expected_store_roles=None,
    ),
    ProfileFindTypeDiscoveryCase(
        test_id="prompts-discovers-prompt-history",
        type_filter="prompts",
        expected_store_roles=agentgrep.PROMPT_HISTORY_STORE_ROLES,
    ),
    ProfileFindTypeDiscoveryCase(
        test_id="history-discovers-prompt-history",
        type_filter="history",
        expected_store_roles=agentgrep.PROMPT_HISTORY_STORE_ROLES,
    ),
    ProfileFindTypeDiscoveryCase(
        test_id="sessions-discovers-conversations",
        type_filter="sessions",
        expected_store_roles=agentgrep.CONVERSATION_STORE_ROLES,
    ),
)


@pytest.mark.parametrize(
    "case",
    PROFILE_FIND_TYPE_DISCOVERY_CASES,
    ids=[c.test_id for c in PROFILE_FIND_TYPE_DISCOVERY_CASES],
)
def test_profile_find_query_pushes_type_filter_into_discovery(
    case: ProfileFindTypeDiscoveryCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Find profiling measures the same narrowed discovery path as runtime find."""
    observed_store_roles: list[frozenset[agentgrep.StoreRole] | None] = []

    def discover_sources(
        *_args: object,
        **kwargs: object,
    ) -> list[agentgrep.SourceHandle]:
        observed_store_roles.append(
            t.cast("frozenset[agentgrep.StoreRole] | None", kwargs.get("store_roles")),
        )
        return []

    monkeypatch.setattr(_rm_profiling, "discover_sources", discover_sources)

    _ = profile_find_query(
        tmp_path,
        ("codex",),
        pattern=None,
        limit=None,
        type_filter=t.cast("agentgrep.FindSourceTypeFilter", case.type_filter),
    )

    assert observed_store_roles == [case.expected_store_roles]


@pytest.mark.parametrize(
    "case",
    SEARCH_PLAN_SAMPLE_CASES,
    ids=[c.test_id for c in SEARCH_PLAN_SAMPLE_CASES],
)
def test_search_planning_reports_source_level_samples(
    case: SearchPlanSampleCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search planning reports fast-path decisions without source paths."""
    path = tmp_path / "session.jsonl"
    path.write_text("tmux prompt", encoding="utf-8")
    query = _make_query()
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=tmp_path if case.sample_kind == "prefilter-root" else None,
        mtime_ns=1,
    )
    profiler = EngineProfiler()

    if case.sample_kind == "prefilter-root":

        def grep_root_paths(
            _search_root: pathlib.Path,
            _query: agentgrep.SearchQuery,
            _grep_program: str,
            *,
            control: agentgrep.SearchControl | None = None,
        ) -> set[pathlib.Path]:
            assert control is not None
            return {path}

        monkeypatch.setattr(orchestration, "grep_root_paths", grep_root_paths)
        with use_engine_profiler(profiler):
            filtered = agentgrep.prefilter_sources_by_root(query, [source], "rg")
        assert filtered == [source]
    else:
        with use_engine_profiler(profiler):
            matched = agentgrep.direct_source_matches(
                source,
                query,
                agentgrep.BackendSelection(None, None, None),
            )
        assert matched is True

    samples = _samples_named(profiler.snapshot(), case.sample_name)
    assert len(samples) == 1
    sample = samples[0]
    for key, expected in case.expected_attributes.items():
        assert sample.attributes[key] == expected

    payload = json.dumps(profiler.snapshot().to_payload(), sort_keys=True)
    assert str(tmp_path) not in payload


def test_prefilter_source_count_excludes_sqlite_candidates(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite candidates sharing a search root stay out of the prefilter count.

    They bypass root prefiltering entirely, so counting them would
    over-report how many sources the grep pass covered.
    """
    file_path = tmp_path / "session.jsonl"
    file_path.write_text("tmux prompt", encoding="utf-8")
    sqlite_path = tmp_path / "state.vscdb"
    sqlite_path.write_bytes(b"")
    file_source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=file_path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=tmp_path,
        mtime_ns=1,
    )
    sqlite_source = agentgrep.SourceHandle(
        agent="cursor-ide",
        store="cursor_ide.state",
        adapter_id="cursor_ide.state_vscdb.v1",
        path=sqlite_path,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=tmp_path,
        mtime_ns=1,
    )

    def fake_grep_root_paths(
        _search_root: pathlib.Path,
        _query: agentgrep.SearchQuery,
        _grep_program: str,
        *,
        control: agentgrep.SearchControl | None = None,
    ) -> set[pathlib.Path]:
        assert control is not None
        return {file_path}

    monkeypatch.setattr(orchestration, "grep_root_paths", fake_grep_root_paths)
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        filtered = agentgrep.prefilter_sources_by_root(
            _make_query(),
            [sqlite_source, file_source],
            "rg",
        )

    assert sqlite_source in filtered
    assert file_source in filtered
    samples = _samples_named(profiler.snapshot(), "search.plan.prefilter_root")
    assert len(samples) == 1
    assert samples[0].attributes["agentgrep_source_count"] == 1


def test_direct_source_matches_skips_sample_when_aborted(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer-now abort records no direct-source sample.

    Recording the abort as ``agentgrep_matched=False`` would conflate
    "stopped early" with "did not match" and skew profiling statistics.
    """

    class _AbortAfterFirstCheckControl(agentgrep.SearchControl):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def answer_now_requested(self) -> bool:
            self.calls += 1
            return self.calls > 1

    path = tmp_path / "session.jsonl"
    path.write_text("tmux prompt", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    def fake_grep_file_matches(
        _path: pathlib.Path,
        _query: agentgrep.SearchQuery,
        _program: str,
        *,
        control: agentgrep.SearchControl | None = None,
    ) -> bool | None:
        assert control is not None
        return None

    monkeypatch.setattr(_rm_orch, "grep_file_matches", fake_grep_file_matches)
    profiler = EngineProfiler()
    control = _AbortAfterFirstCheckControl()

    with use_engine_profiler(profiler):
        matched = agentgrep.direct_source_matches(
            source,
            _make_query(),
            agentgrep.BackendSelection(None, "rg", None),
            control,
        )

    assert matched is False
    assert control.calls > 1
    assert _samples_named(profiler.snapshot(), "search.plan.direct_source") == ()


def test_run_readonly_command_records_redacted_subprocess_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess profiling records command family and byte counts, never argv text."""

    def fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["/private/home/bin/rg", "--files", "/private/home/project"]
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(command, 0, "alpha\n", "")

    monkeypatch.setattr(agentgrep.subprocess, "run", fake_run)

    profiler = EngineProfiler()
    with use_engine_profiler(profiler):
        completed = agentgrep.run_readonly_command(
            ["/private/home/bin/rg", "--files", "/private/home/project"],
        )

    assert completed.returncode == 0
    snapshot = profiler.snapshot()
    assert len(snapshot.samples) == 1
    sample = snapshot.samples[0]
    assert sample.name == "subprocess.run"
    assert sample.attributes["agentgrep_tool"] == "rg"
    assert sample.attributes["agentgrep_returncode"] == 0
    assert sample.attributes["agentgrep_stdout_bytes"] == len("alpha\n")

    payload = json.dumps(snapshot.to_payload(), sort_keys=True)
    assert "/private/home" not in payload
    assert "--files" not in payload


def test_run_readonly_command_does_not_import_profiler_when_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default command path stays free of profiling imports."""

    def fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["rg", "--version"]
        return subprocess.CompletedProcess(command, 0, "ripgrep\n", "")

    monkeypatch.setattr(agentgrep.subprocess, "run", fake_run)
    monkeypatch.delitem(sys.modules, "agentgrep._engine.profiling", raising=False)

    completed = agentgrep.run_readonly_command(["rg", "--version"])

    assert completed.returncode == 0
    assert "agentgrep._engine.profiling" not in sys.modules


def test_collect_search_records_does_not_import_profiler_when_inactive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-profiled db path keeps the profiler module unloaded."""
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source.path,
        text="tmux",
    )
    query = _make_query()

    def iter_records(_source: agentgrep.SourceHandle) -> t.Iterator[agentgrep.SearchRecord]:
        yield record

    monkeypatch.setattr(_rm_scanning, "iter_source_records", iter_records)
    monkeypatch.delitem(sys.modules, "agentgrep._engine.profiling", raising=False)

    records = agentgrep.collect_search_records(query, [source])

    assert records == [record]
    assert "agentgrep._engine.profiling" not in sys.modules
