"""Tests for scripts/otel_acceptance.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import typing as t
import urllib.parse

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "otel_acceptance.py"

_spec = importlib.util.spec_from_file_location("otel_acceptance_script", _SCRIPT)
assert _spec and _spec.loader
otel_acceptance = importlib.util.module_from_spec(_spec)
sys.modules["otel_acceptance_script"] = otel_acceptance
_spec.loader.exec_module(otel_acceptance)


def test_start_stack_starts_existing_stopped_container(
    monkeypatch: t.Any,
) -> None:
    """An existing stopped LGTM container should be started, not ignored."""
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check
        calls.append(command)
        if command == ["docker", "inspect", otel_acceptance.CONTAINER_NAME]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    [
                        {
                            "State": {"Running": False},
                            "Config": {
                                "Labels": {
                                    "agentgrep.lgtm.config": otel_acceptance.LGTM_CONFIG_LABEL,
                                },
                            },
                        },
                    ],
                ),
            )
        if command == ["docker", "start", otel_acceptance.CONTAINER_NAME]:
            return subprocess.CompletedProcess(command, 0, stdout=otel_acceptance.CONTAINER_NAME)
        msg = f"unexpected command: {command}"
        raise AssertionError(msg)

    monkeypatch.setattr(otel_acceptance.subprocess, "run", fake_run)
    monkeypatch.setattr(otel_acceptance, "generate_lgtm_source_map", lambda: None)

    otel_acceptance.start_stack()

    assert calls == [
        ["docker", "inspect", otel_acceptance.CONTAINER_NAME],
        ["docker", "start", otel_acceptance.CONTAINER_NAME],
    ]


def test_lgtm_docker_run_command_mounts_source_linking_configs() -> None:
    """The acceptance stack should use the same LGTM config as ``just otel-up``."""
    command = otel_acceptance.lgtm_docker_run_command(env={})

    assert command[:4] == ["docker", "run", "-d", "--name"]
    assert f"agentgrep.lgtm.config={otel_acceptance.LGTM_CONFIG_LABEL}" in command
    assert str(otel_acceptance.LGTM_GRAFANA_DATASOURCES_CONFIG) in " ".join(command)
    assert str(otel_acceptance.LGTM_PYROSCOPE_CONFIG) in " ".join(command)
    assert command[-1] == "grafana/otel-lgtm:latest"


def test_start_stack_recreates_container_with_stale_config(monkeypatch: t.Any) -> None:
    """A pre-existing LGTM container without current mounts should be recreated."""
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check
        calls.append(command)
        if command == ["docker", "inspect", otel_acceptance.CONTAINER_NAME]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"State": {"Running": True}, "Config": {"Labels": {}}}]),
            )
        if command == ["docker", "rm", "-f", otel_acceptance.CONTAINER_NAME]:
            return subprocess.CompletedProcess(command, 0, stdout=otel_acceptance.CONTAINER_NAME)
        if command[:5] == ["docker", "run", "-d", "--name", otel_acceptance.CONTAINER_NAME]:
            return subprocess.CompletedProcess(command, 0, stdout=otel_acceptance.CONTAINER_NAME)
        msg = f"unexpected command: {command}"
        raise AssertionError(msg)

    monkeypatch.setattr(otel_acceptance.subprocess, "run", fake_run)
    monkeypatch.setattr(otel_acceptance, "generate_lgtm_source_map", lambda: None)

    otel_acceptance.start_stack()

    assert calls[0] == ["docker", "inspect", otel_acceptance.CONTAINER_NAME]
    assert calls[1] == ["docker", "rm", "-f", otel_acceptance.CONTAINER_NAME]
    assert calls[2][:5] == ["docker", "run", "-d", "--name", otel_acceptance.CONTAINER_NAME]


def test_lgtm_docker_run_command_keeps_github_env_opt_in() -> None:
    """GitHub OAuth values should be forwarded only when the caller set them."""
    command_without_github = otel_acceptance.lgtm_docker_run_command(env={})

    assert "-e" not in command_without_github

    command_with_github = otel_acceptance.lgtm_docker_run_command(
        env={
            "GITHUB_CLIENT_ID": "client",
            "GITHUB_CLIENT_SECRET": "secret",
            "GITHUB_SESSION_SECRET": "session",
            "GH_TOKEN": "api-token",
        },
    )

    assert command_with_github[-7:-5] == ["-e", "GITHUB_CLIENT_ID"]
    assert command_with_github[-5:-3] == ["-e", "GITHUB_CLIENT_SECRET"]
    assert command_with_github[-3:-1] == ["-e", "GITHUB_SESSION_SECRET"]
    assert "GH_TOKEN" not in command_with_github


def test_grep_parse_error_workload_uses_invalid_regex() -> None:
    """Acceptance should still exercise traced argparse failures."""
    run_id = "agentgrep-test-run"

    assert otel_acceptance._grep_parse_error_workload_command(run_id) == [
        sys.executable,
        "-m",
        "agentgrep",
        "grep",
        "[",
    ]


def test_cli_acceptance_matrix_covers_short_lived_process_shapes() -> None:
    """The live CLI matrix should identify each short subprocess by candidate id."""
    cases = otel_acceptance._cli_acceptance_workload_cases("run-123")

    assert [(case.test_id, case.expected_returncode) for case in cases] == [
        ("help", 0),
        ("search", 0),
        ("grep-parse-error", 2),
        ("grep-invert", 0),
        ("find", 0),
        ("json-no-hit", 1),
        ("ui-help", 0),
    ]
    assert [case.candidate_id for case in cases] == [
        "cli-help",
        "cli-search",
        "cli-grep-parse-error",
        "cli-grep-invert",
        "cli-find",
        "cli-json-no-hit",
        "cli-ui-help",
    ]


def test_cli_acceptance_matrix_sets_candidate_env(
    monkeypatch: t.Any,
    tmp_path: pathlib.Path,
) -> None:
    """Each live CLI subprocess should carry its candidate id in resource attrs."""
    observed: list[tuple[list[str], str | None]] = []
    expected_codes = {
        "cli-help": 0,
        "cli-search": 0,
        "cli-grep-parse-error": 2,
        "cli-grep-invert": 0,
        "cli-find": 0,
        "cli-json-no-hit": 1,
        "cli-ui-help": 0,
    }

    def fake_run(
        command: list[str],
        *,
        cwd: pathlib.Path,
        env: dict[str, str],
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check, capture_output, text
        candidate_id = env.get("AGENTGREP_DEBUG_CANDIDATE_ID")
        observed.append((command, candidate_id))
        assert candidate_id is not None
        return subprocess.CompletedProcess(
            command,
            expected_codes[candidate_id],
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(otel_acceptance.subprocess, "run", fake_run)

    otel_acceptance._run_cli_acceptance_matrix(
        "run-123",
        home=tmp_path,
        env={"AGENTGREP_DEBUG_SESSION_ID": "run-123"},
    )

    assert [candidate_id for _command, candidate_id in observed] == list(expected_codes)


def test_tui_acceptance_workload_exercises_tui_root_and_child_spans() -> None:
    """Acceptance should exercise idle TUI lifecycle and shutdown child spans."""
    command = otel_acceptance._tui_root_workload_command()

    assert command[:2] == [sys.executable, "-c"]
    assert "agentgrep.tui.search" not in command[2]
    assert "ui_app.run_ui(" in command[2]
    assert 'initial_search_text="acceptance tui"' in command[2]


def test_mcp_acceptance_workload_exercises_server_root_and_flush() -> None:
    """Acceptance should exercise short MCP server lifecycle and flush spans."""
    command = otel_acceptance._mcp_server_workload_command()

    assert command[:2] == [sys.executable, "-c"]
    assert "mcp_server.main()" in command[2]
    assert "FakeServer" in command[2]


def test_query_logs_filters_run_id_via_structured_metadata(monkeypatch: t.Any) -> None:
    """Loki log checks query run ids through OTLP structured metadata, no JSON parse."""
    observed_urls: list[str] = []

    def fake_http_json(url: str, **_kwargs: object) -> dict[str, object]:
        observed_urls.append(url)
        return {
            "data": {
                "result": [
                    {
                        "stream": {
                            "service_name": "agentgrep",
                            "vcs_ref_head_name": "otel-bootstrap",
                        },
                        "values": [
                            [
                                "1782000000000000000",
                                "mcp request completed",
                                {
                                    "agentgrep_debug_session_id": "run-123",
                                    "trace_id": "trace",
                                    "span_id": "span",
                                },
                            ],
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(otel_acceptance, "http_json", fake_http_json)

    result = otel_acceptance.query_logs(
        "run-123",
        {"labels": {"vcs_ref_head_name": "otel-bootstrap"}},
    )

    parsed = urllib.parse.urlparse(observed_urls[0])
    params = urllib.parse.parse_qs(parsed.query)
    assert params["query"] == [
        '{service_name=~"agentgrep|agentgrep-.+"} | agentgrep_debug_session_id="run-123"',
    ]
    assert "| json" not in params["query"][0]
    assert result["count"] == 1


def test_orphan_trace_spans_detect_missing_parent() -> None:
    """Tempo traces with child spans whose parent is absent should fail acceptance."""
    spans = [
        {"spanID": "root", "name": "agentgrep.cli.invocation"},
        {
            "spanID": "child",
            "parentSpanId": "missing-parent",
            "name": "agentgrep.cli.parse",
        },
    ]

    assert otel_acceptance._orphan_trace_spans(spans) == [
        {
            "span": "agentgrep.cli.parse",
            "span_id": "child",
            "parent_span_id": "missing-parent",
        },
    ]


class _TraceRootCase(t.NamedTuple):
    """Parametrized case for trace-root acceptance classification."""

    test_id: str
    spans: list[dict[str, object]]
    expected_status: str
    expected_root: str | None


_TRACE_ROOT_CASES: tuple[_TraceRootCase, ...] = (
    _TraceRootCase(
        test_id="approved-single-span-root",
        spans=[{"spanID": "root", "name": "agentgrep.cli.invocation"}],
        expected_status="approved",
        expected_root="agentgrep.cli.invocation",
    ),
    _TraceRootCase(
        test_id="approved-multi-span-root",
        spans=[
            {"spanID": "root", "name": "agentgrep.cli.invocation"},
            {"spanID": "child", "parentSpanId": "root", "name": "agentgrep.cli.parse"},
        ],
        expected_status="approved",
        expected_root="agentgrep.cli.invocation",
    ),
    _TraceRootCase(
        test_id="unapproved-single-span-root",
        spans=[{"spanID": "root", "name": "agentgrep.engine.scan"}],
        expected_status="bad_root",
        expected_root="agentgrep.engine.scan",
    ),
    _TraceRootCase(
        test_id="orphan-child",
        spans=[
            {"spanID": "root", "name": "agentgrep.cli.invocation"},
            {"spanID": "child", "parentSpanId": "missing", "name": "agentgrep.cli.parse"},
        ],
        expected_status="orphan",
        expected_root=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    _TRACE_ROOT_CASES,
    ids=[case.test_id for case in _TRACE_ROOT_CASES],
)
def test_evaluate_trace_root_accepts_approved_single_span_roots(case: _TraceRootCase) -> None:
    """Approved roots are valid at any span count; orphans and unknown roots fail."""
    verdict = otel_acceptance._evaluate_trace_root(case.spans, otel_acceptance.APPROVED_ROOTS)

    assert verdict.status == case.expected_status
    assert verdict.root_name == case.expected_root


def test_query_logs_rejects_logs_missing_trace_link(monkeypatch: t.Any) -> None:
    """A selected log without trace and span ids in its metadata must fail."""

    def fake_http_json(_url: str, **_kwargs: object) -> dict[str, object]:
        return {
            "data": {
                "result": [
                    {
                        "stream": {
                            "service_name": "agentgrep",
                            "vcs_ref_head_name": "otel-bootstrap",
                        },
                        "values": [
                            [
                                "1782000000000000000",
                                "search completed",
                                {"agentgrep_debug_session_id": "run-123"},
                            ],
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(otel_acceptance, "http_json", fake_http_json)

    with pytest.raises(otel_acceptance.AcceptanceCheckError, match="unlinked agentgrep logs"):
        otel_acceptance.query_logs(
            "run-123",
            {"labels": {"vcs_ref_head_name": "otel-bootstrap"}},
        )


def test_query_logs_rejects_streams_missing_vcs_labels(monkeypatch: t.Any) -> None:
    """A log stream without the expected VCS labels must fail acceptance."""

    def fake_http_json(_url: str, **_kwargs: object) -> dict[str, object]:
        return {
            "data": {
                "result": [
                    {
                        "stream": {"service_name": "agentgrep"},
                        "values": [
                            [
                                "1782000000000000000",
                                "search completed",
                                {
                                    "agentgrep_debug_session_id": "run-123",
                                    "trace_id": "trace",
                                    "span_id": "span",
                                },
                            ],
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(otel_acceptance, "http_json", fake_http_json)

    with pytest.raises(otel_acceptance.AcceptanceCheckError, match="missing VCS labels"):
        otel_acceptance.query_logs(
            "run-123",
            {"labels": {"vcs_ref_head_name": "otel-bootstrap"}},
        )


def test_pyroscope_label_values_body_scopes_to_run_and_source_labels() -> None:
    """Pyroscope label queries should not scan broad historical profile windows."""
    body = otel_acceptance._pyroscope_label_values_body(
        "service_git_ref",
        run_id="run-123",
        vcs_identity={
            "labels": {
                "vcs_ref_head_revision": "abc123",
                "vcs_repository_url_full": "https://github.com/tony/agentgrep",
            },
            "resource": {
                "vcs.ref.head.revision": "abc123",
                "vcs.repository.url.full": "https://github.com/tony/agentgrep",
            },
        },
        now_ms=1_782_000_000_000,
    )

    assert body["name"] == "service_git_ref"
    assert body["start"] == 1_782_000_000_000 - 60 * 60 * 1000
    assert body["end"] == 1_782_000_000_000
    assert body["matchers"] == [
        (
            '{agentgrep_debug_session_id="run-123",service_git_ref="abc123",'
            'service_name=~"agentgrep|agentgrep-.+",'
            'service_repository="https://github.com/tony/agentgrep",'
            'vcs_ref_head_revision="abc123",'
            'vcs_repository_url_full="https://github.com/tony/agentgrep"}'
        ),
    ]


def test_lgtm_grafana_datasource_forwards_pyroscope_git_session() -> None:
    """Grafana must forward Pyroscope's GitHub session cookie."""
    content = otel_acceptance.LGTM_GRAFANA_DATASOURCES_CONFIG.read_text(encoding="utf-8")

    assert "grafana-pyroscope-datasource" in content
    assert "keepCookies: [pyroscope_git_session]" in content


