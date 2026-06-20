"""Tests for scripts/otel_acceptance.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import typing as t

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
                stdout=json.dumps([{"State": {"Running": False}}]),
            )
        if command == ["docker", "start", otel_acceptance.CONTAINER_NAME]:
            return subprocess.CompletedProcess(command, 0, stdout=otel_acceptance.CONTAINER_NAME)
        msg = f"unexpected command: {command}"
        raise AssertionError(msg)

    monkeypatch.setattr(otel_acceptance.subprocess, "run", fake_run)

    otel_acceptance.start_stack()

    assert calls == [
        ["docker", "inspect", otel_acceptance.CONTAINER_NAME],
        ["docker", "start", otel_acceptance.CONTAINER_NAME],
    ]
