"""Tests for the report orchestrator (level resolution, status, payload)."""

from __future__ import annotations

import json
import pathlib
import typing as t

import agentgrep
from agentgrep.insights import build_report
from agentgrep.insights.model import ReportRequest


def _rec(
    text: str, *, session_id: str | None = None, timestamp: str | None = None
) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for report tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="claude",
        store="proj",
        adapter_id="adapter.v1",
        path=pathlib.Path("/x/proj/file.jsonl"),
        text=text,
        timestamp=timestamp,
        session_id=session_id,
    )


def _none_importer(name: str) -> t.Any:
    """Raise ImportError for every module so no optional backend is available."""
    raise ImportError(name)


_RECORDS = [
    _rec("Configure tantivy parser", session_id="s1", timestamp="2026-06-10T10:00:00Z"),
    _rec("Add a sqlite-vec index", session_id="s2", timestamp="2026-06-11T10:00:00Z"),
]


def test_builtin_report_is_ok_and_counts_records() -> None:
    """The builtin report runs with status ok and populated counters."""
    report = build_report(
        _RECORDS, ReportRequest(requested_level="builtin"), import_module=_none_importer
    )
    assert report.status == "ok"
    assert report.level == "builtin"
    assert report.records_analyzed == 2
    assert report.agents == {"claude": 2}
    assert report.earliest_timestamp == "2026-06-10T10:00:00Z"
    assert report.latest_timestamp == "2026-06-11T10:00:00Z"


def test_report_payload_is_json_serializable_with_schema_version() -> None:
    """``to_payload`` returns a JSON-serializable dict carrying schema_version."""
    report = build_report(_RECORDS, ReportRequest(), import_module=_none_importer)
    payload = report.to_payload()
    assert payload["schema_version"] == 1
    encoded = json.dumps(payload)  # must not raise
    assert "activity" in json.loads(encoded)


def test_empty_records_yield_empty_status() -> None:
    """No records produces the ``empty`` status, not an error."""
    report = build_report([], ReportRequest(), import_module=_none_importer)
    assert report.status == "empty"
    assert report.records_analyzed == 0


def test_unavailable_level_falls_back_to_builtin_with_diagnostic() -> None:
    """Requesting an uninstalled level degrades to builtin and explains how to fix it."""
    report = build_report(
        _RECORDS, ReportRequest(requested_level="ml"), import_module=_none_importer
    )
    assert report.level == "builtin"
    assert report.status == "partial"
    codes = {diag.code for diag in report.diagnostics}
    assert "level-unavailable" in codes
    setup = next(d.setup_command for d in report.diagnostics if d.code == "level-unavailable")
    assert setup is not None and "insights-ml" in setup


def test_best_installed_picks_builtin_when_nothing_installed() -> None:
    """``best-installed`` selects builtin (no fallback) when no extras are present."""
    report = build_report(
        _RECORDS, ReportRequest(requested_level="best-installed"), import_module=_none_importer
    )
    assert report.level == "builtin"
    assert report.status == "ok"


def test_sampled_flag_set_when_limit_reached() -> None:
    """The sampled flag trips when the record count meets the limit."""
    report = build_report(_RECORDS, ReportRequest(record_limit=2), import_module=_none_importer)
    assert report.sampled is True


def test_levels_field_lists_every_rung() -> None:
    """The report enumerates every level for the levels/doctor surfaces."""
    report = build_report(_RECORDS, ReportRequest(), import_module=_none_importer)
    listed = {status.level for status in report.levels}
    assert listed == {"builtin", "html", "ml", "embeddings", "index", "graph", "llm"}
    builtin = next(s for s in report.levels if s.level == "builtin")
    assert builtin.available is True
