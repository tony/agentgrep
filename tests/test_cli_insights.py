"""Tests for the ``agentgrep insights`` concept commands."""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys
import time
import types
import typing as t

import pytest

import agentgrep
import agentgrep.insights as insights
from agentgrep.cli import render as cli_render


class InsightsParseCase(t.NamedTuple):
    """Parametrized parse case for ``agentgrep insights report``."""

    test_id: str
    argv: tuple[str, ...]
    expected_scope: agentgrep.SearchScope
    expected_level: str
    expected_limit: int | None
    expected_all_records: bool
    expected_report_format: str
    expected_output_path: pathlib.Path | None


INSIGHTS_PARSE_CASES: tuple[InsightsParseCase, ...] = (
    InsightsParseCase(
        test_id="report-defaults-bounded-builtin",
        argv=("insights", "report"),
        expected_scope="prompts",
        expected_level="builtin",
        expected_limit=500,
        expected_all_records=False,
        expected_report_format="text",
        expected_output_path=None,
    ),
    InsightsParseCase(
        test_id="report-all-removes-bound",
        argv=("insights", "report", "--all"),
        expected_scope="prompts",
        expected_level="builtin",
        expected_limit=None,
        expected_all_records=True,
        expected_report_format="text",
        expected_output_path=None,
    ),
    InsightsParseCase(
        test_id="report-best-installed-level",
        argv=("insights", "report", "--scope", "all", "--level", "best-installed"),
        expected_scope="all",
        expected_level="best-installed",
        expected_limit=500,
        expected_all_records=False,
        expected_report_format="text",
        expected_output_path=None,
    ),
    InsightsParseCase(
        test_id="report-html-output-options",
        argv=(
            "insights",
            "report",
            "--level",
            "html",
            "--format",
            "html",
            "--output",
            "report.html",
        ),
        expected_scope="prompts",
        expected_level="html",
        expected_limit=500,
        expected_all_records=False,
        expected_report_format="html",
        expected_output_path=pathlib.Path("report.html"),
    ),
)


class InsightsSetupParseCase(t.NamedTuple):
    """Parametrized parse case for ``agentgrep insights setup``."""

    test_id: str
    argv: tuple[str, ...]
    expected_level: str
    expected_manager: str
    expected_install: bool
    expected_yes: bool


class RuntimeConfigCase(t.NamedTuple):
    """One installed backend with missing runtime configuration."""

    test_id: str
    level: insights.InsightsLevel
    modules: tuple[str, ...]
    expected_detail: str
    expected_examples: tuple[str, ...]


class ReportProgress(t.Protocol):
    """Progress callbacks expected by insights report enrichment."""

    def llm_started(self, *, backend: str, model: str, endpoint: str) -> None:
        """Report that a local LLM request is starting."""

    def llm_waiting(self, *, backend: str, model: str, endpoint: str) -> None:
        """Report that a local LLM request is waiting for tokens."""

    def llm_chunk(
        self,
        *,
        backend: str,
        model: str,
        chunk_count: int,
        char_count: int,
    ) -> None:
        """Report one or more streamed response chunks."""

    def llm_finished(
        self,
        *,
        backend: str,
        model: str,
        chunk_count: int,
        char_count: int,
    ) -> None:
        """Report that local LLM streaming has finished."""


INSIGHTS_SETUP_PARSE_CASES: tuple[InsightsSetupParseCase, ...] = (
    InsightsSetupParseCase(
        test_id="setup-defaults-to-dry-run-auto-manager",
        argv=("insights", "setup", "html"),
        expected_level="html",
        expected_manager="auto",
        expected_install=False,
        expected_yes=False,
    ),
    InsightsSetupParseCase(
        test_id="setup-captures-explicit-install-confirmation",
        argv=("insights", "setup", "embeddings", "--manager", "pip", "--install", "--yes"),
        expected_level="embeddings",
        expected_manager="pip",
        expected_install=True,
        expected_yes=True,
    ),
)

