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


def test_lgtm_grafana_datasource_forwards_pyroscope_git_session() -> None:
    """Grafana must forward Pyroscope's GitHub session cookie."""
    content = otel_acceptance.LGTM_GRAFANA_DATASOURCES_CONFIG.read_text(encoding="utf-8")

    assert "grafana-pyroscope-datasource" in content
    assert "keepCookies: [pyroscope_git_session]" in content
