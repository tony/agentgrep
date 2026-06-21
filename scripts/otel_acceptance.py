"""Live LGTM acceptance checks for agentgrep telemetry."""

from __future__ import annotations

import argparse
import collections.abc as cabc
import contextlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTAINER_NAME = "agentgrep-lgtm"
LGTM_CONFIG_LABEL = "source-linking-v1"
LGTM_GRAFANA_DATASOURCES_CONFIG = ROOT / "scripts" / "lgtm" / "grafana-datasources.yaml"
LGTM_PYROSCOPE_CONFIG = ROOT / "scripts" / "lgtm" / "pyroscope-config.yaml"
LGTM_SOURCE_MAP_GENERATOR = ROOT / "scripts" / "lgtm" / "generate_pyroscope_source_map.py"
LGTM_SOURCE_MAP = ROOT / ".tmp" / "lgtm" / ".pyroscope.yaml"
DEFAULT_LOKI_BASE_URL = "http://localhost:3000/api/datasources/proxy/uid/loki"
DEFAULT_PROMETHEUS_BASE_URL = "http://localhost:3000/api/datasources/proxy/uid/prometheus"
APPROVED_ROOTS = {
    "agentgrep.cli.invocation",
    "agentgrep.mcp.server",
    "agentgrep.tui.session",
    "mcp.server.request",
    "mcp.server.tool",
    "agentgrep.benchmark.run",
    "agentgrep.profile_engine.run",
    "agentgrep.pytest.session",
    "agentgrep.pytest.test",
    "agentgrep.otel.smoke",
}
VCS_RESOURCE_TO_LABEL = {
    "vcs.repository.name": "vcs_repository_name",
    "vcs.repository.url.full": "vcs_repository_url_full",
    "vcs.ref.head.name": "vcs_ref_head_name",
    "vcs.ref.head.revision": "vcs_ref_head_revision",
    "vcs.ref.head.type": "vcs_ref_head_type",
}
SERVICE_NAME_PATTERN = "agentgrep|agentgrep-.+"
PYROSCOPE_SOURCE_LABELS = {
    "service_git_ref": "vcs.ref.head.revision",
    "service_repository": "vcs.repository.url.full",
}
REQUIRED_VCS_RESOURCE_KEYS = (
    "vcs.ref.head.name",
    "vcs.ref.head.revision",
    "vcs.ref.head.type",
    "vcs.repository.name",
)


class AcceptanceCheckError(RuntimeError):
    """Raised when live LGTM evidence is not yet available."""


class CliAcceptanceWorkloadCase(t.NamedTuple):
    """One short-lived CLI subprocess shape for live trace coverage."""

    test_id: str
    candidate_id: str
    command: list[str]
    expected_returncode: int