RUNTIME_CONFIG_CASES: tuple[RuntimeConfigCase, ...] = (
    RuntimeConfigCase(
        test_id="llm-installed-but-no-model",
        level="llm",
        modules=("llama_cpp", "httpx"),
        expected_detail="local llama.cpp model path or Ollama model name",
        expected_examples=(
            "agentgrep insights report --level llm --model /path/to/model.gguf",
            "agentgrep insights report --level llm --llm-backend ollama --model llama3",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    INSIGHTS_PARSE_CASES,
    ids=[case.test_id for case in INSIGHTS_PARSE_CASES],
)
def test_insights_report_parse_args(case: InsightsParseCase) -> None:
    """The report parser captures bounded pure-Python defaults."""
    parsed = agentgrep.parse_args(case.argv)
    assert isinstance(parsed, agentgrep.InsightsReportArgs)
    assert parsed.scope == case.expected_scope
    assert parsed.level == case.expected_level
    assert parsed.limit == case.expected_limit
    assert parsed.all_records == case.expected_all_records
    assert parsed.report_format == case.expected_report_format
    assert parsed.output_path == case.expected_output_path


def test_insights_levels_parse_args() -> None:
    """The levels command supports machine-readable output."""
    parsed = agentgrep.parse_args(("insights", "levels", "--json"))
    assert isinstance(parsed, agentgrep.InsightsLevelsArgs)
    assert parsed.output_mode == "json"


def test_insights_doctor_parse_args() -> None:
    """The doctor command supports machine-readable output."""
    parsed = agentgrep.parse_args(("insights", "doctor", "--ndjson"))
    assert isinstance(parsed, agentgrep.InsightsDoctorArgs)
    assert parsed.output_mode == "ndjson"


@pytest.mark.parametrize(
    "case",
    INSIGHTS_SETUP_PARSE_CASES,
    ids=[case.test_id for case in INSIGHTS_SETUP_PARSE_CASES],
)
def test_insights_setup_parse_args(case: InsightsSetupParseCase) -> None:
    """The setup parser captures explicit environment mutation choices."""
    parsed = agentgrep.parse_args(case.argv)
    assert isinstance(parsed, agentgrep.InsightsSetupArgs)
    assert parsed.level == case.expected_level
    assert parsed.manager == case.expected_manager
    assert parsed.install is case.expected_install
    assert parsed.yes is case.expected_yes


def test_insights_report_rejects_limit_with_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--all`` and ``--limit`` are mutually exclusive report bounds."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("insights", "report", "--all", "--limit", "20"))
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--all cannot be combined with --limit" in captured.err
    assert "Traceback" not in captured.err


def _search_record(
    text: str,
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "codex.history",
    timestamp: str | None = None,
) -> agentgrep.SearchRecord:
    """Build one synthetic search record for report tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=agent,
        store=store,
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/history.jsonl"),
        text=text,
        timestamp=timestamp,
    )


def test_insights_report_json_uses_bounded_builtin_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON reports summarize bounded records without optional dependencies."""
    seen_queries: list[agentgrep.SearchQuery] = []

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, progress, control)
        seen_queries.append(query)
        return [
            _search_record(
                "Deploy docs and docs release notes",
                timestamp="2026-06-01T12:00:00Z",
            ),
            _search_record(
                "Deploy docs again",
                store="claude.projects",
                agent="claude",
                timestamp="2026-06-02T12:00:00Z",
            ),
        ]

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)

    exit_code = agentgrep.main(("insights", "report", "--json", "--limit", "2"))

    assert exit_code == 0
    assert len(seen_queries) == 1
    query = seen_queries[0]
    assert query.terms == ()
    assert query.scope == "prompts"
    assert query.limit == 2
    assert query.dedupe is False

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "insights report"
    result = payload["results"][0]
    assert result["level"] == "builtin"
    assert result["records_analyzed"] == 2
    assert result["sampled"] is True
    assert result["record_limit"] == 2
    assert result["agents"] == {"claude": 1, "codex": 1}
    assert result["stores"] == {"claude.projects": 1, "codex.history": 1}
    assert result["top_terms"][0]["term"] == "docs"
    assert result["enrichments"] == []


def test_insights_report_text_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default text output is human-readable and mentions builtin mode."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Local report without models")]

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)

    exit_code = agentgrep.main(("insights", "report", "--no-progress"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Insights report" in output
    assert "level: builtin" in output
    assert "records analyzed: 1" in output
    assert "optional enrichers skipped" in output


def test_insights_report_progress_always_emits_search_and_report_steps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report progress mirrors search progress and names report-building work."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, control)
        assert progress is not None
        progress.start(query)
        progress.sources_discovered(1)
        progress.finish(1)
        return [_search_record("Progress report")]

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)

    exit_code = agentgrep.main(("insights", "report", "--progress", "always"))

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Searching all records" in captured.err
    assert "Search complete: 1 match" in captured.err
    assert "Building insights report: level builtin" in captured.err
    assert "Traceback" not in captured.err


def test_insights_report_progress_heartbeats_while_building() -> None:
    """Long report enrichment emits ongoing progress before it returns."""
    stream = io.StringIO()
    progress = cli_render.InsightsReportProgress(
        enabled=True,
        stream=stream,
        tty=False,
        refresh_interval=0.005,
        heartbeat_interval=0.01,
    )

    progress.start("llm")
    deadline = time.monotonic() + 1.0
    while "... still building insights report: level llm" not in stream.getvalue():
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)
    progress.close()

    output = stream.getvalue()
    assert "Building insights report: level llm" in output
    assert "... still building insights report: level llm" in output


