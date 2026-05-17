# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` CLI package."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
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


def _build_empty_ui_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Any:
    """Build a streaming UI app with the search worker stubbed to a no-op."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
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
    return agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())


async def test_streaming_ui_app_mounts_cleanly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the Textual app via ``Pilot`` to surface CSS / mount errors in CI.

    Also asserts the results widget is in the screen's focus chain — the
    Textual API requires ``can_focus=True`` as a class keyword (not a class
    attribute), and that detail is easy to get wrong on a dynamic-base
    subclass.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        focus_chain_ids = {getattr(w, "id", None) for w in app.screen.focus_chain}
        assert "results" in focus_chain_ids, f"#results not in focus chain; chain={focus_chain_ids}"


async def test_tab_moves_focus_from_filter_to_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tab on the filter input moves focus to the DataTable below it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Filter starts focused (first focusable in the chain).
        assert app.focused is not None
        assert app.focused.id == "filter"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "results"


async def test_down_at_empty_filter_releases_focus_to_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``down`` arrow on an empty filter moves focus to the results table."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_up_at_results_top_row_releases_focus_to_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up`` when the results-list cursor is at row 0 moves focus to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Seed one record so the list has a row 0 to be on.
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        # Move focus to the results list and confirm cursor is at row 0.
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        # Ensure highlight is on row 0 before pressing up.
        assert app._results.highlighted in (None, 0)
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_l_from_results_focuses_detail_pane(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vim-style ``l`` (and right-arrow) from the results list focuses the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


async def test_k_at_detail_top_focuses_filter_input(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``k`` / ``up`` on the detail pane at scroll_y=0 releases focus to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        # Pre-condition: at the top of the (short) detail body.
        assert app._detail_scroll.scroll_y <= 0
        await pilot.press("k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_h_from_detail_focuses_results_pane(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vim-style ``h`` (and left-arrow) from the detail pane focuses the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        # Focus the detail-scroll widget directly, then bounce back via ``h``.
        app._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


def _seed_records(
    agentgrep: t.Any,
    tmp_path: pathlib.Path,
    count: int,
) -> list[t.Any]:
    """Build ``count`` ``SearchRecord`` instances under ``tmp_path``."""
    return [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(count)
    ]


async def test_g_on_results_jumps_to_top(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``g`` while the results list is focused snaps the cursor to row 0."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        app._results.highlighted = 3
        await pilot.pause()
        assert app._results.highlighted == 3
        await pilot.press("g")
        await pilot.pause()
        assert app._results.highlighted == 0


async def test_G_on_results_jumps_to_bottom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``G`` while the results list is focused snaps the cursor to the last row."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert app._results.highlighted == 4


async def test_ctrl_d_on_results_advances_half_page(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-D`` on the results list advances the highlight by at least one row."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 20)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        app._results.highlighted = 0
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        # Robust against tiny viewports during ``run_test`` — half-page may be
        # as small as 1 if the simulated screen is shallow. Either way, the
        # cursor must have moved forward and stayed within bounds.
        assert app._results.highlighted is not None
        assert app._results.highlighted > 0
        assert app._results.highlighted <= len(records) - 1


async def test_g_on_detail_scrolls_to_top(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``g`` on the detail pane jumps scroll_y back to 0."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        app._detail_scroll.scroll_to(y=50, animate=False)
        await pilot.pause()
        assert app._detail_scroll.scroll_y > 0
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert app._detail_scroll.scroll_y == 0


async def test_G_on_detail_scrolls_to_bottom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``G`` on the detail pane snaps scroll_y to (near) the maximum."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert app._detail_scroll.scroll_y >= app._detail_scroll.max_scroll_y - 0.5


async def test_ctrl_f_on_detail_pages_down(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-F`` on the detail pane scrolls down by approximately one page."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        before = app._detail_scroll.scroll_y
        await pilot.press("ctrl+f")
        await pilot.pause()
        # Scrolled forward; exact delta depends on viewport size, just assert
        # something happened in the right direction.
        assert app._detail_scroll.scroll_y > before


async def test_ctrl_j_from_filter_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-J`` while the filter input has focus moves focus to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_ctrl_l_from_results_focuses_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-L`` from the results list moves focus rightward to the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


async def test_ctrl_h_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` from the detail pane moves focus leftward to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_ctrl_k_from_results_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the results list moves focus up to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_ctrl_k_from_detail_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the detail pane jumps focus all the way back to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_backspace_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backspace aliases ``Ctrl-H`` in many terminals — should focus results from detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_backspace_in_filter_still_deletes_a_character(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backspace alias must NOT steal backspace from the filter input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("a")
        await pilot.press("b")
        await pilot.press("c")
        await pilot.pause()
        assert app._filter_input.value == "abc"
        await pilot.press("backspace")
        await pilot.pause()
        # Backspace deleted the last character; focus stayed on filter.
        assert app._filter_input.value == "ab"
        assert app.focused.id == "filter"


async def test_ctrl_h_from_filter_is_a_noop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` on the filter does nothing (no pane to the left)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_search_results_list_append_under_load(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending 1000 records to the results list completes within a generous bound.

    Smoke test against accidental O(N²) regressions. Bound is intentionally
    loose because ``OptionList.add_options`` is O(M) per call (vs the prior
    custom widget's O(1)) — we're trading per-record speed for proven
    correctness (visible cursor + Tab focus). If the bound trips on real
    hardware, the escalation path is ``textual-fastdatatable``.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(1000)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        start = time.monotonic()
        app._results.append_records(records)
        elapsed = time.monotonic() - start
        await pilot.pause()
        assert len(app._results._records) == 1000
        assert elapsed < 2.0, f"append_records(1000) took {elapsed:.3f}s; expected < 2.0s"


def test_format_compact_path_passes_short_paths_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Paths that already fit the width budget are returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    short = tmp_path / "a" / "b.txt"
    assert agentgrep.format_compact_path(short, max_width=80) == "~/a/b.txt"


def test_format_compact_path_middle_elides_long_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Long paths get a ``…/`` middle elide, preserving the hidden-dir root."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "uuid.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=30)
    assert result == "~/.codex/…/14/uuid.jsonl"
    assert len(result) <= 30


def test_format_compact_path_drops_root_when_tight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """When even the rooted elide doesn't fit, drop the root: ``…/parent/file``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "verylongfilename.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=20)
    # Either tier-2 (root dropped) or tier-3 (filename only) — whichever fits.
    assert len(result) <= 20
    assert "verylongfilename" in result or "…" in result


