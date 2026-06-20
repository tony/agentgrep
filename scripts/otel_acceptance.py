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
DEFAULT_LOKI_BASE_URL = "http://localhost:3000/api/datasources/proxy/uid/loki"
DEFAULT_PROMETHEUS_BASE_URL = "http://localhost:3000/api/datasources/proxy/uid/prometheus"
APPROVED_ROOTS = {
    "agentgrep.cli.invocation",
    "agentgrep.cli.interactive_session",
    "agentgrep.tui.session",
    "agentgrep.mcp.request",
    "agentgrep.mcp.tool",
    "agentgrep.benchmark.run",
    "agentgrep.profile_engine.run",
    "agentgrep.pytest.session",
    "agentgrep.pytest.test",
    "agentgrep.otel.smoke",
}


class AcceptanceCheckError(RuntimeError):
    """Raised when live LGTM evidence is not yet available."""


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
    run_workloads(args.run_id)
    deadline = time.monotonic() + args.timeout
    evidence: dict[str, object] = {}
    evidence["traces"] = wait_for(lambda: query_traces(args.run_id), deadline, "traces")
    evidence["metrics"] = wait_for(
        lambda: query_metrics(started_at, args.run_id),
        deadline,
        "metrics",
    )
    evidence["logs"] = wait_for(lambda: query_logs(args.run_id), deadline, "logs")
    evidence["profiles"] = wait_for(lambda: query_profiles(args.run_id), deadline, "profiles")
    print(json.dumps({"run_id": args.run_id, "evidence": evidence}, indent=2, sort_keys=True))
    return 0


def start_stack() -> None:
    """Start the local LGTM container if needed."""
    inspect = subprocess.run(
        ["docker", "inspect", CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode == 0:
        return
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
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
            "grafana/otel-lgtm:latest",
        ],
        check=True,
    )


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
        marker = f"{run_id} prompt"
        session = (
            pathlib.Path(temp_home) / ".codex" / "sessions" / "2026" / "01" / "01" / "session.jsonl"
        )
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
        cursor_db = pathlib.Path(temp_home) / ".cursor" / "state.vscdb"
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
        subprocess.run(
            [
                sys.executable,
                "-m",
                "agentgrep",
                "--help",
            ],
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=True,
            capture_output=True,
            text=True,
        )
        parse_error = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentgrep",
                "grep",
                "--invert-match",
                run_id,
            ],
            cwd=ROOT,
            env={**env, "HOME": temp_home},
            check=False,
            capture_output=True,
            text=True,
        )
        if parse_error.returncode != 2:
            message = f"parse-error workload exited {parse_error.returncode}: {parse_error.stderr}"
            raise AcceptanceCheckError(message)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "agentgrep",
                "search",
                "--agent",
                "codex",
                "--scope",
                "prompts",
                "--limit",
                "5",
                run_id,
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