def test_insights_report_progress_reports_ollama_streaming_steps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LLM report progress includes Ollama contact, wait, chunk, and done states."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Local report with Ollama progress")]

    def fake_build_report(
        records: t.Iterable[agentgrep.SearchRecord],
        *,
        scope: agentgrep.SearchScope,
        requested_level: insights.InsightsLevel,
        record_limit: int | None,
        sampled: bool,
        model: str | None = None,
        model_cache: pathlib.Path | None = None,
        allow_download: bool = False,
        llm_backend: insights.InsightsLLMBackend = "auto",
        llm_endpoint: str = "http://127.0.0.1:11434",
        allow_network: bool = False,
        index_backend: insights.InsightsIndexBackend = "auto",
        progress: ReportProgress | None = None,
    ) -> insights.InsightsReport:
        _ = (
            model_cache,
            allow_download,
            llm_backend,
            allow_network,
            index_backend,
        )
        records_list = list(records)
        assert progress is not None
        progress.llm_started(backend="ollama", model=model or "", endpoint=llm_endpoint)
        progress.llm_waiting(backend="ollama", model=model or "", endpoint=llm_endpoint)
        progress.llm_chunk(
            backend="ollama",
            model=model or "",
            chunk_count=1,
            char_count=7,
        )
        progress.llm_finished(
            backend="ollama",
            model=model or "",
            chunk_count=1,
            char_count=7,
        )
        return insights.InsightsReport(
            level="llm",
            requested_level=requested_level,
            scope=scope,
            records_analyzed=len(records_list),
            record_limit=record_limit,
            sampled=sampled,
            agents={"codex": len(records_list)},
            stores={"codex.history": len(records_list)},
            kinds={"prompt": len(records_list)},
            earliest_timestamp=None,
            latest_timestamp=None,
            top_terms=(),
            skipped_enrichers=(),
            enrichments=(),
        )

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    monkeypatch.setattr(insights, "build_report", fake_build_report)

    exit_code = agentgrep.main(
        (
            "insights",
            "report",
            "--level",
            "llm",
            "--llm-backend",
            "ollama",
            "--model",
            "llama3",
            "--progress",
            "always",
        ),
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Building insights report: level llm" in captured.err
    assert "Contacting Ollama at http://127.0.0.1:11434" in captured.err
    assert "Waiting for Ollama model llama3" in captured.err
    assert "Streaming Ollama response: 1 chunk, 7 chars" in captured.err
    assert "Ollama response complete: 1 chunk, 7 chars" in captured.err


def test_insights_report_explicit_missing_backend_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit optional levels fail instead of silently falling back."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Local report without models")]

    def fake_import_module(name: str) -> types.ModuleType:
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    monkeypatch.setattr(insights, "import_module_for_backend", fake_import_module)

    exit_code = agentgrep.main(("insights", "report", "--level", "ml"))

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Missing optional insights backend for level 'ml'" in captured.err
    assert "agentgrep insights setup ml --install --yes" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "case",
    RUNTIME_CONFIG_CASES,
    ids=[case.test_id for case in RUNTIME_CONFIG_CASES],
)
def test_insights_report_installed_backend_missing_runtime_config_guides_next_step(
    case: RuntimeConfigCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Installed optional backends ask for runtime inputs, not reinstall."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Local report without models")]

    def fake_import_module(name: str) -> types.ModuleType:
        if name in case.modules:
            return types.ModuleType(name)
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    monkeypatch.setattr(insights, "import_module_for_backend", fake_import_module)

    exit_code = agentgrep.main(("insights", "report", "--level", case.level))

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "needs runtime configuration" in captured.err
    assert case.expected_detail in captured.err
    assert "\nTry:\n" in captured.err
    for command in case.expected_examples:
        assert f"  {command}\n" in captured.err
    assert f"agentgrep insights setup {case.level}" not in captured.err
    assert "Traceback" not in captured.err