def test_truncate_lines_passes_short_text_through() -> None:
    """Short text is returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "a\nb\nc"
    assert agentgrep.truncate_lines(text, max_lines=10) == text


def test_truncate_lines_appends_overflow_marker() -> None:
    """Long text is truncated and a ``+N more`` marker is appended."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "\n".join(f"line {i}" for i in range(50))
    result = agentgrep.truncate_lines(text, max_lines=5)
    assert result.startswith("line 0\nline 1\nline 2\nline 3\nline 4\n")
    assert "(+45 more lines)" in result


async def test_show_detail_caps_body_at_max_lines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` caps the body so giant records render instantly.

    The body is now wrapped in a ``VerticalScroll`` so the cap is a generous
    sanity bound (default 1000 lines), not the visible-height. Test the cap.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    cap = agentgrep.DETAIL_BODY_MAX_LINES
    huge_body = "\n".join(f"body line {i}" for i in range(cap + 1000))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text=huge_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        # ``Static.content`` is the original Group we passed to update().
        # For this plain-text body, the body renderable is a ``Text``.
        group = app._detail.content
        body_text = next(
            item
            for item in group.renderables
            if hasattr(item, "plain") and "body line" in item.plain
        )
        assert "more lines" in body_text.plain
        assert body_text.plain.count("body line") == cap


def test_find_first_match_line_returns_index_of_first_match() -> None:
    """Returns the line index of the first matching line; case-insensitive by default."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta\nFOO bar\nbaz"
    assert agentgrep.find_first_match_line(text, ("foo",)) == 2
    assert agentgrep.find_first_match_line(text, ("foo",), case_sensitive=True) is None
    assert agentgrep.find_first_match_line(text, ("FOO",), case_sensitive=True) == 2
    assert agentgrep.find_first_match_line("", ("foo",)) is None
    assert agentgrep.find_first_match_line(text, ()) is None
    # Regex mode
    assert agentgrep.find_first_match_line(text, (r"b\w+",), regex=True) == 1


def test_find_first_match_line_skips_malformed_regex() -> None:
    """Malformed regex patterns are silently skipped; valid siblings still match."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta gamma\ndelta"
    # ``[`` is unbalanced; should be ignored. ``gamma`` should still match.
    assert agentgrep.find_first_match_line(text, ("[", "gamma"), regex=True) == 1


def test_highlight_matches_styles_each_occurrence() -> None:
    """``highlight_matches`` adds a styled span for every occurrence of every term."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("foo foo bar", ("foo",))
    # Two spans for two occurrences.
    assert sum(1 for span in rich_text.spans if "bold yellow" in str(span.style)) == 2


def test_highlight_matches_combines_terms() -> None:
    """Multiple terms each get their own styled spans."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("alpha beta alpha gamma", ("alpha", "gamma"))
    styled = [str(span.style) for span in rich_text.spans if "bold yellow" in str(span.style)]
    assert len(styled) == 3  # 2 alpha + 1 gamma


async def test_show_detail_scrolls_to_first_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the record body contains a match, the detail-scroll jumps so the match centers."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    # Build the app with a query that has terms; default _build_empty_ui_app
    # uses ``terms=()``, which would make first_match always return None.
    query = agentgrep.SearchQuery(
        terms=("needle",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    # Match lands at line 50 of the body; record_at_match.
    body = "\n".join(["padding"] * 50 + ["this needle is the match"] + ["padding"] * 50)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "match.jsonl",
        text=body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        # Match at body line 50 + 8 header lines = ~line 58; centered into a
        # multi-row viewport, scroll_y should be > 0.
        assert app._detail_scroll.scroll_y > 0


def test_detect_content_format_recognizes_json() -> None:
    """``detect_content_format`` returns ``"json"`` for parseable JSON objects/arrays."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"a": 1, "b": 2}') == "json"
    assert agentgrep.detect_content_format("[1, 2, 3]") == "json"
    # Whitespace + pretty-printed JSON.
    assert agentgrep.detect_content_format('  {\n  "x": 1\n}') == "json"


def test_detect_content_format_falls_back_to_text_for_malformed_json() -> None:
    """A leading ``{`` that doesn't parse falls through to ``"text"``, not ``"json"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"missing": ') == "text"
    assert agentgrep.detect_content_format("{not even json}") == "text"


def test_detect_content_format_recognizes_markdown() -> None:
    """ATX headings and fenced code blocks at line-start trip markdown mode."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("# Heading\n\nbody") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n## Subhead\n\nrest") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n```python\nprint(1)\n```") == "markdown"


def test_detect_content_format_leans_false_negative_for_weak_markdown() -> None:
    """Bullet-style or inline-bold chat content is intentionally NOT classified as markdown."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    # A chat message starting with "- " should keep its match highlight.
    assert agentgrep.detect_content_format("- not really markdown") == "text"
    # Inline **bold** alone isn't enough either.
    assert agentgrep.detect_content_format("plain message with **emphasis** inline") == "text"


def test_detect_content_format_handles_empty_and_plain_text() -> None:
    """Empty body and plain chat prose both return ``"text"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("") == "text"
    assert agentgrep.detect_content_format("just a plain prompt") == "text"
    assert agentgrep.detect_content_format("multi\nline\nplain\nbody") == "text"


async def test_show_detail_renders_json_with_syntax(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON record body produces a ``Syntax`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_syntax = importlib.import_module("rich.syntax")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "json.jsonl",
        text='{"alpha": 1, "beta": "two"}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        rendered = app._detail.content
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_syntax.Syntax) for item in renderables)


async def test_show_detail_renders_markdown_with_markdown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A markdown body produces a ``Markdown`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_markdown = importlib.import_module("rich.markdown")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "md.jsonl",
        text="# Heading\n\nbody paragraph\n",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        rendered = app._detail.content
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_markdown.Markdown) for item in renderables)


async def test_show_detail_keeps_text_highlighting_for_plain_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain-text bodies still get yellow ``highlight_regex`` spans for matches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text_module = importlib.import_module("rich.text")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=("libtmux",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "plain.jsonl",
        text="plain prose mentioning libtmux exactly once",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        rendered = app._detail.content
        renderables = list(rendered.renderables)
        # Two Text instances: the header and the body. The body is the one
        # carrying the highlight spans (header is bold labels only).
        text_bodies = [
            item
            for item in renderables
            if isinstance(item, rich_text_module.Text) and "libtmux" in item.plain
        ]
        assert text_bodies, "expected the body Text containing 'libtmux'"
        styled = [str(span.style) for span in text_bodies[0].spans]
        assert any("bold yellow" in style for style in styled)


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
