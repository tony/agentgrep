# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` CLI package."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import typing as t

import pytest

import agentgrep as _agentgrep_module

if t.TYPE_CHECKING:
    import collections.abc as cabc

AgentName = t.Literal["codex", "claude", "cursor"]
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class BackendSelectionLike(t.Protocol):
    """Structural type for backend selection values."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


class SearchRecordLike(t.Protocol):
    """Structural type for search results used in tests."""

    kind: str
    agent: str
    text: str
    timestamp: str | None
    session_id: str | None
    path: pathlib.Path


class FindRecordLike(t.Protocol):
    """Structural type for find results used in tests."""

    agent: str
    path: pathlib.Path


class SourceHandleLike(t.Protocol):
    """Structural type for discovered sources used in tests."""

    path: pathlib.Path


class SearchQueryFactory(t.Protocol):
    """Factory protocol for query construction."""

    def __call__(
        self,
        *,
        terms: tuple[str, ...],
        search_type: str,
        any_term: bool,
        regex: bool,
        case_sensitive: bool,
        agents: tuple[AgentName, ...],
        limit: int | None,
    ) -> object: ...


class SearchArgsFactory(t.Protocol):
    """Factory protocol for argument construction."""

    def __call__(
        self,
        *,
        terms: tuple[str, ...],
        agents: tuple[AgentName, ...],
        search_type: str,
        any_term: bool,
        regex: bool,
        case_sensitive: bool,
        limit: int | None,
        output_mode: str,
        color_mode: str,
        progress_mode: str,
    ) -> object: ...


class SearchRecordFactory(t.Protocol):
    """Factory protocol for constructing search records."""

    def __call__(
        self,
        *,
        kind: str,
        agent: str,
        store: str,
        adapter_id: str,
        path: pathlib.Path,
        text: str,
    ) -> SearchRecordLike: ...


class BackendSelectionFactory(t.Protocol):
    """Factory protocol for backend selection construction."""

    def __call__(
        self,
        find_tool: str | None,
        grep_tool: str | None,
        json_tool: str | None,
    ) -> BackendSelectionLike: ...


class ShutilLike(t.Protocol):
    """Minimal shutil surface used by tests."""

    def which(self, name: str) -> str | None: ...


class ImportlibLike(t.Protocol):
    """Minimal importlib surface used by tests."""

    def import_module(self, name: str) -> object: ...


class AgentGrepModule(t.Protocol):
    """Structural type for the loaded standalone module."""

    shutil: ShutilLike
    importlib: ImportlibLike
    SearchQuery: SearchQueryFactory
    SearchArgs: SearchArgsFactory
    SearchRecord: SearchRecordFactory
    BackendSelection: BackendSelectionFactory

    def select_backends(self) -> BackendSelectionLike: ...

    def discover_sources(
        self,
        home: pathlib.Path,
        agents: tuple[AgentName, ...],
        backends: BackendSelectionLike,
    ) -> list[SourceHandleLike]: ...

    def search_sources(
        self,
        query: object,
        sources: cabc.Sequence[SourceHandleLike],
        backends: BackendSelectionLike,
    ) -> list[SearchRecordLike]: ...

    def run_search_query(
        self,
        home: pathlib.Path,
        query: object,
        *,
        backends: BackendSelectionLike | None = None,
        progress: object | None = None,
    ) -> list[SearchRecordLike]: ...

    def plan_search_sources(
        self,
        query: object,
        sources: list[SourceHandleLike],
        backends: BackendSelectionLike,
    ) -> list[SourceHandleLike]: ...

    def find_sources(
        self,
        pattern: str | None,
        sources: cabc.Sequence[SourceHandleLike],
        limit: int | None,
    ) -> list[FindRecordLike]: ...

    def print_search_results(self, records: list[SearchRecordLike], args: object) -> None: ...

    def parse_args(self, argv: cabc.Sequence[str] | None = None) -> object | None: ...


def load_agentgrep_module() -> AgentGrepModule:
    """Return the installed ``agentgrep`` package."""
    return t.cast("AgentGrepModule", t.cast("object", _agentgrep_module))


def write_jsonl(path: pathlib.Path, rows: cabc.Sequence[object]) -> None:
    """Write JSONL rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def strip_ansi(text: str) -> str:
    """Return ``text`` without ANSI escape sequences."""
    return ANSI_RE.sub("", text)


