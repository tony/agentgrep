"""Tests for stream_search_results and run_search_command routing.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import io
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.cli.parser import SearchArgs
from agentgrep.cli.render import stream_search_results


def _make_search_args(**overrides: t.Any) -> SearchArgs:
    defaults: dict[str, t.Any] = {
        "terms": ("streaming",),
        "agents": ("codex", "claude", "cursor", "gemini"),
        "search_type": "prompts",
        "any_term": False,
        "regex": False,
        "case_sensitive": False,
        "limit": None,
        "output_mode": "text",
        "color_mode": "never",
        "progress_mode": "never",
    }
    defaults.update(overrides)
    return SearchArgs(**defaults)


def _make_record(
    *,
    agent: agentgrep.AgentName = "claude",
    text: str = "streaming parser for JSONL",
    timestamp: str | None = "2026-05-22T14:30:00Z",
) -> agentgrep.SearchRecord:
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=agent,
        store=f"{agent}.sessions",
        adapter_id=f"{agent}.sessions_jsonl.v1",
        path=pathlib.Path(f"/home/user/.{agent}/sessions/abc.jsonl"),
        text=text,
        timestamp=timestamp,
    )


def _make_events(
    records: list[agentgrep.SearchRecord],
    *,
    elapsed: float = 0.42,
) -> list[t.Any]:
    """Build a canned event sequence for monkeypatching."""
    from agentgrep import events

    evts: list[t.Any] = [events.SearchStarted(source_count=1)]
    evts.append(
        events.SourceStarted(adapter_id="test.v1", index=0, total=1),
    )
    evts.extend(events.RecordEmitted(record=record) for record in records)
    evts.append(
        events.SourceFinished(
            adapter_id="test.v1",
            records_seen=len(records),
            matches_seen=len(records),
        ),
    )
    evts.append(
        events.SearchFinished(
            match_count=len(records),
            elapsed_seconds=elapsed,
        ),
    )
    return evts


def test_streams_records_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Streaming search prints snippet-first records to stdout."""
    records = [_make_record(), _make_record(agent="codex")]
    events = _make_events(records)
    monkeypatch.setattr(
        agentgrep,
        "iter_search_events",
        lambda *a, **kw: iter(events),
    )
    monkeypatch.setattr("sys.stdout", io.StringIO())

    args = _make_search_args()
    exit_code = stream_search_results(args)

    stdout = t.cast("io.StringIO", __import__("sys").stdout).getvalue()
    assert "streaming parser for JSONL" in stdout
    assert exit_code == 0


def test_no_matches_prints_stderr_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero matches prints 'No matches found.' to stderr."""
    events = _make_events([], elapsed=0.1)
    monkeypatch.setattr(
        agentgrep,
        "iter_search_events",
        lambda *a, **kw: iter(events),
    )
    args = _make_search_args()
    exit_code = stream_search_results(args)

    captured = capsys.readouterr()
    assert "No matches found." in captured.err
    assert exit_code == 1


def test_has_matches_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At least one match returns exit code 0."""
    records = [_make_record()]
    events = _make_events(records)
    monkeypatch.setattr(
        agentgrep,
        "iter_search_events",
        lambda *a, **kw: iter(events),
    )
    monkeypatch.setattr("sys.stdout", io.StringIO())

    args = _make_search_args()
    assert stream_search_results(args) == 0


# ---------------------------------------------------------------------------
# _search_path_is_eager routing
# ---------------------------------------------------------------------------


class EagerCase(t.NamedTuple):
    """Parametrized case for eager-path detection."""

    test_id: str
    output_mode: str
    expected_eager: bool


_EAGER_CASES: tuple[EagerCase, ...] = (
    EagerCase(test_id="json-is-eager", output_mode="json", expected_eager=True),
    EagerCase(test_id="ndjson-is-eager", output_mode="ndjson", expected_eager=True),
    EagerCase(test_id="text-is-streaming", output_mode="text", expected_eager=False),
)


@pytest.mark.parametrize("case", _EAGER_CASES, ids=[c.test_id for c in _EAGER_CASES])
def test_search_path_is_eager(case: EagerCase) -> None:
    """Eager-path detection matches expected output modes."""
    from agentgrep.cli.render import _search_path_is_eager

    args = _make_search_args(output_mode=case.output_mode)
    assert _search_path_is_eager(args) == case.expected_eager


def test_run_search_command_routes_text_through_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_search_command routes text mode through stream_search_results."""
    from agentgrep.cli import render

    called_with: list[SearchArgs] = []

    def fake_stream(args: SearchArgs) -> int:
        called_with.append(args)
        return 0

    monkeypatch.setattr(render, "stream_search_results", fake_stream)
    args = _make_search_args(output_mode="text")
    render.run_search_command(args)
    assert len(called_with) == 1


def test_run_search_command_json_uses_eager_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_search_command routes json mode through the eager path."""
    records = [_make_record()]
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *a, **kw: records,
    )
    monkeypatch.setattr(
        agentgrep,
        "should_enable_answer_now",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        agentgrep,
        "build_search_progress",
        lambda *a, **kw: agentgrep.NoopSearchProgress(),
    )
    monkeypatch.setattr("sys.stdout", io.StringIO())

    args = _make_search_args(output_mode="json")
    from agentgrep.cli import render

    exit_code = render.run_search_command(args)
    stdout = t.cast("io.StringIO", __import__("sys").stdout).getvalue()
    assert '"command": "search"' in stdout
    assert exit_code == 0
