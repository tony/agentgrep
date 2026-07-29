"""The console reporter must acknowledge an answer-now request when it lands.

Ranking what has been collected runs for seconds before
:meth:`agentgrep.ConsoleSearchProgress.answer_now` prints, and sources still
draining keep pushing ``"scanning"`` status updates through that window. These
tests drive the reporter's state machine directly — no clocks, no threads
except the listener's own — and pin the acknowledgment to the phase label,
which is the only segment that survives at the narrow panes where the
``[Press enter, answer now]`` reminder is abbreviated or absent.
"""

from __future__ import annotations

import io
import pathlib
import threading
import typing as t

import pytest

from agentgrep import (
    AnswerNowInputListener,
    ConsoleSearchProgress,
    SearchControl,
    SearchQuery,
    SourceHandle,
)
from agentgrep.cli.render import _build_search_feedback
from agentgrep.progress import NoopSearchProgress

if t.TYPE_CHECKING:
    import collections.abc as cabc


def _query() -> SearchQuery:
    """Return one plain single-term prompt query."""
    return SearchQuery(
        terms=("tmux",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )


def _source() -> SourceHandle:
    """Return one synthetic source handle for the progress callbacks."""
    return SourceHandle(
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/home/example/.codex/history.jsonl"),
        path_kind="history_file",
        source_kind="jsonl",
        search_root=pathlib.Path("/home/example/.codex"),
        mtime_ns=0,
    )


def _reporter(stream: io.StringIO) -> ConsoleSearchProgress:
    """Return one scanning-phase reporter writing plain text to ``stream``."""
    progress = ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="never",
        answer_now_hint=True,
    )
    progress.start(_query())
    progress.source_started(7, 19, _source())
    return progress


def _last_line(stream: io.StringIO) -> str:
    """Return the final non-empty line written to ``stream``."""
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines, "reporter wrote nothing"
    return lines[-1]


class LateCallbackCase(t.NamedTuple):
    """One progress callback a draining source can still deliver after the request.

    Attributes
    ----------
    test_id : str
        Parametrize id naming the callback under test.
    call : collections.abc.Callable
        Invokes the callback on the reporter passed to it.
    """

    test_id: str
    call: cabc.Callable[[ConsoleSearchProgress], None]


LATE_CALLBACKS = (
    LateCallbackCase(
        test_id="source_started",
        call=lambda p: p.source_started(8, 19, _source()),
    ),
    LateCallbackCase(
        test_id="source_finished",
        call=lambda p: p.source_finished(8, 19, _source(), 120, 3),
    ),
    LateCallbackCase(
        test_id="source_progress",
        call=lambda p: p.source_progress(8, 19, _source(), 256, 4),
    ),
    LateCallbackCase(
        test_id="set_status",
        call=lambda p: p.set_status("scanning", current=8, total=19, detail="late"),
    ),
)


@pytest.mark.parametrize(
    "case",
    LATE_CALLBACKS,
    ids=[case.test_id for case in LATE_CALLBACKS],
)
def test_answering_phase_survives_late_source_callbacks(case: LateCallbackCase) -> None:
    """A source finishing after the request must not revert the phase to scanning."""
    stream = io.StringIO()
    progress = _reporter(stream)
    progress.answer_now_pending()
    case.call(progress)
    progress.interrupt()
    line = _last_line(stream)
    assert "answering" in line
    assert "scanning" not in line


def test_scanning_phase_is_shown_until_the_request_lands() -> None:
    """The reporter keeps reporting scanning while no request has been made."""
    stream = io.StringIO()
    progress = _reporter(stream)
    progress.source_finished(8, 19, _source(), 120, 3)
    progress.interrupt()
    line = _last_line(stream)
    assert "scanning" in line
    assert "answering" not in line


def test_request_retires_the_answer_now_reminder() -> None:
    """The stale reminder goes away, so a second press is never invited."""
    stream = io.StringIO()
    progress = _reporter(stream)
    progress.interrupt()
    assert "[Press enter, answer now]" in _last_line(stream)
    progress.answer_now_pending()
    progress.source_finished(8, 19, _source(), 120, 3)
    progress.interrupt()
    last = _last_line(stream)
    assert "[Press enter, answer now]" not in last
    assert "[↵ answer]" not in last