def query_traces(run_id: str) -> dict[str, object]:
    """Return trace evidence and reject single-root traces."""
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
    single_root_traces: list[str] = []
    bad_root_traces: list[dict[str, object]] = []
    sqlite_trace_count = 0
    profile_engine_sqlite_trace_count = 0
    observed_span_names: set[str] = set()
    for trace_id in trace_ids:
        trace_data = http_json(f"http://localhost:3200/api/traces/{trace_id}")
        spans = list(iter_trace_spans(trace_data))
        span_names = [str(span.get("name")) for span in spans if span.get("name")]
        observed_span_names.update(span_names)
        sqlite_span_names = sorted(
            {name for name in span_names if name.startswith("agentgrep.sqlite.")}
        )
        root_spans = [span for span in spans if not span.get("parentSpanId")]
        root_name = None if not root_spans else root_spans[0].get("name")
        if len(spans) == 1 and root_spans:
            single_root_traces.append(trace_id)
            continue
        if root_name not in APPROVED_ROOTS:
            bad_root_traces.append({"trace_id": trace_id, "root": root_name})
            continue
        if len(spans) > 1:
            checked.append(
                {
                    "trace_id": trace_id,
                    "span_count": len(spans),
                    "root": root_name,
                    "sqlite_spans": sqlite_span_names[:10],
                }
            )
            if sqlite_span_names:
                sqlite_trace_count += 1
                if root_name == "agentgrep.profile_engine.run":
                    profile_engine_sqlite_trace_count += 1
    if single_root_traces:
        message = f"single-root traces found: {single_root_traces[:5]}"
        raise AcceptanceCheckError(message)
    if bad_root_traces:
        message = f"unexpected root traces found: {bad_root_traces[:5]}"
        raise AcceptanceCheckError(message)
    if not checked:
        message = f"no app-rooted multi-span traces found; candidates={trace_ids[:5]}"
        raise AcceptanceCheckError(message)
    required_roots = {
        "agentgrep.otel.smoke",
        "agentgrep.benchmark.run",
        "agentgrep.cli.invocation",
        "agentgrep.profile_engine.run",
        "agentgrep.pytest.test",
    }
    observed_roots = {str(trace["root"]) for trace in checked}
    missing_roots = sorted(required_roots - observed_roots)
    if missing_roots:
        message = f"missing required trace roots: {missing_roots}"
        raise AcceptanceCheckError(message)
    required_spans = {
        "agentgrep.benchmark.command",
        "agentgrep.benchmark.subprocess",
        "agentgrep.mcp.request",
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


def query_metrics(started_at: float, run_id: str) -> dict[str, object]:
    """Return Prometheus metric evidence."""
    required_metrics = {
        "agentgrep_span_count_total": (),
        "agentgrep_span_duration_seconds_count": (),
        "agentgrep_otel_cpu_loops_count": ('agentgrep_surface="engine"',),
        "agentgrep_otel_sqlite_total_count": ('agentgrep_surface="sqlite"',),
        "agentgrep_benchmark_subprocess_count_total": ('agentgrep_surface="benchmark"',),
    }
    fresh_metrics: dict[str, float] = {}
    for metric_name, matchers in required_metrics.items():
        timestamp = _latest_metric_timestamp(metric_name, run_id=run_id, matchers=matchers)
        if timestamp >= started_at - 5.0:
            fresh_metrics[metric_name] = timestamp
    if len(fresh_metrics) != len(required_metrics):
        missing = sorted(set(required_metrics) - set(fresh_metrics))
        message = f"missing fresh metrics: {missing}"
        raise AcceptanceCheckError(message)
    return {"fresh": fresh_metrics}


def query_logs(run_id: str) -> dict[str, object]:
    """Return Loki log evidence with trace IDs."""
    query = urllib.parse.urlencode(
        {
            "query": '{service_name="agentgrep"}',
            "direction": "backward",
            "limit": "500",
        },
    )
    data = http_json(_loki_url(f"/loki/api/v1/query_range?{query}"))
    linked = []
    unlinked = []
    payload = _dict_value(data, "data")
    for stream in _list_value(payload, "result"):
        stream_dict = _dict_or_none(stream)
        if stream_dict is None:
            continue
        labels = _dict_value(stream_dict, "stream")
        if labels.get("agentgrep_debug_session_id") != run_id:
            continue
        for value in _list_value(stream_dict, "values"):
            rendered = json.dumps({"labels": labels, "value": value}, sort_keys=True)
            if "trace" in rendered.lower() and "span" in rendered.lower():
                linked.append(rendered[:500])
            else:
                unlinked.append(rendered[:500])
    if unlinked:
        message = f"unlinked agentgrep logs found: {unlinked[:5]}"
        raise AcceptanceCheckError(message)
    if not linked:
        message = "no trace-linked agentgrep logs found"
        raise AcceptanceCheckError(message)
    return {"count": len(linked), "sample": linked[0]}


def query_profiles(run_id: str) -> dict[str, object]:
    """Return Pyroscope profile evidence."""
    now_ms = int(time.time() * 1000)
    service_body = {
        "start": now_ms - 60 * 60 * 1000,
        "end": now_ms,
        "name": "service_name",
    }
    service_data = http_json(
        "http://localhost:4040/querier.v1.QuerierService/LabelValues",
        method="POST",
        body=service_body,
    )
    session_body = {
        "start": now_ms - 60 * 60 * 1000,
        "end": now_ms,
        "name": "agentgrep_debug_session_id",
    }
    session_data = http_json(
        "http://localhost:4040/querier.v1.QuerierService/LabelValues",
        method="POST",
        body=session_body,
    )
    rendered = json.dumps({"service": service_data, "session": session_data}, sort_keys=True)
    if "agentgrep" not in rendered:
        message = f"no agentgrep profile labels found: {rendered[:500]}"
        raise AcceptanceCheckError(message)
    if run_id not in rendered:
        message = f"no run-scoped profile labels found: {rendered[:500]}"
        raise AcceptanceCheckError(message)
    return {"service": service_data, "session": session_data}


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
        labels.insert(0, f'agentgrep_debug_session_id="{run_id}"')
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
