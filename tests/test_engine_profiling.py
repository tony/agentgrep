"""Tests for engine-only profiling helpers."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import typing as t

import pytest

import agentgrep
from agentgrep._engine.profiling import (
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


def _make_query(*, limit: int | None = 10) -> agentgrep.SearchQuery:
    """Build a narrow search query for profiling fixtures."""
    return agentgrep.SearchQuery(
        terms=("tmux",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        dedupe=True,
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