def test_a_request_during_discovery_keeps_the_cancel_reminder() -> None:
    """Nothing is read yet, so discovery must not claim to be answering.

    The reminder drawn during discovery is about Ctrl-C, which stays true after
    a keypress the engine cannot honour with anything yet.
    """
    stream = io.StringIO()
    progress = ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="never",
        answer_now_hint=True,
    )
    progress.start(_query())
    progress.answer_now_pending()
    progress.set_status("discovering", current=None, total=None, detail="codex")
    progress.interrupt()
    line = _last_line(stream)
    assert "discovering" in line
    assert "answering" not in line
    assert "[Ctrl-C to cancel]" in line


def test_a_request_latched_before_start_is_still_acknowledged() -> None:
    """A newline already queued in the tty buffer must not be thrown away.

    The listener thread runs before the engine calls
    :meth:`ConsoleSearchProgress.start`, so typeahead is latched first.
    """
    stream = io.StringIO()
    progress = ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="never",
        answer_now_hint=True,
    )
    progress.answer_now_pending()
    progress.start(_query())
    progress.set_status("scanning", current=3, total=19, detail=None)
    progress.interrupt()
    line = _last_line(stream)
    assert "answering" in line
    assert "scanning" not in line


def test_input_listener_publishes_the_request_to_its_subscriber() -> None:
    """A blank Enter both requests the answer and notifies the reporter."""
    control = SearchControl()
    observed = threading.Event()
    listener = AnswerNowInputListener(
        control,
        stream=io.StringIO("\n"),
        on_request=observed.set,
    )
    listener.start()
    try:
        # Generous expiry treated as failure: the publication is the signal.
        assert observed.wait(timeout=10.0)
    finally:
        listener.stop()
    assert control.answer_now_requested()
    assert control.stop_reason() == "answer_now"


def test_input_listener_stays_silent_without_a_request() -> None:
    """Closed input ends the listener without publishing anything."""
    control = SearchControl()
    published: list[bool] = []
    listener = AnswerNowInputListener(
        control,
        stream=io.StringIO(""),
        on_request=lambda: published.append(True),
    )
    listener.start()
    listener.stop()
    assert published == []
    assert not control.answer_now_requested()


def test_cli_wires_the_keypress_through_to_the_progress_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped pair must acknowledge, not just the reporter in isolation.

    Severing the subscription is invisible to every reporter-only test, so this
    drives the real ``agentgrep search`` wiring: a blank line on stdin has to
    reach the progress line the CLI would draw.
    """
    acknowledged = threading.Event()
    pin = ConsoleSearchProgress.answer_now_pending

    def spy(self: ConsoleSearchProgress) -> None:
        pin(self)
        acknowledged.set()

    monkeypatch.setattr(ConsoleSearchProgress, "answer_now_pending", spy)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    stream = io.StringIO()
    control = SearchControl()
    progress, listener = _build_search_feedback(
        control,
        color_mode="never",
        progress_enabled=True,
        answer_now_enabled=True,
    )
    assert listener is not None
    assert isinstance(progress, ConsoleSearchProgress)
    monkeypatch.setattr(progress, "_stream", stream)
    monkeypatch.setattr(progress, "_tty", False)
    progress.start(_query())
    progress.set_status("scanning", current=7, total=19, detail=None)
    listener.start()
    try:
        # Generous expiry treated as failure: the reporter's own pin is the
        # published signal that the keypress reached it.
        assert acknowledged.wait(timeout=10.0)
    finally:
        listener.stop()
    assert control.answer_now_requested()
    progress.set_status("scanning", current=8, total=19, detail=None)
    progress.interrupt()
    line = _last_line(stream)
    assert "answering" in line
    assert "scanning" not in line


def test_feedback_pair_is_silent_without_progress() -> None:
    """No progress line means no reporter and no listener."""
    progress, listener = _build_search_feedback(
        SearchControl(),
        color_mode="never",
        progress_enabled=False,
        answer_now_enabled=False,
    )
    assert listener is None
    assert isinstance(progress, NoopSearchProgress)


def test_feedback_pair_omits_the_listener_when_input_is_not_interactive() -> None:
    """A non-tty stdin gets a progress line but nothing listening on it."""
    progress, listener = _build_search_feedback(
        SearchControl(),
        color_mode="never",
        progress_enabled=True,
        answer_now_enabled=False,
    )
    assert listener is None
    assert isinstance(progress, ConsoleSearchProgress)
