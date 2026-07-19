"""Tests for the search-input history persistence layer (``ui/_history.py``).

The persistence layer is deliberately Textual-free: it reads and writes a small
JSONL file under the user's XDG state dir. These tests exercise the on-disk
contract (path resolution, append, dedup, cap, corruption tolerance, opt-out)
with ``tmp_path`` and ``monkeypatch`` — no app or Pilot required.
"""

from __future__ import annotations

import json
import stat
import typing as t

import pytest

from agentgrep.ui import _history

pytestmark = pytest.mark.tui

if t.TYPE_CHECKING:
    import pathlib


def test_history_path_uses_xdg_state_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``XDG_STATE_HOME`` wins over the home fallback."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = _history.history_path(tmp_path / "home")
    assert path == tmp_path / "state" / "agentgrep" / "history.jsonl"


def test_history_path_falls_back_to_home_local_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``XDG_STATE_HOME`` the path is ``<home>/.local/state/agentgrep``."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    path = _history.history_path(tmp_path)
    assert path == tmp_path / ".local" / "state" / "agentgrep" / "history.jsonl"


def test_append_and_load_roundtrip(tmp_path: pathlib.Path) -> None:
    """Appended queries load back newest-first with their timestamp and scope."""
    path = tmp_path / "h.jsonl"
    assert _history.append_query(path, "tmux pane", scope="prompts", now=100) is True
    assert _history.append_query(path, "agent:codex", scope="conversations", now=200) is True
    entries = _history.load_history(path)
    assert [e.text for e in entries] == ["agent:codex", "tmux pane"]
    assert entries[0].ts == 200
    assert entries[0].scope == "conversations"


def test_append_skips_empty_and_consecutive_dup(tmp_path: pathlib.Path) -> None:
    """Empty/whitespace queries and a consecutive duplicate are not recorded."""
    path = tmp_path / "h.jsonl"
    assert _history.append_query(path, "   ", now=1) is False
    assert _history.append_query(path, "x", now=2) is True
    assert _history.append_query(path, "x", now=3, dedup_last="x") is False
    assert [e.text for e in _history.load_history(path)] == ["x"]


def test_load_dedups_by_text_keeping_newest(tmp_path: pathlib.Path) -> None:
    """A non-consecutive repeat collapses to one row at its newest position."""
    path = tmp_path / "h.jsonl"
    _history.append_query(path, "a", now=1)
    _history.append_query(path, "b", now=2)
    _history.append_query(path, "a", now=3, dedup_last="b")
    entries = _history.load_history(path)
    assert [e.text for e in entries] == ["a", "b"]
    assert entries[0].ts == 3


class HistoryOrderCase(t.NamedTuple):
    """Physical append order ``(text, ts)`` and the expected newest-first text."""

    test_id: str
    writes: tuple[tuple[str, float], ...]
    expected: tuple[str, ...]


HISTORY_ORDER_CASES = (
    HistoryOrderCase(
        test_id="out-of-order-write",
        writes=(("newer", 200.0), ("older", 100.0)),
        expected=("newer", "older"),
    ),
    HistoryOrderCase(
        test_id="subsecond-race",
        writes=(("late", 100.6), ("early", 100.2)),
        expected=("late", "early"),
    ),
)


@pytest.mark.parametrize(
    "case",
    HISTORY_ORDER_CASES,
    ids=[case.test_id for case in HISTORY_ORDER_CASES],
)
def test_load_history_orders_by_submit_time_not_write_order(
    case: HistoryOrderCase,
    tmp_path: pathlib.Path,
) -> None:
    """Recency follows submit-time ts even when appends land out of order."""
    path = tmp_path / "h.jsonl"
    prev = ""
    for text, ts in case.writes:
        assert _history.append_query(path, text, now=ts, dedup_last=prev) is True
        prev = text
    assert tuple(e.text for e in _history.load_history(path)) == case.expected