def main() -> int:
    """Run acceptance checks."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-stack", action="store_true")
    parser.add_argument("--run-id", default=f"agentgrep-otel-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    if args.start_stack:
        start_stack()
    wait_for_lgtm(args.timeout)
    started_at = time.time()
    vcs_identity = expected_vcs_identity()
    run_workloads(args.run_id)
    deadline = time.monotonic() + args.timeout
    evidence: dict[str, object] = {}
    evidence["traces"] = wait_for(
        lambda: query_traces(args.run_id, vcs_identity),
        deadline,
        "traces",
    )
    evidence["metrics"] = wait_for(
        lambda: query_metrics(started_at, args.run_id, vcs_identity),
        deadline,
        "metrics",
    )
    evidence["logs"] = wait_for(lambda: query_logs(args.run_id, vcs_identity), deadline, "logs")
    evidence["profiles"] = wait_for(
        lambda: query_profiles(args.run_id, vcs_identity),
        deadline,
        "profiles",
    )
    print(json.dumps({"run_id": args.run_id, "evidence": evidence}, indent=2, sort_keys=True))
    return 0


def start_stack() -> None:
    """Start the local LGTM container if needed."""
    generate_lgtm_source_map()
    inspect = subprocess.run(
        ["docker", "inspect", CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode == 0:
        if not _container_has_current_config(inspect.stdout):
            subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=True)
            subprocess.run(lgtm_docker_run_command(env=os.environ), check=True)
            return
        if not _container_is_running(inspect.stdout):
            subprocess.run(["docker", "start", CONTAINER_NAME], check=True)
        return
    subprocess.run(lgtm_docker_run_command(env=os.environ), check=True)


def lgtm_docker_run_command(*, env: cabc.Mapping[str, str]) -> list[str]:
    """Return the Docker command for the local LGTM stack."""
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "--label",
        f"agentgrep.lgtm.config={LGTM_CONFIG_LABEL}",
        "-p",
        "3000:3000",
        "-p",
        "3100:3100",
        "-p",
        "3200:3200",
        "-p",
        "4040:4040",
        "-p",
        "4317:4317",
        "-p",
        "4318:4318",
        "-p",
        "9090:9090",
        "-v",
        (
            f"{LGTM_GRAFANA_DATASOURCES_CONFIG}:"
            "/otel-lgtm/grafana/conf/provisioning/datasources/"
            "grafana-datasources.yaml:ro"
        ),
        "-v",
        f"{LGTM_PYROSCOPE_CONFIG}:/otel-lgtm/pyroscope-config.yaml:ro",
    ]
    for name in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_SESSION_SECRET"):
        if env.get(name):
            command.extend(["-e", name])
    command.append("grafana/otel-lgtm:latest")
    return command


def generate_lgtm_source_map() -> None:
    """Generate the local Pyroscope source map used for source-link setup."""
    subprocess.run(
        [
            sys.executable,
            str(LGTM_SOURCE_MAP_GENERATOR),
            "--output",
            str(LGTM_SOURCE_MAP),
        ],
        cwd=ROOT,
        check=True,
    )


def _container_is_running(inspect_stdout: str) -> bool:
    """Return whether ``docker inspect`` reports a running container."""
    with contextlib.suppress(json.JSONDecodeError, TypeError, KeyError, IndexError):
        payload = json.loads(inspect_stdout)
        return bool(payload[0]["State"]["Running"])
    return False


def _container_has_current_config(inspect_stdout: str) -> bool:
    """Return whether ``docker inspect`` reports the current local LGTM config."""
    with contextlib.suppress(json.JSONDecodeError, TypeError, KeyError, IndexError):
        payload = json.loads(inspect_stdout)
        labels = payload[0]["Config"].get("Labels") or {}
        return labels.get("agentgrep.lgtm.config") == LGTM_CONFIG_LABEL
    return False


def wait_for_lgtm(timeout: float) -> None:
    """Wait for LGTM APIs to become available."""
    deadline = time.monotonic() + timeout
    wait_for(lambda: http_json("http://localhost:3000/api/health"), deadline, "grafana")
    wait_for(lambda: http_text("http://localhost:3200/ready"), deadline, "tempo")
    wait_for(
        lambda: http_json(_prometheus_url("/api/v1/status/runtimeinfo")),
        deadline,
        "prometheus",
    )
    wait_for(lambda: http_text(_loki_url("/ready")), deadline, "loki")


def _agentgrep_module_command(*args: str) -> list[str]:
    """Return a Python-module agentgrep command."""
    return [sys.executable, "-m", "agentgrep", *args]


def _grep_parse_error_workload_command(run_id: str) -> list[str]:
    """Return the grep command used to exercise traced parse errors."""
    del run_id
    return _agentgrep_module_command("grep", "[")


def _cli_acceptance_workload_cases(run_id: str) -> tuple[CliAcceptanceWorkloadCase, ...]:
    """Return the short-lived CLI subprocess matrix."""
    return (
        CliAcceptanceWorkloadCase(
            test_id="help",
            candidate_id="cli-help",
            command=_agentgrep_module_command("--help"),
            expected_returncode=0,
        ),
        CliAcceptanceWorkloadCase(
            test_id="search",
            candidate_id="cli-search",
            command=_agentgrep_module_command(
                "search",
                "--agent",
                "codex",
                "--scope",
                "prompts",
                "--limit",
                "5",
                run_id,
            ),
            expected_returncode=0,
        ),
        CliAcceptanceWorkloadCase(
            test_id="grep-parse-error",
            candidate_id="cli-grep-parse-error",
            command=_grep_parse_error_workload_command(run_id),
            expected_returncode=2,
        ),
        CliAcceptanceWorkloadCase(
            test_id="grep-invert",
            candidate_id="cli-grep-invert",
            command=_agentgrep_module_command("grep", "--invert-match", run_id),
            expected_returncode=0,
        ),
        CliAcceptanceWorkloadCase(
            test_id="find",
            candidate_id="cli-find",
            command=_agentgrep_module_command("find", "codex", "--json"),
            expected_returncode=0,
        ),
        CliAcceptanceWorkloadCase(
            test_id="json-no-hit",
            candidate_id="cli-json-no-hit",
            command=_agentgrep_module_command(
                "search",
                "--agent",
                "codex",
                "--scope",
                "prompts",
                "--json",
                f"{run_id}-missing",
            ),
            expected_returncode=1,
        ),
        CliAcceptanceWorkloadCase(
            test_id="ui-help",
            candidate_id="cli-ui-help",
            command=_agentgrep_module_command("ui", "--help"),
            expected_returncode=0,
        ),
    )


def _run_cli_acceptance_matrix(
    run_id: str,
    *,
    home: pathlib.Path,
    env: cabc.Mapping[str, str],
) -> None:
    """Run short-lived CLI subprocesses with per-case candidate IDs."""
    for case in _cli_acceptance_workload_cases(run_id):
        completed = subprocess.run(
            case.command,
            cwd=ROOT,
            env={
                **env,
                "HOME": str(home),
                "AGENTGREP_DEBUG_CANDIDATE_ID": case.candidate_id,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != case.expected_returncode:
            message = (
                f"CLI workload {case.test_id} exited {completed.returncode}, "
                f"expected {case.expected_returncode}: {completed.stderr[:500]}"
            )
            raise AcceptanceCheckError(message)


def _tui_root_workload_command() -> list[str]:
    """Return a command that exercises a TUI root span without opening a UI."""
    code = """
