"""Tests for the ``agentgrep insights`` CLI surface and dispatchers."""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.cli.insights_render import (
    run_insights_cache_command,
    run_insights_levels_command,
    run_insights_report_command,
    run_insights_setup_command,
)
from agentgrep.cli.parser import (
    InsightsCacheArgs,
    InsightsLevelsArgs,
    InsightsModelsArgs,
    InsightsReportArgs,
    parse_args,
)


def _rec(text: str) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for CLI tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="claude",
        store="proj",
        adapter_id="adapter.v1",
        path=pathlib.Path("/x/proj/file.jsonl"),
        text=text,
        timestamp="2026-06-10T10:00:00Z",
        session_id="s1",
    )


# --- parsing ---------------------------------------------------------------


def test_parse_report_args() -> None:
    """``insights report`` parses into a fully populated InsightsReportArgs."""
    parsed = parse_args(
        [
            "insights",
            "report",
            "--level",
            "embeddings",
            "--format",
            "json",
            "--limit",
            "10",
            "--agent",
            "claude",
        ]
    )
    assert isinstance(parsed, InsightsReportArgs)
    assert parsed.requested_level == "embeddings"
    assert parsed.output_format == "json"
    assert parsed.limit == 10
    assert parsed.agents == ("claude",)


def test_parse_levels_args() -> None:
    """``insights levels`` parses into InsightsLevelsArgs."""
    parsed = parse_args(["insights", "levels", "--format", "json"])
    assert isinstance(parsed, InsightsLevelsArgs)
    assert parsed.output_format == "json"


def test_parse_models_install_args() -> None:
    """``insights models install`` parses action, kind, and dry-run."""
    parsed = parse_args(
        ["insights", "models", "install", "potion-base-8M", "--level", "embeddings", "--dry-run"]
    )
    assert isinstance(parsed, InsightsModelsArgs)
    assert parsed.action == "install"
    assert parsed.kind == "embeddings"
    assert parsed.model == "potion-base-8M"
    assert parsed.dry_run is True


def test_parse_cache_prune_args() -> None:
    """``insights cache prune --dry-run`` parses into InsightsCacheArgs."""
    parsed = parse_args(["insights", "cache", "prune", "--dry-run"])
    assert isinstance(parsed, InsightsCacheArgs)
    assert parsed.action == "prune"
    assert parsed.dry_run is True


# --- dispatch --------------------------------------------------------------


def test_report_command_emits_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report dispatcher renders JSON from the collected records."""
    records = [_rec("Configure tantivy"), _rec("Add sqlite-vec index")]
    monkeypatch.setattr(
        "agentgrep._engine.orchestration.run_search_query",
        lambda home, query, **kwargs: records,
    )

    args = t.cast(
        "InsightsReportArgs",
        parse_args(["insights", "report", "--format", "json", "--no-progress"]),
    )
    code = run_insights_report_command(args)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["records_analyzed"] == 2
    assert payload["schema_version"] == 1


def test_report_command_returns_one_when_empty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty record set exits non-zero (no matches)."""
    monkeypatch.setattr(
        "agentgrep._engine.orchestration.run_search_query", lambda home, query, **kwargs: []
    )
    args = t.cast(
        "InsightsReportArgs",
        parse_args(["insights", "report", "--format", "json", "--no-progress"]),
    )
    assert run_insights_report_command(args) == 1
    _ = capsys.readouterr()


def test_levels_command_lists_builtin(capsys: pytest.CaptureFixture[str]) -> None:
    """``insights levels`` always lists builtin as available."""
    args = InsightsLevelsArgs(output_format="text", color_mode="never")
    assert run_insights_levels_command(args) == 0
    out = capsys.readouterr().out
    assert "builtin" in out


def test_setup_command_prints_install_for_unavailable_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``insights setup <level>`` prints the install command for a missing level."""
    import agentgrep.insights as insights_pkg
    from agentgrep.cli.parser import InsightsSetupArgs
    from agentgrep.insights.model import InsightsLevelStatus

    # Force a deterministic "ml unavailable" view via the package probe seam.
    monkeypatch.setattr(
        insights_pkg,
        "probe_levels",
        lambda request, import_module=None: (
            InsightsLevelStatus(
                level="ml",
                available=False,
                backend=None,
                reason="missing: sklearn",
                setup_command="uv pip install 'agentgrep[insights-ml]'",
            ),
        ),
    )
    args = InsightsSetupArgs(level="ml", color_mode="never")
    assert run_insights_setup_command(args) == 0
    assert "insights-ml" in capsys.readouterr().out


def test_cache_dir_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """``insights cache dir`` reports the resolved cache directories."""
    monkeypatch.setenv("AGENTGREP_CACHE_DIR", str(tmp_path))
    args = InsightsCacheArgs(action="dir", dry_run=False, output_format="text", color_mode="never")
    assert run_insights_cache_command(args) == 0
    out = capsys.readouterr().out
    assert str(tmp_path) in out