def run_agentgrep_cli(
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI in a subprocess via ``python -m agentgrep``."""
    command = [sys.executable, "-m", "agentgrep", *args]
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=merged_env,
    )


def test_select_backends_prefers_first_available(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = load_agentgrep_module()

    def fake_which(name: str) -> str | None:
        mapping = {
            "fd": "/tmp/fd",
            "rg": "/tmp/rg",
            "jq": "/tmp/jq",
        }
        return mapping.get(name)

    monkeypatch.setattr(agentgrep.shutil, "which", fake_which)
    backends = agentgrep.select_backends()

    assert backends.find_tool == "/tmp/fd"
    assert backends.grep_tool == "/tmp/rg"
    assert backends.json_tool == "/tmp/jq"


def test_cli_without_subcommand_prints_main_help() -> None:
    completed = run_agentgrep_cli()

    assert completed.returncode == 0
    assert "usage: agentgrep" in completed.stdout
    assert "search examples:" in completed.stdout
    assert "find examples:" in completed.stdout


def test_search_without_terms_prints_help() -> None:
    completed = run_agentgrep_cli("search")

    assert completed.returncode == 0
    assert "usage: agentgrep search" in completed.stdout
    assert "examples:" in completed.stdout
    assert "agentgrep search bliss" in completed.stdout


def test_find_without_pattern_prints_help() -> None:
    completed = run_agentgrep_cli("find")

    assert completed.returncode == 0
    assert "usage: agentgrep find" in completed.stdout
    assert "examples:" in completed.stdout
    assert "agentgrep find codex" in completed.stdout
    assert "codex history_file" not in completed.stdout


def test_help_examples_are_present_for_help_flags() -> None:
    root_help = run_agentgrep_cli("--help")
    search_help = run_agentgrep_cli("search", "--help")
    find_help = run_agentgrep_cli("find", "--help")

    assert root_help.returncode == 0
    assert search_help.returncode == 0
    assert find_help.returncode == 0
    assert "search examples:" in root_help.stdout
    assert "agentgrep search serenity --json" in search_help.stdout
    assert "agentgrep find cursor --json" in find_help.stdout


def test_search_is_default_verb(tmp_path: pathlib.Path) -> None:
    completed = run_agentgrep_cli(
        "zzz_default_verb_no_match",
        env={"HOME": str(tmp_path)},
    )

    assert "search examples:" not in completed.stdout
    assert "find examples:" not in completed.stdout
    assert completed.returncode == 1
    assert "No matches found." in completed.stderr


def test_default_verb_works_after_global_color_flag(tmp_path: pathlib.Path) -> None:
    completed = run_agentgrep_cli(
        "--color",
        "never",
        "zzz_default_verb_no_match",
        env={"HOME": str(tmp_path)},
    )

    assert "search examples:" not in completed.stdout
    assert completed.returncode == 1


def test_search_progress_mode_parses_default_and_explicit() -> None:
    agentgrep = load_agentgrep_module()

    default_args = t.cast("t.Any", agentgrep.parse_args(["search", "bliss"]))
    disabled_args = t.cast(
        "t.Any",
        agentgrep.parse_args(["search", "--progress", "never", "bliss"]),
    )

    assert default_args.progress_mode == "auto"
    assert disabled_args.progress_mode == "never"


def test_answer_now_enabled_only_for_interactive_text_progress() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    class PlainStream(io.StringIO):
        def isatty(self) -> bool:
            return False

    text_args = agentgrep.SearchArgs(
        terms=("bliss",),
        agents=("codex",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="auto",
        progress_mode="auto",
    )
    json_args = dataclasses.replace(text_args, output_mode="json")
    no_progress_args = dataclasses.replace(text_args, progress_mode="never")

    assert agentgrep.should_enable_answer_now(
        text_args,
        stdin=TtyStream(),
        stderr=TtyStream(),
    )
    assert not agentgrep.should_enable_answer_now(
        json_args,
        stdin=TtyStream(),
        stderr=TtyStream(),
    )
    assert not agentgrep.should_enable_answer_now(
        no_progress_args,
        stdin=TtyStream(),
        stderr=TtyStream(),
    )
    assert not agentgrep.should_enable_answer_now(
        text_args,
        stdin=PlainStream(),
        stderr=TtyStream(),
    )


def test_root_help_not_rewritten_by_default_verb() -> None:
    completed = run_agentgrep_cli("--help")

    assert completed.returncode == 0
    assert "search examples:" in completed.stdout
    assert "find examples:" in completed.stdout


def test_force_color_colorizes_help_output() -> None:
    completed = run_agentgrep_cli(
        "--color",
        "always",
        "search",
        env={"FORCE_COLOR": "1", "NO_COLOR": ""},
    )

    assert completed.returncode == 0
    assert "\x1b[" in completed.stdout


def test_no_color_overrides_color_always() -> None:
    completed = run_agentgrep_cli(
        "--color",
        "always",
        "search",
        env={"NO_COLOR": "1"},
    )

    assert completed.returncode == 0
    assert "\x1b[" not in completed.stdout


def test_search_codex_prompt_match_returns_full_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "model_provider": "openai"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "A serenity prompt with bliss and detail.",
                        },
                    ],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Assistant reply"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("serenity", "bliss"),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].kind == "prompt"
    assert records[0].text == "A serenity prompt with bliss and detail."
    assert records[0].session_id == "session-1"


def test_search_reports_source_and_match_progress(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    first = home / ".codex" / "sessions" / "2026" / "01" / "01" / "first.jsonl"
    second = home / ".codex" / "sessions" / "2026" / "01" / "01" / "second.jsonl"
    write_jsonl(
        first,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    write_jsonl(
        second,
        [{"type": "response_item", "payload": {"role": "user", "content": "other"}}],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    class RecordingProgress:
        def __init__(self) -> None:
            self.events: list[tuple[str, int | str]] = []

        def start(self, query: object) -> None:
            self.events.append(("start", 0))

        def sources_discovered(self, count: int) -> None:
            self.events.append(("discovered", count))

        def prefilter_started(self, root: pathlib.Path) -> None:
            self.events.append(("prefilter", root.name))

        def sources_planned(self, planned: int, total: int) -> None:
            self.events.append(("planned", planned))
            self.events.append(("total", total))

        def source_started(self, index: int, total: int, source: object) -> None:
            self.events.append(("source_started", index))

        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None:
            self.events.append(("source_finished", matches))

        def result_added(self, count: int) -> None:
            self.events.append(("result_added", count))

        def record_added(self, record: object) -> None:
            self.events.append(("record_added", getattr(record, "kind", "?")))

        def finish(self, result_count: int) -> None:
            self.events.append(("finish", result_count))

        def close(self) -> None:
            self.events.append(("close", 0))

    progress = RecordingProgress()
    records = agentgrep.run_search_query(
        home,
        query,
        backends=agentgrep.BackendSelection(None, None, None),
        progress=progress,
    )

    assert len(records) == 1
    assert ("discovered", 2) in progress.events
    assert ("planned", 2) in progress.events
    assert ("source_started", 1) in progress.events
    assert ("source_started", 2) in progress.events
    assert ("source_finished", 1) in progress.events
    assert ("result_added", 1) in progress.events
    assert progress.events[-2:] == [("finish", 1), ("close", 0)]


def test_collect_search_records_calls_record_added_with_each_unique_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
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
        text="bliss",
        session_id="abc",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    class CapturingProgress:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.counts: list[int] = []

        def start(self, query: object) -> None: ...
        def sources_discovered(self, count: int) -> None: ...
        def prefilter_started(self, root: pathlib.Path) -> None: ...
        def sources_planned(self, planned: int, total: int) -> None: ...
        def source_started(self, index: int, total: int, source: object) -> None: ...
        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None: ...
        def result_added(self, count: int) -> None:
            self.counts.append(count)

        def record_added(self, record: object) -> None:
            self.added.append(record)

        def finish(self, result_count: int) -> None: ...
        def close(self) -> None: ...

    def iter_records(source: object) -> cabc.Iterator[object]:
        yield record
        yield record  # same dedupe key — second insert must not fire record_added

    monkeypatch.setattr(agentgrep, "iter_source_records", iter_records)
    progress = CapturingProgress()

    records = agentgrep.collect_search_records(query, [source], progress=progress)

    assert records == [record]
    assert progress.added == [record]
    assert progress.counts == [1]


def test_streaming_search_progress_buffers_and_flushes_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    # Freeze the clock so the self-pacing auto-flush threshold (50 ms) never
    # fires during the explicit-flush sequence; the test exercises the
    # buffer/explicit-flush surface only.
    monkeypatch.setattr(agentgrep.time, "monotonic", lambda: 0.0)
    emitted: list[object] = []
    progress = agentgrep.StreamingSearchProgress(emit=emitted.append)
    record_a = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="a",
    )
    record_b = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "b.jsonl",
        text="b",
    )

    progress.record_added(record_a)
    progress.record_added(record_b)
    progress.result_added(2)
    progress.flush()

    batches = [e for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)]
    assert len(batches) == 1
    assert batches[0].records == (record_a, record_b)
    assert batches[0].total == 2

    progress.flush()
    assert sum(1 for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)) == 1


def test_streaming_search_progress_self_paces_flush(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_added`` auto-flushes once the 50 ms batching window elapses."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    clock = {"now": 0.0}
    monkeypatch.setattr(agentgrep.time, "monotonic", lambda: clock["now"])
    emitted: list[object] = []
    progress = agentgrep.StreamingSearchProgress(emit=emitted.append)
    record_a = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="a",
    )
    record_b = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "b.jsonl",
        text="b",
    )

    progress.record_added(record_a)
    assert [e for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)] == []

    clock["now"] = agentgrep.StreamingSearchProgress._FLUSH_INTERVAL_SECONDS + 0.01
    progress.record_added(record_b)

    batches = [e for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)]
    assert len(batches) == 1
    assert batches[0].records == (record_a, record_b)


def test_streaming_search_progress_translates_progress_callbacks(
    tmp_path: pathlib.Path,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    emitted: list[object] = []
    progress = agentgrep.StreamingSearchProgress(emit=emitted.append)
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
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

    progress.start(query)
    progress.sources_discovered(10)
    progress.sources_planned(7, 10)
    progress.source_started(1, 7, source)
    progress.source_finished(1, 7, source, records=5, matches=2)
    progress.result_added(2)
    progress.finish(2)

    snapshots = [e for e in emitted if isinstance(e, agentgrep.ProgressSnapshot)]
    finished = [e for e in emitted if isinstance(e, agentgrep.StreamingSearchFinished)]

    assert len(snapshots) == 5
    assert snapshots[0].phase == "discovering"
    assert snapshots[0].query_label == "bliss"
    assert snapshots[1].phase == "discovered"
    assert snapshots[1].detail == "10 sources"
    assert snapshots[2].phase == "planning"
    assert snapshots[2].current == 7
    assert snapshots[2].total == 10
    assert snapshots[3].phase == "scanning"
    assert snapshots[3].current == 1
    assert snapshots[3].total == 7
    assert snapshots[3].detail == "session.jsonl"
    assert snapshots[4].phase == "scanning"
    assert snapshots[4].detail is not None
    assert "matches" in snapshots[4].detail

    assert len(finished) == 1
    assert finished[0].outcome == "complete"
    assert finished[0].total == 2
    assert finished[0].elapsed >= 0.0


def test_compute_filter_matches_returns_substring_matches(
    tmp_path: pathlib.Path,
) -> None:
    """The filter worker's pure helper matches by case-folded substring."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    blissful = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "bliss.jsonl",
        text="serene BLISS abounds",
    )
    other = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "other.jsonl",
        text="unrelated text",
    )

    matches = agentgrep.compute_filter_matches([blissful, other], "bliss")
    assert matches == (blissful,)

    no_matches = agentgrep.compute_filter_matches([blissful, other], "xyz")
    assert no_matches == ()