def test_load_tolerates_corruption(tmp_path: pathlib.Path) -> None:
    """A half-written or foreign line is skipped, not fatal."""
    path = tmp_path / "h.jsonl"
    path.write_text(
        '{"text": "ok", "ts": 5}\nnot json at all\n{"text": "good", "ts": 6}\n',
        encoding="utf-8",
    )
    entries = _history.load_history(path)
    assert [e.text for e in entries] == ["good", "ok"]


def test_load_bounds_foreign_history_text(tmp_path: pathlib.Path) -> None:
    """A legacy or hand-written row cannot inject an oversized modal entry."""
    path = tmp_path / "h.jsonl"
    path.write_text(
        json.dumps({"text": "x" * 10_000, "ts": 1}) + "\n",
        encoding="utf-8",
    )
    [entry] = _history.load_history(path)
    assert len(entry.text) == _history.QUERY_TEXT_MAX_CHARS


class CorruptTimestampCase(t.NamedTuple):
    """History timestamp token accepted by JSON but not a finite number."""

    test_id: str
    raw_ts: str


CORRUPT_TIMESTAMP_CASES = (
    CorruptTimestampCase(test_id="nan", raw_ts="NaN"),
    CorruptTimestampCase(test_id="positive-infinity", raw_ts="Infinity"),
    CorruptTimestampCase(test_id="negative-infinity", raw_ts="-Infinity"),
)


@pytest.mark.parametrize(
    "case",
    CORRUPT_TIMESTAMP_CASES,
    ids=[case.test_id for case in CORRUPT_TIMESTAMP_CASES],
)
def test_load_tolerates_non_finite_timestamp(
    case: CorruptTimestampCase,
    tmp_path: pathlib.Path,
) -> None:
    """A non-finite numeric timestamp falls back to zero, not startup failure."""
    path = tmp_path / "h.jsonl"
    path.write_text(
        f'{{"text": "bad", "ts": {case.raw_ts}}}\n{{"text": "ok", "ts": 5}}\n',
        encoding="utf-8",
    )
    entries = _history.load_history(path)
    by_text = {entry.text: entry for entry in entries}
    assert [entry.text for entry in entries] == ["ok", "bad"]
    assert by_text["ok"].ts == 5
    assert by_text["bad"].ts == 0


def test_load_missing_file_is_empty(tmp_path: pathlib.Path) -> None:
    """A missing history file loads as an empty list, never raises."""
    assert _history.load_history(tmp_path / "nope.jsonl") == []


def test_load_caps_displayed_rows(tmp_path: pathlib.Path) -> None:
    """The displayed list is capped to ``limit`` newest rows."""
    path = tmp_path / "h.jsonl"
    for i in range(10):
        _history.append_query(path, f"q{i}", now=i)
    entries = _history.load_history(path, limit=3)
    assert [e.text for e in entries] == ["q9", "q8", "q7"]


def test_append_creates_file_mode_0o600(tmp_path: pathlib.Path) -> None:
    """The history file holds the user's queries — created private (0o600)."""
    path = tmp_path / "sub" / "h.jsonl"
    _history.append_query(path, "x", now=1)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_trims_disk_when_over_cap(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading rewrites the file down to the disk cap when it overgrows."""
    monkeypatch.setattr(_history, "DISK_CAP", 5)
    path = tmp_path / "h.jsonl"
    for i in range(12):
        _history.append_query(path, f"q{i}", now=i)
    _history.load_history(path)
    raw = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(raw) <= 5


def test_history_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGENTGREP_NO_HISTORY`` opt-out is truthy for a real value only."""
    monkeypatch.setenv("AGENTGREP_NO_HISTORY", "1")
    assert _history.history_disabled() is True
    monkeypatch.setenv("AGENTGREP_NO_HISTORY", "0")
    assert _history.history_disabled() is False
    monkeypatch.delenv("AGENTGREP_NO_HISTORY", raising=False)
    assert _history.history_disabled() is False
