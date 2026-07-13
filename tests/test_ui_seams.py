"""The engine seam lets the app shell call narrow Protocols, not engine guts.

These structural tests prove a fake double satisfies each ``Protocol`` (so
widget/app tests can fake the engine) and that the concrete adapter is itself a
``SearchInvoker`` — without running a real search.
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import threading

from agentgrep.progress import ProgressSnapshot, SearchControl
from agentgrep.records import SearchQuery, SourceHandle
from agentgrep.ui._seams import (
    EngineSearchInvoker,
    SearchInvoker,
    _UiStreamingSearchProgress,
)
from agentgrep.ui._source_diagnostics import (
    SourceScanFinished,
    SourceScanStarted,
    UiProgressSnapshot,
)


def _make_query() -> SearchQuery:
    """Build a minimal valid prompts-scope query."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


class _FakeInvoker:
    """A test double that records calls and emits a sentinel event."""

    def __init__(self) -> None:
        self.calls: list[SearchQuery] = []

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Record ``query`` and emit a single ``"done"`` event."""
        self.calls.append(query)
        emit("done")


def test_fake_satisfies_search_invoker_protocol() -> None:
    """A structural fake is accepted as a SearchInvoker and forwards events."""
    invoker: SearchInvoker = _FakeInvoker()
    seen: list[object] = []
    invoker.run(_make_query(), control=SearchControl(), emit=seen.append)
    assert seen == ["done"]


def test_engine_invoker_satisfies_protocol() -> None:
    """EngineSearchInvoker is structurally a SearchInvoker (not run here)."""
    invoker: SearchInvoker = EngineSearchInvoker(pathlib.Path("/nonexistent"))
    assert callable(invoker.run)


def _make_source(path: pathlib.Path) -> SourceHandle:
    """Build a source whose private path must not become its diagnostic label."""
    return SourceHandle(
        agent="cursor-ide",
        store="cursor-ide.state_vscdb",
        adapter_id="cursor-ide.state_vscdb.v1",
        path=path,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=1,
    )


def test_ui_reporter_coalesces_lifecycle_without_extra_emissions(
    tmp_path: pathlib.Path,
) -> None:
    """Each source callback still crosses the pump exactly once."""
    emitted: list[object] = []
    reporter = _UiStreamingSearchProgress(emit=emitted.append)
    source = _make_source(tmp_path / "private-state.vscdb")

    reporter.source_started(3, 82, source)
    reporter.source_progress(3, 82, source, records=128, matches=1)
    reporter.source_finished(3, 82, source, records=256, matches=1)

    assert len(emitted) == 3
    started, progress, finished = emitted
    assert isinstance(started, UiProgressSnapshot)
    assert isinstance(started.snapshot, ProgressSnapshot)
    assert isinstance(started.lifecycle, SourceScanStarted)
    assert started.lifecycle.store == "cursor-ide.state_vscdb"
    assert not hasattr(started.lifecycle, "started_at")
    assert not hasattr(started.lifecycle, "path")
    assert isinstance(progress, ProgressSnapshot)
    assert isinstance(finished, UiProgressSnapshot)
    assert isinstance(finished.lifecycle, SourceScanFinished)


def test_ui_reporter_lifecycle_context_is_thread_local(
    tmp_path: pathlib.Path,
) -> None:
    """A concurrent heartbeat cannot steal another thread's start marker."""
    emitted: list[object] = []
    start_emit_entered = threading.Event()
    release_start_emit = threading.Event()

    def emit(event: object) -> None:
        if isinstance(event, UiProgressSnapshot) and isinstance(
            event.lifecycle,
            SourceScanStarted,
        ):
            start_emit_entered.set()
            assert release_start_emit.wait(timeout=2.0)
        emitted.append(event)

    reporter = _UiStreamingSearchProgress(emit=emit)
    source = _make_source(tmp_path / "private-state.vscdb")
    started = threading.Thread(target=reporter.source_started, args=(3, 82, source))
    started.start()
    assert start_emit_entered.wait(timeout=2.0)

    reporter.source_progress(3, 82, source, records=128, matches=1)
    release_start_emit.set()
    started.join(timeout=2.0)

    assert not started.is_alive()
    assert sum(isinstance(event, UiProgressSnapshot) for event in emitted) == 1
    assert sum(isinstance(event, ProgressSnapshot) for event in emitted) == 1