def test_compute_filter_matches_empty_text_returns_all(tmp_path: pathlib.Path) -> None:
    """Whitespace-only or empty filter text returns every record unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="anything",
    )
    assert agentgrep.compute_filter_matches([record], "") == (record,)
    assert agentgrep.compute_filter_matches([record], "   ") == (record,)


async def test_streaming_ui_app_mounts_cleanly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the Textual app via ``Pilot`` to surface CSS / mount errors in CI."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Quiet the search worker so it doesn't try to walk a real codex/claude/cursor tree
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    query = agentgrep.SearchQuery(
        terms=(),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    async with app.run_test() as pilot:
        await pilot.pause()
        # If we got here, CSS parsed and on_mount completed without raising.


def test_pydantic_payloads_reject_wrong_types(tmp_path: pathlib.Path) -> None:
    """Payload models validate field types at construction time."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="hi",
    )

    # Happy path constructs cleanly
    rap = agentgrep.RecordsAppendedPayload(records=(record,), total=1)
    assert rap.records == (record,)
    assert rap.total == 1

    sfp = agentgrep.SearchFinishedPayload(outcome="complete", total=1, elapsed=0.5)
    assert sfp.error_message is None

    fcp = agentgrep.FilterCompletedPayload(text="abc", matching=(record,))
    assert fcp.text == "abc"

    # Wrong types raise ValidationError
    with pytest.raises(agentgrep.pydantic.ValidationError):
        agentgrep.SearchFinishedPayload(outcome="not-a-valid-outcome", total=0, elapsed=0.0)
    with pytest.raises(agentgrep.pydantic.ValidationError):
        agentgrep.FilterRequestedPayload(text=None)  # type: ignore[arg-type]