import pathlib

import agentgrep
import agentgrep._telemetry as telemetry
from agentgrep.ui import app as ui_app


class FakeApp:
    def run(self) -> None:
        return None


def fake_build(*_args, **_kwargs):
    return FakeApp()


handle = telemetry.setup(repo_root=pathlib.Path.cwd(), service_name="agentgrep-tui")
try:
    ui_app.build_streaming_ui_app = fake_build
    query = agentgrep.SearchQuery(
        terms=("acceptance",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
    )
    ui_app.run_ui(
        pathlib.Path.home(),
        query,
        control=agentgrep.SearchControl(),
        initial_search_text="acceptance tui",
    )
finally:
    handle.shutdown()
""".strip()
    return [sys.executable, "-c", code]


def _mcp_server_workload_command() -> list[str]:
    """Return a command that exercises a short MCP stdio server lifecycle."""
    code = """
from agentgrep.mcp import server as mcp_server


class FakeServer:
    def run(self) -> None:
        return None


mcp_server.build_mcp_server = lambda: FakeServer()
raise SystemExit(mcp_server.main())
""".strip()
    return [sys.executable, "-c", code]


def run_workloads(run_id: str) -> None:
    """Run smoke, CLI, profiler, and pytest workloads."""
    env = {
        **os.environ,
        "AGENTGREP_OTEL": "live",
        "AGENTGREP_DEBUG_SESSION_ID": run_id,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
        "OTEL_EXPORTER_OTLP_TIMEOUT": "2",
        "PYROSCOPE_SERVER_ADDRESS": "http://localhost:4040",
    }
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "otel_smoke.py"),
            "--run-id",
            run_id,
            "--seconds",
            "10",
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="agentgrep-otel-home-") as temp_home:
        temp_home_path = pathlib.Path(temp_home)
        marker = f"{run_id} prompt\nacceptance invert line"
        session = temp_home_path / ".codex" / "sessions" / "2026" / "01" / "01" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {"type": "session_meta", "payload": {"id": run_id, "model_provider": "openai"}},
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": marker}],
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )
        cursor_db = temp_home_path / ".cursor" / "state.vscdb"
        cursor_db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(cursor_db)
        try:
            connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            cursor_payload = {"messages": [{"role": "user", "content": marker}]}
            connection.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("workbench.panel.chat.composerData", json.dumps(cursor_payload)),
            )
            connection.commit()
        finally:
            connection.close()
        _run_cli_acceptance_matrix(run_id, home=temp_home_path, env=env)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "profile_engine.py"),
                "grep-prompts",
                run_id,
                "--agent",
                "codex",
                "--max-count",
                "5",
                "--json",
            ],
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "profile_engine.py"),
                "search-prompts",
                run_id,
                "--agent",
                "cursor-ide",
                "--limit",
                "5",
                "--json",
            ],
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )
        benchmark_config = pathlib.Path(temp_home) / "benchmark-otel.toml"
        benchmark_config.write_text(
            "\n".join(
                [
                    "[settings]",
                    "warmup = 0",
                    "runs = 1",
                    'venv = "."',
                    "",
                    "[bench.otel-help]",
                    'description = "OTel acceptance benchmark help path"',
                    f"command = {json.dumps(f'{sys.executable} -m agentgrep --help')}",
                    'default_query = ""',
                    "",
                ],
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "benchmark.py"),
                "run",
                "--config",
                str(benchmark_config),
                "--target",
                "HEAD",
                "--commands",
                "otel-help",
                "--runs",
                "1",
                "--warmup",
                "0",
                "--no-sync",
                "--no-hyperfine",
                "--allow-dirty",
                "--format",
                "json",
                "--output",
                str(pathlib.Path(temp_home) / "benchmark-otel.json"),
                "--no-progress",
            ],
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            _tui_root_workload_command(),
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            _mcp_server_workload_command(),
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_agentgrep.py::test_streaming_ui_app_mounts_cleanly",
                "tests/test_agentgrep_mcp.py::test_mcp_lists_tools_resources_prompts_and_templates",
                "-q",
            ],
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )


def expected_vcs_identity() -> dict[str, dict[str, str]]:
    """Return the VCS identity that all telemetry signals should expose."""
    from agentgrep import _telemetry

    resource = _telemetry.build_resource_attributes(
        env={},
        repo_root=ROOT,
        service_version="acceptance",
    )
    expected_resource = {
        key: str(resource[key]) for key in VCS_RESOURCE_TO_LABEL if resource.get(key) is not None
    }
    missing = sorted(key for key in REQUIRED_VCS_RESOURCE_KEYS if key not in expected_resource)
    if missing:
        message = f"missing local VCS resource attributes: {missing}"
        raise AcceptanceCheckError(message)
    expected_labels = {
        VCS_RESOURCE_TO_LABEL[key]: value for key, value in expected_resource.items()
    }
    return {"resource": expected_resource, "labels": expected_labels}


class _TraceRootVerdict(t.NamedTuple):
    """Per-trace acceptance verdict for the trace evidence walk."""

    status: t.Literal["approved", "orphan", "bad_root"]
    root_name: str | None
    span_names: list[str]
    sqlite_span_names: list[str]
    orphans: list[dict[str, str]]


def _evaluate_trace_root(
    spans: cabc.Sequence[cabc.Mapping[str, object]],
    approved_roots: cabc.Set[str],
) -> _TraceRootVerdict:
    """Classify one trace's root.

    An approved root is accepted regardless of span count, so a single-span
    approved root (such as a childless CLI invocation or an idle pytest session)
    is valid evidence. Orphan child spans and roots outside ``approved_roots``
    are still rejected.
    """
    span_names = [str(span.get("name")) for span in spans if span.get("name")]
    sqlite_span_names = sorted(
        {name for name in span_names if name.startswith("agentgrep.sqlite.")},
    )
    orphans = _orphan_trace_spans(spans)
    if orphans:
        return _TraceRootVerdict("orphan", None, span_names, sqlite_span_names, orphans[:5])
    root_spans = [span for span in spans if not span.get("parentSpanId")]
    root_name = None if not root_spans else str(root_spans[0].get("name"))
    if root_name not in approved_roots:
        return _TraceRootVerdict("bad_root", root_name, span_names, sqlite_span_names, [])
    return _TraceRootVerdict("approved", root_name, span_names, sqlite_span_names, [])


def query_traces(run_id: str, vcs_identity: dict[str, dict[str, str]]) -> dict[str, object]:
    """Return trace evidence and reject orphan or unapproved-root traces."""
    params = urllib.parse.urlencode(
        {
            "q": f'{{resource.agentgrep.debug.session_id="{run_id}"}}',
            "limit": "200",
        },
    )
    data = http_json(f"http://localhost:3200/api/search?{params}")
    traces = _list_value(data, "traces")
    trace_ids: list[str] = []
    for trace_value in traces:
        trace = _dict_or_none(trace_value)
        if trace is not None and trace.get("traceID"):
            trace_ids.append(str(trace["traceID"]))
    checked: list[dict[str, object]] = []
    bad_root_traces: list[dict[str, object]] = []
    sqlite_trace_count = 0
    profile_engine_sqlite_trace_count = 0
    observed_span_names: set[str] = set()
    observed_cli_candidate_ids: set[str] = set()
    missing_vcs_traces: list[dict[str, object]] = []
    orphan_span_traces: list[dict[str, object]] = []
    expected_vcs_resource = vcs_identity["resource"]
    required_cli_candidate_ids = {
        case.candidate_id for case in _cli_acceptance_workload_cases(run_id)
    }
    for trace_id in trace_ids:
        trace_data = http_json(f"http://localhost:3200/api/traces/{trace_id}")
        resource_attributes = trace_resource_attributes(trace_data)
        spans = list(iter_trace_spans(trace_data))
        verdict = _evaluate_trace_root(spans, APPROVED_ROOTS)
        observed_span_names.update(verdict.span_names)
        if verdict.status == "orphan":
            orphan_span_traces.append({"trace_id": trace_id, "orphans": verdict.orphans})
            continue
        if verdict.status == "bad_root":
            bad_root_traces.append({"trace_id": trace_id, "root": verdict.root_name})
            continue
        root_name = verdict.root_name
        if not _labels_match(resource_attributes, expected_vcs_resource):
            missing_vcs_traces.append(
                {
                    "trace_id": trace_id,
                    "observed": _selected_labels(resource_attributes, expected_vcs_resource),
                },
            )
        checked.append(
            {
                "trace_id": trace_id,
                "span_count": len(spans),
                "root": root_name,
                "sqlite_spans": verdict.sqlite_span_names[:10],
                "vcs": _selected_labels(resource_attributes, expected_vcs_resource),
            },
        )
        if verdict.sqlite_span_names:
            sqlite_trace_count += 1
            if root_name == "agentgrep.profile_engine.run":
                profile_engine_sqlite_trace_count += 1
        if root_name == "agentgrep.cli.invocation":
            candidate_id = resource_attributes.get("agentgrep.debug.candidate_id")
            if isinstance(candidate_id, str):
                observed_cli_candidate_ids.add(candidate_id)
    if bad_root_traces:
        message = f"unexpected root traces found: {bad_root_traces[:5]}"
        raise AcceptanceCheckError(message)
    if orphan_span_traces:
        message = f"orphan child spans found: {orphan_span_traces[:5]}"
        raise AcceptanceCheckError(message)
    if not checked:
        message = f"no app-rooted traces found; candidates={trace_ids[:5]}"
        raise AcceptanceCheckError(message)
    if missing_vcs_traces:
        message = f"traces missing VCS resource attributes: {missing_vcs_traces[:5]}"
        raise AcceptanceCheckError(message)
    required_roots = {
        "agentgrep.otel.smoke",
        "agentgrep.benchmark.run",
        "agentgrep.cli.invocation",
        "agentgrep.mcp.server",
        "agentgrep.profile_engine.run",
        "agentgrep.pytest.test",
        "agentgrep.tui.session",
    }
    observed_roots = {str(trace["root"]) for trace in checked}
    missing_roots = sorted(required_roots - observed_roots)
    if missing_roots:
        message = f"missing required trace roots: {missing_roots}"
        raise AcceptanceCheckError(message)
    missing_cli_candidates = sorted(required_cli_candidate_ids - observed_cli_candidate_ids)
    if missing_cli_candidates:
        message = f"missing CLI candidate traces: {missing_cli_candidates}"
        raise AcceptanceCheckError(message)
    required_spans = {
        "agentgrep.benchmark.command",
        "agentgrep.benchmark.subprocess",
        "mcp.server.request",
        "agentgrep.mcp.server.lifecycle",
        "agentgrep.mcp.flush",
        "agentgrep.tui.lifecycle",
        "agentgrep.tui.shutdown",
    }
    missing_spans = sorted(required_spans - observed_span_names)
    if missing_spans:
        message = f"missing required spans: {missing_spans}"
        raise AcceptanceCheckError(message)
    if sqlite_trace_count == 0:
        message = f"no sqlite spans found in checked traces: {checked[:5]}"
        raise AcceptanceCheckError(message)
    if profile_engine_sqlite_trace_count == 0:
        message = f"no profile-engine sqlite spans found in checked traces: {checked[:5]}"
        raise AcceptanceCheckError(message)
    return {"count": len(checked), "traces": checked}


def trace_resource_attributes(trace_data: dict[str, object]) -> dict[str, object]:
    """Return merged OTel resource attributes from a Tempo trace payload."""
    attributes: dict[str, object] = {}
    for batch in _list_value(trace_data, "batches"):
        batch_dict = _dict_or_none(batch)
        if batch_dict is None:
            continue
        attributes.update(otel_attribute_map(_dict_value(batch_dict, "resource")))
    return attributes


def iter_trace_spans(trace_data: dict[str, object]) -> cabc.Iterator[dict[str, object]]:
    """Yield Tempo span dictionaries."""
    for batch in _list_value(trace_data, "batches"):
        batch_dict = _dict_or_none(batch)
        if batch_dict is None:
            continue
        for scope_span in _list_value(batch_dict, "scopeSpans"):
            scope_span_dict = _dict_or_none(scope_span)
            if scope_span_dict is None:
                continue
            for span in _list_value(scope_span_dict, "spans"):
                span_dict = _dict_or_none(span)
                if span_dict is not None:
                    yield span_dict


def _orphan_trace_spans(spans: cabc.Sequence[cabc.Mapping[str, object]]) -> list[dict[str, str]]:
    """Return child spans whose parent span id is missing from the trace."""
    span_ids = {
        str(span_id) for span in spans if (span_id := span.get("spanID") or span.get("spanId"))
    }
    orphans: list[dict[str, str]] = []
    for span in spans:
        parent_span_id = span.get("parentSpanId") or span.get("parentSpanID")
        if not parent_span_id:
            continue
        parent = str(parent_span_id)
        if parent in span_ids:
            continue
        orphans.append(
            {
                "span": str(span.get("name") or "<unknown>"),
                "span_id": str(span.get("spanID") or span.get("spanId") or "<unknown>"),
                "parent_span_id": parent,
            },
        )
    return orphans


def query_metrics(
    started_at: float,
    run_id: str,
    vcs_identity: dict[str, dict[str, str]],
) -> dict[str, object]:
    """Return Prometheus metric evidence."""
    required_metrics = {
        "agentgrep_span_count_total": (),
        "agentgrep_span_duration_seconds_count": (),
        "agentgrep_otel_cpu_loops_count": (
            'agentgrep_surface="engine"',
            'agentgrep_component="core"',
            'agentgrep_component_kind="in_process"',
        ),
        "agentgrep_otel_sqlite_total_count": ('agentgrep_surface="sqlite"',),
        "agentgrep_benchmark_subprocess_count_total": ('agentgrep_surface="benchmark"',),
    }
    fresh_metrics: dict[str, float] = {}
    vcs_matchers = _label_matchers(vcs_identity["labels"])
    for metric_name, matchers in required_metrics.items():
        timestamp = _latest_metric_timestamp(
            metric_name,
            run_id=run_id,
            matchers=(*matchers, *vcs_matchers),
        )
        if timestamp >= started_at - 5.0:
            fresh_metrics[metric_name] = timestamp
    if len(fresh_metrics) != len(required_metrics):
        missing = sorted(set(required_metrics) - set(fresh_metrics))
        message = f"missing fresh metrics: {missing}"
        raise AcceptanceCheckError(message)
    return {"fresh": fresh_metrics}


def query_logs(run_id: str, vcs_identity: dict[str, dict[str, str]]) -> dict[str, object]:
    """Return Loki log evidence with trace IDs."""
    query = urllib.parse.urlencode(
        {
            "query": _loki_log_query(run_id),
            "direction": "backward",
            "limit": "500",
        },
    )
    data = http_json(_loki_url(f"/loki/api/v1/query_range?{query}"))
    linked = []
    unlinked = []
    parser_errors = []
    unstructured = []
    missing_vcs = []
    expected_vcs_labels = vcs_identity["labels"]
    payload = _dict_value(data, "data")
    for stream in _list_value(payload, "result"):
        stream_dict = _dict_or_none(stream)
        if stream_dict is None:
            continue
        labels = _dict_value(stream_dict, "stream")
        if not _labels_match(labels, expected_vcs_labels):
            missing_vcs.append(_selected_labels(labels, expected_vcs_labels))
            continue
        for value in _list_value(stream_dict, "values"):
            body_fields = _loki_log_body_fields(value)
            fields = _loki_log_fields(labels, value)
            if fields.get("agentgrep_debug_session_id") != run_id:
                continue
            rendered = json.dumps({"labels": fields, "value": value}, sort_keys=True)
            if _loki_log_has_parser_error(fields):
                parser_errors.append(rendered[:500])
                continue
            if not body_fields:
                unstructured.append(rendered[:500])
                continue
            if _loki_log_has_trace_link(fields, value):
                linked.append(rendered[:500])
            else:
                unlinked.append(rendered[:500])
    if parser_errors:
        message = f"Loki JSON parser errors found: {parser_errors[:5]}"
        raise AcceptanceCheckError(message)
    if unstructured:
        message = f"unstructured agentgrep log bodies found: {unstructured[:5]}"
        raise AcceptanceCheckError(message)
    if unlinked:
        message = f"unlinked agentgrep logs found: {unlinked[:5]}"
        raise AcceptanceCheckError(message)
    if missing_vcs:
        message = f"agentgrep log streams missing VCS labels: {missing_vcs[:5]}"
        raise AcceptanceCheckError(message)
    if not linked:
        message = "no trace-linked agentgrep logs found"
        raise AcceptanceCheckError(message)
    return {"count": len(linked), "sample": linked[0]}


def _loki_log_query(run_id: str) -> str:
    """Return the LogQL query used for run-scoped structured log checks."""
    return (
        f'{{service_name=~"{SERVICE_NAME_PATTERN}"}} '
        f"| json | agentgrep_debug_session_id={json.dumps(run_id)}"
    )


def _loki_log_fields(
    stream_labels: cabc.Mapping[str, object],
    value: object,
) -> dict[str, object]:
    """Return stream labels plus JSON fields parsed from a Loki value."""
    fields = dict(stream_labels)
    fields.update(_loki_log_body_fields(value))
    return fields


def _loki_log_body_fields(value: object) -> dict[str, object]:
    """Return structured JSON fields parsed from a Loki log body."""
    if not isinstance(value, list | tuple) or len(value) < 2 or not isinstance(value[1], str):
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        body = json.loads(value[1])
        if isinstance(body, dict):
            return body
    return {}


def _loki_log_has_parser_error(fields: cabc.Mapping[str, object]) -> bool:
    """Return whether Loki reported a JSON parser error for a selected log."""
    return bool(fields.get("__error__") or fields.get("__error_details__"))


def _loki_log_has_trace_link(fields: cabc.Mapping[str, object], value: object) -> bool:
    """Return whether a Loki log record carries trace and span identifiers."""
    if any(
        fields.get(key)
        for key in (
            "trace_id",
            "traceid",
            "traceID",
            "span_id",
            "spanid",
            "spanID",
        )
    ):
        field_names = {key.lower() for key, field_value in fields.items() if field_value}
        return any(name in field_names for name in ("trace_id", "traceid")) and any(
            name in field_names for name in ("span_id", "spanid")
        )
    rendered = json.dumps({"labels": fields, "value": value}, sort_keys=True).lower()
    return "trace" in rendered and "span" in rendered


def query_profiles(run_id: str, vcs_identity: dict[str, dict[str, str]]) -> dict[str, object]:
    """Return Pyroscope profile evidence."""
    now_ms = int(time.time() * 1000)
    service_data = http_json(
        "http://localhost:4040/querier.v1.QuerierService/LabelValues",
        method="POST",
        body=_pyroscope_label_values_body(
            "service_name",
            run_id=run_id,
            vcs_identity=vcs_identity,
            now_ms=now_ms,
        ),
    )
    session_data = http_json(
        "http://localhost:4040/querier.v1.QuerierService/LabelValues",
        method="POST",
        body=_pyroscope_label_values_body(
            "agentgrep_debug_session_id",
            run_id=run_id,
            vcs_identity=vcs_identity,
            now_ms=now_ms,
        ),
    )
    vcs_label_data = {
        label: http_json(
            "http://localhost:4040/querier.v1.QuerierService/LabelValues",
            method="POST",
            body=_pyroscope_label_values_body(
                label,
                run_id=run_id,
                vcs_identity=vcs_identity,
                now_ms=now_ms,
            ),
        )
        for label in vcs_identity["labels"]
    }
    source_label_data = {
        label: http_json(
            "http://localhost:4040/querier.v1.QuerierService/LabelValues",
            method="POST",
            body=_pyroscope_label_values_body(
                label,
                run_id=run_id,
                vcs_identity=vcs_identity,
                now_ms=now_ms,
            ),
        )
        for label in PYROSCOPE_SOURCE_LABELS
    }
    rendered = json.dumps(
        {
            "service": service_data,
            "session": session_data,
            "source": source_label_data,
            "vcs": vcs_label_data,
        },
        sort_keys=True,
    )
    if "agentgrep" not in rendered:
        message = f"no agentgrep profile labels found: {rendered[:500]}"
        raise AcceptanceCheckError(message)
    if run_id not in rendered:
        message = f"no run-scoped profile labels found: {rendered[:500]}"
        raise AcceptanceCheckError(message)
    for label, expected in vcs_identity["labels"].items():
        if expected not in json.dumps(vcs_label_data[label], sort_keys=True):
            message = f"no VCS profile label {label}={expected}: {vcs_label_data[label]}"
            raise AcceptanceCheckError(message)
    for label, resource_key in PYROSCOPE_SOURCE_LABELS.items():
        expected = vcs_identity["resource"].get(resource_key)
        if expected is None:
            continue
        if str(expected) not in json.dumps(source_label_data[label], sort_keys=True):
            message = f"no source profile label {label}={expected}: {source_label_data[label]}"
            raise AcceptanceCheckError(message)
    return {
        "service": service_data,
        "session": session_data,
        "source": source_label_data,
        "vcs": vcs_label_data,
    }


def _pyroscope_label_values_body(
    name: str,
    *,
    run_id: str,
    vcs_identity: dict[str, dict[str, str]],
    now_ms: int,
) -> dict[str, object]:
    """Return a run- and source-scoped Pyroscope LabelValues request body."""
    matchers: dict[str, str] = {
        "agentgrep_debug_session_id": run_id,
        "service_name": f"~{SERVICE_NAME_PATTERN}",
    }
    matchers.update(vcs_identity["labels"])
    repository = vcs_identity["resource"].get("vcs.repository.url.full")
    if repository is not None:
        matchers["service_repository"] = repository
    git_ref = vcs_identity["resource"].get("vcs.ref.head.revision")
    if git_ref is not None:
        matchers["service_git_ref"] = git_ref
    selector = (
        "{" + ",".join(_label_matcher(key, value) for key, value in sorted(matchers.items())) + "}"
    )
    return {
        "start": now_ms - 60 * 60 * 1000,
        "end": now_ms,
        "name": name,
        "matchers": [selector],
    }


def wait_for(callback: cabc.Callable[[], object], deadline: float, label: str) -> object:
    """Run ``callback`` until it succeeds or the deadline expires."""
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return callback()
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    message = f"timed out waiting for {label}: {last_error}"
    raise AcceptanceCheckError(message) from last_error


def http_text(url: str) -> str:
    """Fetch text from ``url``."""
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8", errors="replace")


def http_json(url: str, *, method: str = "GET", body: object | None = None) -> dict[str, object]:
    """Fetch JSON from ``url``."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message = f"{url} returned {exc.code}: {detail}"
        raise AcceptanceCheckError(message) from exc
    if not isinstance(parsed, dict):
        message = f"{url} returned non-object JSON"
        raise AcceptanceCheckError(message)
    return parsed


def _loki_url(path: str) -> str:
    """Return a Loki API URL, defaulting to Grafana's datasource proxy."""
    return os.environ.get("AGENTGREP_LOKI_BASE_URL", DEFAULT_LOKI_BASE_URL).rstrip("/") + path


def _prometheus_url(path: str) -> str:
    """Return a Prometheus API URL, defaulting to Grafana's datasource proxy."""
    return (
        os.environ.get("AGENTGREP_PROMETHEUS_BASE_URL", DEFAULT_PROMETHEUS_BASE_URL).rstrip("/")
        + path
    )


def _latest_metric_timestamp(
    metric_name: str,
    *,
    run_id: str | None = None,
    matchers: tuple[str, ...] = (),
) -> float:
    """Return the newest sample timestamp for ``metric_name``."""
    labels = [*matchers]
    if run_id is not None:
        labels.insert(0, _label_matcher("agentgrep_debug_session_id", run_id))
    query = metric_name if not labels else f"{metric_name}{{{','.join(labels)}}}"
    params = urllib.parse.urlencode({"query": query})
    data = http_json(_prometheus_url(f"/api/v1/query?{params}"))
    payload = _dict_value(data, "data")
    timestamps: list[float] = []
    for result in _list_value(payload, "result"):
        result_dict = _dict_or_none(result)
        if result_dict is None:
            continue
        value = _list_value(result_dict, "value")
        if not value:
            continue
        raw_timestamp = value[0]
        if not isinstance(raw_timestamp, str | int | float):
            continue
        with contextlib.suppress(TypeError, ValueError):
            timestamps.append(float(raw_timestamp))
    if not timestamps:
        return 0.0
    return max(timestamps)


def _label_matchers(labels: cabc.Mapping[str, object]) -> tuple[str, ...]:
    """Return PromQL equality matchers for scalar labels."""
    return tuple(_label_matcher(key, value) for key, value in sorted(labels.items()))


def _label_matcher(key: str, value: object) -> str:
    """Return one PromQL equality matcher with JSON string escaping."""
    if isinstance(value, str) and value.startswith("~"):
        return f"{key}=~{json.dumps(value[1:])}"
    return f"{key}={json.dumps(str(value))}"


def _labels_match(
    observed: cabc.Mapping[str, object],
    expected: cabc.Mapping[str, str],
) -> bool:
    """Return whether all expected labels match the observed mapping."""
    return all(str(observed.get(key)) == value for key, value in expected.items())


def _selected_labels(
    observed: cabc.Mapping[str, object],
    expected: cabc.Mapping[str, str],
) -> dict[str, object]:
    """Return just the expected keys from an observed label mapping."""
    return {key: observed.get(key) for key in expected}


def otel_attribute_map(container: cabc.Mapping[str, object]) -> dict[str, object]:
    """Return an OpenTelemetry JSON ``attributes`` list as a mapping."""
    mapped: dict[str, object] = {}
    for raw_attribute in _list_value(container, "attributes"):
        attribute = _dict_or_none(raw_attribute)
        if attribute is None:
            continue
        key = attribute.get("key")
        if not isinstance(key, str):
            continue
        mapped[key] = otel_value(attribute.get("value"))
    return mapped


def otel_value(value: object) -> object:
    """Return a scalar value from an OpenTelemetry JSON value object."""
    value_dict = _dict_or_none(value)
    if value_dict is None:
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value_dict:
            return value_dict[key]
    return value


def _list_value(data: cabc.Mapping[str, object], key: str) -> list[object]:
    """Return a JSON list field or an empty list."""
    value = data.get(key)
    if isinstance(value, list):
        return t.cast("list[object]", value)
    return []


def _dict_value(data: cabc.Mapping[str, object], key: str) -> dict[str, object]:
    """Return a JSON object field or an empty dict."""
    value = data.get(key)
    return {} if not isinstance(value, dict) else t.cast("dict[str, object]", value)


def _dict_or_none(value: object) -> dict[str, object] | None:
    """Return ``value`` as a typed JSON object when possible."""
    if isinstance(value, dict):
        return t.cast("dict[str, object]", value)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