def test_insights_report_ollama_timeout_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ollama connection failures are CLI diagnostics, not Python tracebacks."""

    class FakeHTTPError(Exception):
        """Base fake httpx transport error."""

    class FakeTimeoutException(FakeHTTPError):
        """Fake timeout raised by the local Ollama client."""

        def __init__(self) -> None:
            super().__init__("timed out")

    class FakeClient:
        """Minimal context-manager client with the httpx.Client surface."""

        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> t.Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: types.TracebackType | None,
        ) -> bool:
            _ = (exc_type, exc, traceback)
            return False

        def stream(self, method: str, url: str, *, json: object) -> object:
            _ = (method, url, json)
            raise FakeTimeoutException()

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Local report with Ollama")]

    httpx = types.ModuleType("httpx")
    vars(httpx).update(
        {
            "Client": FakeClient,
            "HTTPError": FakeHTTPError,
            "TimeoutException": FakeTimeoutException,
        },
    )

    def fake_import_module(name: str) -> types.ModuleType:
        if name == "httpx":
            return httpx
        if name == "llama_cpp":
            return types.ModuleType("llama_cpp")
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    monkeypatch.setattr(insights, "import_module_for_backend", fake_import_module)

    exit_code = agentgrep.main(
        (
            "insights",
            "report",
            "--level",
            "llm",
            "--llm-backend",
            "ollama",
            "--model",
            "llama3",
            "--progress",
            "always",
        ),
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Building insights report: level llm" in captured.err
    assert "Ollama" in captured.err
    assert "timed out" in captured.err
    assert "ollama serve" in captured.err
    assert "Traceback" not in captured.err


def test_insights_report_best_installed_falls_back_to_builtin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``best-installed`` stays offline and safe when no optional backend is usable."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Builtin fallback report")]

    def fake_import_module(name: str) -> types.ModuleType:
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    monkeypatch.setattr(insights, "import_module_for_backend", fake_import_module)

    exit_code = agentgrep.main(("insights", "report", "--level", "best-installed", "--json"))

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    result = payload["results"][0]
    assert result["requested_level"] == "best-installed"
    assert result["level"] == "builtin"
    assert result["enrichments"] == []


def test_insights_levels_json_reports_optional_extras(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``insights levels`` reports five optional extras without imports."""
    monkeypatch.setattr(insights, "_module_available", lambda name: name == "sklearn")

    exit_code = agentgrep.main(("insights", "levels", "--json"))

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "insights levels"
    by_level = {row["level"]: row for row in payload["results"]}
    assert tuple(by_level) == ("builtin", "html", "ml", "embeddings", "index", "llm")
    assert by_level["builtin"]["installed"] is True
    assert by_level["builtin"]["extra"] is None
    assert by_level["html"]["extra"] == "insights-html"
    assert by_level["html"]["installed"] is False
    assert by_level["html"]["missing_modules"] == ["jinja2", "platformdirs"]
    assert by_level["ml"]["extra"] == "insights-ml"
    assert by_level["ml"]["installed"] is True
    assert by_level["llm"]["extra"] == "insights-llm"


def test_insights_doctor_text_lists_setup_hints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``insights doctor`` gives actionable missing-extra hints."""
    seen_modules: list[str] = []

    def fake_module_available(name: str) -> bool:
        seen_modules.append(name)
        return False

    monkeypatch.setattr(insights, "_module_available", fake_module_available)

    exit_code = agentgrep.main(("insights", "doctor"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Insights doctor" in output
    assert "builtin: available" in output
    assert "html: missing" in output
    assert "agentgrep insights setup html --install --yes" in output
    assert "sklearn" in seen_modules
    assert "sentence_transformers" in seen_modules


def test_insights_setup_dry_run_prefers_uv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Setup defaults to a dry-run command and prefers uv when available."""
    monkeypatch.setattr(
        insights.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )

    exit_code = agentgrep.main(("insights", "setup", "embeddings"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert 'uv pip install "agentgrep[insights-embeddings]"' in output
    assert "Dry run" in output


def test_insights_setup_dry_run_can_force_pip(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Users can request a pip-shaped setup command explicitly."""
    exit_code = agentgrep.main(("insights", "setup", "ml", "--manager", "pip"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"{sys.executable} -m pip install " in output
    assert '"agentgrep[insights-ml]"' in output


def test_insights_setup_install_requires_yes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Setup refuses environment mutation without explicit confirmation."""
    exit_code = agentgrep.main(("insights", "setup", "llm", "--install"))

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "Traceback" not in captured.err


def test_insights_setup_install_executes_confirmed_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Confirmed setup executes the exact extra install command."""
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert check is False
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agentgrep.cli.render.subprocess.run", fake_run)

    exit_code = agentgrep.main(
        ("insights", "setup", "html", "--manager", "pip", "--install", "--yes"),
    )

    assert exit_code == 0
    assert calls == [
        (sys.executable, "-m", "pip", "install", "agentgrep[insights-html]"),
    ]
    assert "Install completed" in capsys.readouterr().out


def test_insights_setup_llm_install_guides_model_next_step(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LLM setup install completion tells users how to provide a local model."""

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        _ = command
        assert check is False
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agentgrep.cli.render.subprocess.run", fake_run)

    exit_code = agentgrep.main(("insights", "setup", "llm", "--install", "--yes"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Install completed" in output
    assert "agentgrep insights report --level llm --model /path/to/model.gguf" in output
    assert "agentgrep insights report --level llm --llm-backend ollama --model llama3" in output
