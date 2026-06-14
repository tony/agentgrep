"""Tests for the deterministic builtin (level 0) activity analysis."""

from __future__ import annotations

import pathlib
import typing as t

import agentgrep
from agentgrep.insights.activity import build_activity


def _rec(
    text: str,
    *,
    agent: str = "claude",
    store: str = "proj",
    timestamp: str | None = None,
    session_id: str | None = None,
    kind: t.Literal["prompt", "history"] = "prompt",
    path: str = "/x/proj/file.jsonl",
) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for activity tests."""
    return agentgrep.SearchRecord(
        kind=kind,
        agent=t.cast("t.Any", agent),
        store=store,
        adapter_id="adapter.v1",
        path=pathlib.Path(path),
        text=text,
        timestamp=timestamp,
        session_id=session_id,
    )


def test_empty_records_produce_empty_summary() -> None:
    """An empty record set yields a no-records summary and zero counts."""
    activity = build_activity([], sampled=False)
    assert activity.records_analyzed == 0
    assert activity.activity_units == 0
    assert "No records" in activity.summary
    assert activity.work_areas == ()
    assert activity.timeline == ()


def test_top_terms_drop_stopwords_and_count_content() -> None:
    """Frequent content terms surface; stopwords and short tokens are dropped."""
    records = [
        _rec("Configure the tantivy parser for the project"),
        _rec("The tantivy parser needs a schema"),
        _rec("Add a sqlite-vec vector index"),
    ]
    activity = build_activity(records, sampled=False)
    terms = {term.term: term.count for term in activity.recurring_patterns}
    assert terms.get("tantivy") == 2
    assert terms.get("parser") == 2
    assert "the" not in terms  # stopword
    assert "for" not in terms  # short / stopword


def test_timeline_buckets_by_day() -> None:
    """Records bucket into daily timeline entries sorted by date."""
    records = [
        _rec("alpha indexing", timestamp="2026-06-10T10:00:00Z"),
        _rec("beta indexing", timestamp="2026-06-10T18:00:00Z"),
        _rec("gamma vectors", timestamp="2026-06-12T09:00:00Z"),
    ]
    activity = build_activity(records, sampled=False)
    dates = {bucket.date: bucket.record_count for bucket in activity.timeline}
    assert dates == {"2026-06-10": 2, "2026-06-12": 1}


def test_work_areas_group_by_session() -> None:
    """Work areas are inferred from session identity, largest first."""
    records = [
        _rec("alpha", session_id="s1"),
        _rec("beta", session_id="s1"),
        _rec("gamma", session_id="s2"),
    ]
    activity = build_activity(records, sampled=False)
    assert activity.activity_units == 2
    assert activity.work_areas[0].record_count == 2
    assert activity.work_areas[0].label.startswith("session s1")


def test_repeated_instructions_detected() -> None:
    """Identical first lines occurring 2+ times are flagged as repeated."""
    records = [
        _rec("commit your files"),
        _rec("commit your files"),
        _rec("a unique instruction"),
    ]
    activity = build_activity(records, sampled=False)
    assert "commit your files" in activity.repeated_instructions
    assert "a unique instruction" not in activity.repeated_instructions


def test_open_threads_flag_trailing_questions() -> None:
    """Prompts ending in a question mark become open-thread candidates."""
    records = [
        _rec("Why does the parser fail?", session_id="s1"),
        _rec("Refactor the builder", session_id="s2"),
    ]
    activity = build_activity(records, sampled=False)
    titles = [thread.title for thread in activity.open_threads]
    assert any("Why does the parser fail" in title for title in titles)
    assert all("Refactor" not in title for title in titles)
    assert activity.open_threads[0].ref.session_id == "s1"


def test_coverage_counts_timestamp_and_session() -> None:
    """Coverage reflects how many records carry timestamps and session ids."""
    records = [
        _rec("with both", timestamp="2026-06-10T10:00:00Z", session_id="s1"),
        _rec("bare"),
    ]
    activity = build_activity(records, sampled=False)
    assert activity.coverage.records_with_timestamp == 1
    assert activity.coverage.records_with_session_identity == 1
