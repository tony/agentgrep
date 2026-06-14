"""Tests for the ``agentgrep insights`` CLI surface and dispatchers."""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.cli import insights_render
from agentgrep.cli.insights_render import (
    run_insights_cache_command,
    run_insights_levels_command,
    run_insights_report_command,
    run_insights_setup_command,
    run_insights_skills_command,
)
from agentgrep.cli.parser import (
    InsightsCacheArgs,
    InsightsLevelsArgs,
    InsightsModelsArgs,
    InsightsReportArgs,
    InsightsSkillsArgs,
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


def test_parse_skills_args() -> None:
    """``insights skills`` parses into InsightsSkillsArgs with the opt-in flags."""
    parsed = parse_args(["insights", "skills", "--llm", "--write", "out", "--since", "30d"])
    assert isinstance(parsed, InsightsSkillsArgs)
    assert parsed.use_llm is True
    assert parsed.write_dir == "out"
    assert parsed.since == "30d"
    assert parsed.scope == "conversations"


_SKILL_SUGGESTION = {
    "type": "template",
    "name": "vcspull-commit",
    "evidence": "3 similar asks across 2 conversations",
    "rationale": "A parameterized skill for this recurring request.",
    "support": 3,
    "terms": ["vcspull", "commit"],
    "examples": ["read .vcspull.yaml changes and commit"],
}


# --- dispatch --------------------------------------------------------------


def test_report_command_emits_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report dispatcher renders JSON from the collected records."""
    records = [_rec("Configure tantivy"), _rec("Add sqlite-vec index")]
    monkeypatch.setattr(agentgrep, "run_search_query", lambda home, query, **kwargs: records)

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
    monkeypatch.setattr(agentgrep, "run_search_query", lambda home, query, **kwargs: [])
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


def _skills_args(**overrides: t.Any) -> InsightsSkillsArgs:
    """Build InsightsSkillsArgs with test defaults."""
    base = {
        "output_format": "text",
        "scope": "conversations",
        "agents": (),
        "limit": 500,
        "model": None,
        "llm_backend": "ollama",
        "use_llm": False,
        "write_dir": None,
        "allow_download": False,
        "yes": False,
        "color_mode": "never",
        "progress_mode": "never",
    }
    base.update(overrides)
    return InsightsSkillsArgs(**t.cast("t.Any", base))


def _patch_one_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub record collection, the report build, and the graph suggestions."""
    monkeypatch.setattr(agentgrep, "run_search_query", lambda home, query, **kwargs: [_rec("x")])
    monkeypatch.setattr(agentgrep.insights, "build_report", lambda *a, **k: None)
    monkeypatch.setattr(
        insights_render, "_graph_skill_suggestions", lambda report: [_SKILL_SUGGESTION]
    )


def test_skills_command_prints_skill_md(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skills dispatcher renders a SKILL.md from a graph suggestion."""
    _patch_one_suggestion(monkeypatch)

    assert run_insights_skills_command(_skills_args()) == 0
    out = capsys.readouterr().out
    assert "name: vcspull-commit" in out
    assert "## Example requests" in out


def test_skills_command_writes_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """``--write DIR`` writes one SKILL.md per suggestion under DIR."""
    _patch_one_suggestion(monkeypatch)

    assert run_insights_skills_command(_skills_args(write_dir=str(tmp_path))) == 0
    written = tmp_path / "vcspull-commit" / "SKILL.md"
    assert written.is_file()
    assert "name: vcspull-commit" in written.read_text(encoding="utf-8")


def test_skills_command_returns_one_without_suggestions(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No recurring-request suggestions exits non-zero with guidance."""
    monkeypatch.setattr(agentgrep, "run_search_query", lambda home, query, **kwargs: [_rec("x")])
    monkeypatch.setattr(agentgrep.insights, "build_report", lambda *a, **k: None)
    monkeypatch.setattr(insights_render, "_graph_skill_suggestions", lambda report: [])

    assert run_insights_skills_command(_skills_args()) == 1
    assert "no recurring-request skill suggestions" in capsys.readouterr().err


def test_build_skill_namer_wires_transformers_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``skills --llm --backend transformers`` builds a real namer, not a silent fallback."""
    from agentgrep.insights import models as models_mod, skills as skills_mod

    def _stub_complete(prompt: str) -> str:
        return '{"name": "x", "description": "Use when y"}'

    monkeypatch.setattr(models_mod, "is_installed", lambda spec, *a, **k: True)
    monkeypatch.setattr(skills_mod, "build_transformers_complete", lambda **kwargs: _stub_complete)

    namer = insights_render._build_skill_namer(
        _skills_args(llm_backend="transformers", use_llm=True)
    )
    assert namer is _stub_complete


def test_parse_models_list_reranker_level() -> None:
    """``models list --level reranker`` parses; the reranker kind is exposed on the CLI."""
    parsed = parse_args(["insights", "models", "list", "--level", "reranker"])
    assert isinstance(parsed, InsightsModelsArgs)
    assert parsed.kind == "reranker"


def test_models_listing_includes_reranker(capsys: pytest.CaptureFixture[str]) -> None:
    """``models available --level reranker --format json`` lists the curated cross-encoder."""
    args = parse_args(
        ["insights", "models", "available", "--level", "reranker", "--format", "json"]
    )
    assert isinstance(args, InsightsModelsArgs)
    assert insights_render.run_insights_models_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(row["kind"] == "reranker" for row in payload)