def test_collect_search_records_returns_partial_results_on_answer_now(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
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
    first = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source.path,
        text="first bliss",
    )
    second = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source.path,
        text="second bliss",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()

    def iter_records(source: object) -> cabc.Iterator[object]:
        yield first
        control.request_answer_now()
        yield second

    monkeypatch.setattr(agentgrep, "iter_source_records", iter_records)

    records = agentgrep.collect_search_records(query, [source], control=control)

    assert records == [first]
    assert control.answer_now_requested()


def test_run_search_command_starts_and_stops_answer_now_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    events: list[str] = []
    args = agentgrep.SearchArgs(
        terms=("bliss",),
        agents=("codex",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="never",
        progress_mode="auto",
    )

    class FakeListener:
        def __init__(self, control: object) -> None:
            self.control = t.cast("t.Any", control)

        def start(self) -> None:
            events.append("start")
            self.control.request_answer_now()

        def stop(self) -> None:
            events.append("stop")

    def fake_run_search_query(
        home: pathlib.Path,
        query: object,
        *,
        progress: object,
        control: object,
    ) -> list[object]:
        typed_progress = t.cast("t.Any", progress)
        typed_control = t.cast("t.Any", control)
        assert typed_control.answer_now_requested()
        typed_progress.answer_now(0)
        return []

    monkeypatch.setattr(agentgrep, "should_enable_answer_now", lambda args: True)
    monkeypatch.setattr(agentgrep, "AnswerNowInputListener", FakeListener)
    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    err = io.StringIO()

    with contextlib.redirect_stderr(err):
        exit_code = agentgrep.run_search_command(args)

    assert exit_code == 1
    assert events == ["start", "stop"]
    assert "Answering now: 0 matches" in err.getvalue()


def test_run_search_query_interrupts_progress_on_keyboard_interrupt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "first.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    class RecordingProgress:
        def __init__(self) -> None:
            self.events: list[str] = []

        def start(self, query: object) -> None:
            self.events.append("start")

        def sources_discovered(self, count: int) -> None:
            self.events.append("sources_discovered")

        def prefilter_started(self, root: pathlib.Path) -> None:
            self.events.append("prefilter_started")

        def sources_planned(self, planned: int, total: int) -> None:
            self.events.append("sources_planned")

        def source_started(self, index: int, total: int, source: object) -> None:
            self.events.append("source_started")

        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None:
            self.events.append("source_finished")

        def result_added(self, count: int) -> None:
            self.events.append("result_added")

        def record_added(self, record: object) -> None:
            self.events.append("record_added")

        def finish(self, result_count: int) -> None:
            self.events.append("finish")

        def interrupt(self) -> None:
            self.events.append("interrupt")

        def close(self) -> None:
            self.events.append("close")

    def raise_interrupt(source: object) -> cabc.Iterator[object]:
        raise KeyboardInterrupt
        yield source

    progress = RecordingProgress()
    monkeypatch.setattr(agentgrep, "iter_source_records", raise_interrupt)

    with pytest.raises(KeyboardInterrupt):
        agentgrep.run_search_query(
            home,
            query,
            backends=agentgrep.BackendSelection(None, None, None),
            progress=progress,
        )

    assert progress.events[-1] == "interrupt"
    assert "close" not in progress.events
    assert "finish" not in progress.events


def test_plan_search_sources_prefilters_one_root_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    first = home / ".codex" / "sessions" / "2026" / "01" / "01" / "one.jsonl"
    second = home / ".codex" / "sessions" / "2026" / "01" / "01" / "two.jsonl"
    write_jsonl(
        first,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    write_jsonl(
        second,
        [{"type": "response_item", "payload": {"role": "user", "content": "other"}}],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        control: object | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, f"{first}\n", "")

    monkeypatch.setattr(agentgrep, "run_readonly_command", fake_run)
    planned = agentgrep.plan_search_sources(
        query,
        sources,
        agentgrep.BackendSelection(None, "/fake/rg", None),
    )

    assert len(calls) == 1
    assert [source.path for source in planned] == [first]


def test_search_prefers_newer_sources_when_limiting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    older = home / ".codex" / "sessions" / "2026" / "01" / "01" / "a-old.jsonl"
    newer = home / ".codex" / "sessions" / "2026" / "01" / "01" / "z-new.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "bliss appears here"}],
            },
        },
    ]
    write_jsonl(older, rows)
    write_jsonl(newer, rows)
    older_mtime_ns = 1_700_000_000_000_000_000
    newer_mtime_ns = older_mtime_ns + 1000
    os.utime(older, ns=(older_mtime_ns, older_mtime_ns))
    os.utime(newer, ns=(newer_mtime_ns, newer_mtime_ns))

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].path == newer