class _DatasourceLinkCase(t.NamedTuple):
    """Parametrized case for a Grafana datasource cross-link."""

    test_id: str
    source: str
    field: str
    target: str
    profile_type_prefix: str | None


_DATASOURCE_LINK_CASES: tuple[_DatasourceLinkCase, ...] = (
    _DatasourceLinkCase("loki-logs-to-traces", "Loki", "derivedFields", "Tempo", None),
    _DatasourceLinkCase("tempo-traces-to-logs", "Tempo", "tracesToLogsV2", "Loki", None),
    _DatasourceLinkCase(
        "tempo-traces-to-metrics", "Tempo", "tracesToMetricsV2", "Prometheus", None
    ),
    _DatasourceLinkCase(
        "tempo-traces-to-profiles", "Tempo", "tracesToProfilesV2", "Pyroscope", "process_cpu:"
    ),
)


@pytest.mark.parametrize(
    "case",
    _DATASOURCE_LINK_CASES,
    ids=[case.test_id for case in _DATASOURCE_LINK_CASES],
)
def test_lgtm_grafana_datasource_links_resolve_by_uid(case: _DatasourceLinkCase) -> None:
    """Each datasource cross-link must target the uid of its named datasource."""
    import yaml

    content = otel_acceptance.LGTM_GRAFANA_DATASOURCES_CONFIG.read_text(encoding="utf-8")
    by_name = {source["name"]: source for source in yaml.safe_load(content)["datasources"]}
    link = by_name[case.source]["jsonData"][case.field]
    if isinstance(link, list):
        trace_field = next(field for field in link if field["name"] == "trace_id")
        assert trace_field["matcherType"] == "label"
        assert trace_field["matcherRegex"] == "trace_id"
        link_uid = trace_field["datasourceUid"]
    else:
        link_uid = link["datasourceUid"]
        if case.profile_type_prefix is not None:
            assert link["profileTypeId"].startswith(case.profile_type_prefix)
    assert link_uid == by_name[case.target]["uid"]
