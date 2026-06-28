"""Generate all agentgrep telemetry signals for local LGTM acceptance."""

from __future__ import annotations

import argparse
import logging
import math
import os
import pathlib
import sqlite3
import time

from agentgrep import _telemetry


def main() -> int:
    """Run the telemetry smoke workload."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seconds", type=float, default=4.0)
    args = parser.parse_args()

    os.environ.setdefault("AGENTGREP_DEBUG_SESSION_ID", args.run_id)
    telemetry = _telemetry.setup(
        mode="live",
        repo_root=pathlib.Path(__file__).resolve().parents[1],
        service_name="agentgrep-otel-smoke",
    )
    logger = logging.getLogger("agentgrep.otel.smoke")
    try:
        with _telemetry.span(
            "agentgrep.otel.smoke",
            agentgrep_surface="otel",
            agentgrep_run_id=args.run_id,
        ):
            logger.info(
                "otel smoke started",
                extra={
                    "agentgrep_surface": "otel",
                    "agentgrep_operation": "otel.smoke",
                    "agentgrep_run_id": args.run_id,
                },
            )
            with _telemetry.span("agentgrep.otel.smoke.sqlite", agentgrep_surface="otel"):
                connection = sqlite3.connect(
                    ":memory:",
                    factory=_telemetry.sqlite_connection_factory(),
                )
                try:
                    connection.execute("create table smoke (value integer)")
                    connection.executemany(
                        "insert into smoke (value) values (?)",
                        [(index,) for index in range(100)],
                    )
                    total = connection.execute("select sum(value) from smoke").fetchone()[0]
                    _telemetry.record_metric(
                        "agentgrep.otel.sqlite_total",
                        float(total),
                        agentgrep_surface="otel",
                    )
                finally:
                    connection.close()
            with _telemetry.span("agentgrep.otel.smoke.cpu", agentgrep_surface="otel"):
                deadline = time.monotonic() + args.seconds
                loops = 0
                accumulator = 0.0
                while time.monotonic() < deadline:
                    accumulator += math.sqrt((loops % 10_000) + 1)
                    loops += 1
                _telemetry.record_metric(
                    "agentgrep.otel.cpu_loops",
                    loops,
                    agentgrep_surface="otel",
                )
                _telemetry.set_span_attribute("agentgrep_cpu_loops", loops)
                _telemetry.set_span_attribute("agentgrep_cpu_accumulator", accumulator)
            logger.info(
                "otel smoke completed",
                extra={
                    "agentgrep_surface": "otel",
                    "agentgrep_operation": "otel.smoke",
                    "agentgrep_run_id": args.run_id,
                },
            )
    finally:
        telemetry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