def test_search_dedupes_identical_prompts_within_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "dupes.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].timestamp == "2026-01-01T00:01:00Z"


def test_search_keeps_identical_prompts_across_sessions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    first = home / ".codex" / "sessions" / "2026" / "01" / "01" / "first.jsonl"
    second = home / ".codex" / "sessions" / "2026" / "01" / "01" / "second.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "bliss prompt"}],
            },
        },
    ]
    write_jsonl(first, rows)
    write_jsonl(second, rows)

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 2
    assert {record.path for record in records} == {first, second}


def test_search_limit_applies_to_unique_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "limit.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss second"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=2,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 2
    assert [record.text for record in records] == ["bliss second", "bliss prompt"]


def test_search_codex_history_json_returns_history_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text(
        json.dumps(
            [
                {
                    "command": "serenity command example",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            ],
        ),
        encoding="utf-8",
    )

    query = agentgrep.SearchQuery(
        terms=("serenity",),
        search_type="history",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].kind == "history"
    assert records[0].text == "serenity command example"


def test_cursor_ai_tracking_summary_is_exposed_as_history(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    db_path = home / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute(
        """
        CREATE TABLE conversation_summaries (
            conversationId TEXT,
            title TEXT,
            tldr TEXT,
            overview TEXT,
            summaryBullets TEXT,
            model TEXT,
            mode TEXT,
            updatedAt TEXT
        )
        """,
    )
    _ = connection.execute(
        """
        INSERT INTO conversation_summaries
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "conv-1",
            "Serenity Session",
            "bliss summary",
            "overview text",
            json.dumps(["bullet one", "bullet two"]),
            "gpt-5",
            "chat",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    query = agentgrep.SearchQuery(
        terms=("serenity", "bliss"),
        search_type="history",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("cursor",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("cursor",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].agent == "cursor"
    assert records[0].kind == "history"
    assert "bliss summary" in records[0].text


def test_cursor_state_itemtable_extracts_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    db_path = home / ".cursor" / "state.vscdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    payload = {
        "messages": [
            {"role": "user", "content": "serenity and bliss live here"},
            {"role": "assistant", "content": "response"},
        ],
    }
    _ = connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("workbench.panel.chat.composerData", json.dumps(payload)),
    )
    connection.commit()
    connection.close()

    query = agentgrep.SearchQuery(
        terms=("serenity", "bliss"),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("cursor",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("cursor",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].kind == "prompt"
    assert records[0].text == "serenity and bliss live here"


def test_find_discovers_sources_and_filters_pattern(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    codex_history = home / ".codex" / "history.json"
    codex_history.parent.mkdir(parents=True, exist_ok=True)
    _ = codex_history.write_text("[]", encoding="utf-8")

    cursor_db = home / ".cursor" / "state.vscdb"
    cursor_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(cursor_db)
    connection.close()

    sources = agentgrep.discover_sources(
        home,
        ("codex", "cursor"),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.find_sources("state", sources, None)

    assert len(records) == 1
    assert records[0].agent == "cursor"
    assert records[0].path.name == "state.vscdb"


def test_display_path_collapses_home_and_marks_directories(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert agentgrep.format_display_path(home / ".codex" / "sessions", directory=True) == (
        "~/.codex/sessions/"
    )
    assert (
        agentgrep.format_display_path(
            home / ".codex" / "sessions" / "rollout.jsonl",
        )
        == "~/.codex/sessions/rollout.jsonl"
    )
    assert agentgrep.format_display_path(home, directory=True) == "~/"
    assert (
        agentgrep.format_display_path(
            pathlib.Path("~/.codex/sessions"),
            directory=True,
        )
        == "~/.codex/sessions/"
    )
    assert (
        agentgrep.format_display_path(
            pathlib.Path(f"{home}-other") / "sessions",
            directory=True,
        )
        == f"{home}-other/sessions/"
    )


def test_search_json_output_uses_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="serenity and bliss",
    )
    args = agentgrep.SearchArgs(
        terms=("serenity",),
        agents=("codex",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="json",
        color_mode="auto",
        progress_mode="auto",
    )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        agentgrep.print_search_results([record], args)

    payload = t.cast("dict[str, object]", json.loads(buffer.getvalue()))
    results = t.cast("list[dict[str, object]]", payload["results"])
    assert results[0]["path"] == "~/.codex/sessions/rollout.jsonl"
    assert str(home) not in buffer.getvalue()


def test_text_outputs_use_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    search_record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="serenity and bliss",
    )
    find_record = agentgrep.FindRecord(
        kind="find",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions",
        path_kind="session_file",
    )
    search_args = agentgrep.SearchArgs(
        terms=("serenity",),
        agents=("codex",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="auto",
        progress_mode="auto",
    )
    find_args = agentgrep.FindArgs(
        pattern="sessions",
        agents=("codex",),
        limit=None,
        output_mode="text",
        color_mode="auto",
    )

    search_buffer = io.StringIO()
    with contextlib.redirect_stdout(search_buffer):
        agentgrep.print_search_results([search_record], search_args)
    find_buffer = io.StringIO()
    with contextlib.redirect_stdout(find_buffer):
        agentgrep.print_find_results([find_record], find_args)

    assert "~/.codex/sessions/rollout.jsonl" in search_buffer.getvalue()
    assert "~/.codex/sessions" in find_buffer.getvalue()
    assert str(home) not in search_buffer.getvalue()
    assert str(home) not in find_buffer.getvalue()


def test_source_handle_serialization_uses_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=home / ".codex" / "sessions",
        mtime_ns=123,
    )

    payload = agentgrep.serialize_source_handle(source)

    assert payload["path"] == "~/.codex/sessions/rollout.jsonl"
    assert payload["search_root"] == "~/.codex/sessions/"
    assert str(home) not in json.dumps(payload)


def test_find_record_serialization_uses_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.FindRecord(
        kind="find",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_json.v1",
        path=home / ".codex" / "history.json",
        path_kind="history_file",
    )

    payload = agentgrep.serialize_find_record(record)

    assert payload["path"] == "~/.codex/history.json"
    assert str(home) not in json.dumps(payload)


def test_json_output_falls_back_without_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = load_agentgrep_module()
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/example.jsonl"),
        text="serenity and bliss",
    )
    args = agentgrep.SearchArgs(
        terms=("serenity",),
        agents=("codex",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="json",
        color_mode="auto",
        progress_mode="auto",
    )

    original_import_module = agentgrep.importlib.import_module

    def fake_import_module(name: str, package: str | None = None) -> object:
        if name == "pydantic":
            raise ImportError
        return original_import_module(name)

    monkeypatch.setattr(agentgrep.importlib, "import_module", fake_import_module)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        agentgrep.print_search_results([record], args)

    payload = t.cast("dict[str, object]", json.loads(buffer.getvalue()))
    assert payload["schema_version"] == "agentgrep.v1"
    results = t.cast("list[dict[str, object]]", payload["results"])
    assert results[0]["text"] == "serenity and bliss"


def test_json_output_default_does_not_emit_progress(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )

    completed = run_agentgrep_cli("search", "bliss", "--json", env={"HOME": str(home)})

    assert completed.returncode == 0
    payload = t.cast("dict[str, object]", json.loads(completed.stdout))
    assert payload["command"] == "search"
    assert completed.stderr == ""


def test_json_output_progress_always_writes_stderr_only(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )

    completed = run_agentgrep_cli(
        "search",
        "bliss",
        "--json",
        "--progress",
        "always",
        env={"HOME": str(home)},
    )

    assert completed.returncode == 0
    payload = t.cast("dict[str, object]", json.loads(completed.stdout))
    assert payload["command"] == "search"
    assert "Searching bliss" in completed.stderr
    assert "Search complete: 1 match" in completed.stderr


def test_json_output_progress_color_always_colours_only_stderr(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )

    completed = run_agentgrep_cli(
        "--color",
        "always",
        "search",
        "bliss",
        "--json",
        "--progress",
        "always",
        env={
            "HOME": str(home),
            "NO_COLOR": "",
            "FORCE_COLOR": "",
        },
    )

    assert completed.returncode == 0
    payload = t.cast("dict[str, object]", json.loads(completed.stdout))
    assert payload["command"] == "search"
    assert "\x1b[" not in completed.stdout
    assert "\x1b[" in completed.stderr
    assert "Search complete:" in strip_ansi(completed.stderr)


def test_progress_no_color_overrides_color_always(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.setenv("NO_COLOR", "1")
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="always",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.finish(1)
    progress.close()

    out = stream.getvalue()
    assert "\x1b[" not in out
    assert "Search complete: 1 match" in out


def test_progress_force_color_enables_auto_for_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="auto",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.finish(1)
    progress.close()

    out = stream.getvalue()
    assert "\x1b[" in out
    assert "Searching bliss" in strip_ansi(out)


def test_non_tty_progress_emits_start_heartbeat_and_finish() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        heartbeat_interval=10.0,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=1, total=3, detail="one.jsonl")
    progress._last_heartbeat_at -= 30.0
    progress.set_status("scanning", current=2, total=3, detail="two.jsonl")
    progress.finish(4)
    progress.close()

    out = stream.getvalue()
    assert "Searching bliss" in out
    assert "... still searching bliss" in out
    assert "scanning 2/3 sources" in out
    assert "Search complete: 4 matches" in out


def test_answer_now_input_listener_requests_on_blank_enter() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    control = agentgrep.SearchControl()
    listener = agentgrep.AnswerNowInputListener(control, stream=TtyInput("\n"))

    listener.start()
    deadline = time.monotonic() + 1.0
    while not control.answer_now_requested() and time.monotonic() < deadline:
        time.sleep(0.01)
    listener.stop()

    assert control.answer_now_requested()


def test_answer_now_input_listener_ignores_nonblank_input() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    control = agentgrep.SearchControl()
    listener = agentgrep.AnswerNowInputListener(control, stream=TtyInput("not yet\n"))

    listener.start()
    time.sleep(0.01)
    listener.stop()

    assert not control.answer_now_requested()


def test_run_readonly_command_terminates_when_answer_now_requested() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    control = agentgrep.SearchControl()
    timer = threading.Timer(0.05, control.request_answer_now)
    timer.start()
    try:
        completed = agentgrep.run_readonly_command(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
            control=control,
        )
    finally:
        timer.cancel()

    assert control.answer_now_requested()
    assert completed.returncode != 0


def test_tty_progress_renders_spinner_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="always",
        refresh_interval=0.01,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=1, total=2, detail="one.jsonl")
    progress.result_added(1)
    time.sleep(0.03)
    progress.finish(1)
    progress.close()

    out = stream.getvalue()
    plain = strip_ansi(out)
    assert "Searching bliss" in plain
    assert "scanning 1/2 sources" in plain
    assert any(f"\x1b[36m{frame}\x1b[0m" in out for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert "\x1b[35mbliss\x1b[0m" in out
    assert "\x1b[33m1 match\x1b[0m" in out
    assert "\r\x1b[2K" in out


def test_tty_progress_renders_answer_now_hint() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=0.01,
        answer_now_hint=True,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    time.sleep(0.03)
    progress.answer_now(3)

    out = stream.getvalue()
    assert "[Press enter, answer now]" in out
    assert "Answering now: 3 matches" in out
    assert out.endswith("\n")


def test_tty_progress_answer_now_hint_is_white(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="always",
        refresh_interval=0.01,
        answer_now_hint=True,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    time.sleep(0.03)
    progress.answer_now(1)

    assert "\x1b[37m[Press enter, answer now]\x1b[0m" in stream.getvalue()


def test_tty_progress_interrupt_preserves_current_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=0.01,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=118, total=126, detail="rollout.jsonl")
    progress.result_added(109)
    time.sleep(0.03)
    progress.interrupt()

    out = stream.getvalue()
    assert "Searching bliss | scanning 118/126 sources | 109 matches" in out
    assert out.endswith("\n")
    assert "\r\x1b[2KSearching bliss | scanning 118/126 sources | 109 matches" in out


def test_tty_progress_prefilter_uses_private_directory_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=0.01,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.prefilter_started(home / ".codex" / "sessions")
    time.sleep(0.03)
    progress.interrupt()

    out = stream.getvalue()
    assert "prefiltering ~/.codex/sessions/" in out
    assert str(home) not in out


def test_non_tty_progress_interrupt_emits_current_summary() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="never",
        heartbeat_interval=10.0,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=118, total=126, detail="rollout.jsonl")
    progress.result_added(109)
    progress.interrupt()

    out = stream.getvalue()
    assert "Searching bliss\n" in out
    assert "Searching bliss | scanning 118/126 sources | 109 matches" in out


def test_main_handles_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    args = agentgrep.SearchArgs(
        terms=("bliss",),
        agents=("codex",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        limit=None,
        output_mode="text",
        color_mode="never",
        progress_mode="auto",
    )

    def parse_args(argv: cabc.Sequence[str] | None = None) -> object:
        return args

    def run_search_command(args: object) -> int:
        raise KeyboardInterrupt

    def exit_on_sigint() -> t.NoReturn:
        raise SystemExit(130)

    monkeypatch.setattr(agentgrep, "parse_args", parse_args)
    monkeypatch.setattr(agentgrep, "run_search_command", run_search_command)
    monkeypatch.setattr(agentgrep, "_exit_on_sigint", exit_on_sigint)

    with pytest.raises(SystemExit) as excinfo:
        agentgrep.main(["search", "bliss"])

    assert excinfo.value.code == 130
    captured = capsys.readouterr()
    assert "Interrupted by user." in captured.err
    assert "Traceback" not in captured.err


def test_exit_on_sigint_posix_installs_default_handler_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(sys, "platform", "linux")
    calls: list[tuple[str, int, object]] = []

    def signal_handler(sig: int, handler: object) -> object:
        calls.append(("signal", sig, handler))
        return None

    def raise_signal(sig: int) -> None:
        calls.append(("raise_signal", sig, None))
        raise SystemExit(130)

    monkeypatch.setattr(signal, "signal", signal_handler)
    monkeypatch.setattr(signal, "raise_signal", raise_signal)

    with pytest.raises(SystemExit) as excinfo:
        agentgrep._exit_on_sigint()

    assert excinfo.value.code == 130
    assert calls == [
        ("signal", signal.SIGINT, signal.SIG_IGN),
        ("signal", signal.SIGINT, signal.SIG_DFL),
        ("raise_signal", signal.SIGINT, None),
    ]


def test_exit_on_sigint_windows_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(sys, "platform", "win32")

    def fail_if_called(*args: object) -> None:
        msg = "signal APIs should not be called on Windows fallback"
        raise AssertionError(msg)

    monkeypatch.setattr(signal, "signal", fail_if_called)
    monkeypatch.setattr(signal, "raise_signal", fail_if_called)

    with pytest.raises(SystemExit) as excinfo:
        agentgrep._exit_on_sigint()

    assert excinfo.value.code == 130


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics only; Windows uses exit-code 130",
)
def test_exit_on_sigint_produces_wifsignaled_sigint() -> None:
    runner = (
        "import signal\n"
        "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
        "from agentgrep import _exit_on_sigint\n"
        "try:\n"
        "    signal.raise_signal(signal.SIGINT)\n"
        "except KeyboardInterrupt:\n"
        "    _exit_on_sigint()\n"
    )
    src_dir = pathlib.Path(__file__).resolve().parents[1] / "src"
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(src_dir), os.environ.get("PYTHONPATH", "")) if p
        ),
    }

    completed = subprocess.run(
        [sys.executable, "-c", runner],
        env=env,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == -signal.SIGINT, (
        f"expected WIFSIGNALED(SIGINT) (-{int(signal.SIGINT)}), "
        f"got returncode={completed.returncode}; stderr={completed.stderr!r}"
    )
