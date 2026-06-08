"""Sandbox backends for command documentation examples."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import subprocess
import tempfile
import typing as t

from .core import DocumentationExample, blocked_command_error, redact_text


@dataclasses.dataclass(frozen=True, slots=True)
class SandboxSeed:
    """File or directory copied into each temporary sandbox."""

    source: pathlib.Path
    target: pathlib.Path


class SandboxBackend(t.Protocol):
    """Protocol for sandboxed command execution."""

    def run_script(
        self,
        script: str,
        *,
        example: DocumentationExample,
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell script for ``example``."""


class TempHomeSandbox:
    """Run shell examples under a temporary home and cwd.

    This protects developer data by redirecting user/config roots. It is not a
    hostile-code security boundary.
    """

    _ROOT_ENV_VARS = (
        "HOME",
        "USERPROFILE",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "CLAUDE_CONFIG_DIR",
        "GEMINI_CLI_HOME",
        "GROK_HOME",
        "PI_CODING_AGENT_DIR",
        "PI_CODING_AGENT_SESSION_DIR",
        "OPENCODE_DB",
        "OPENCODE_CONFIG_DIR",
    )
    _BLOCKED_WORDS = frozenset(
        {
            "curl",
            "docker",
            "fastmcp",
            "git clone",
            "claude mcp",
            "agentgrep-mcp",
            "agentgrep ui",
            "pip install",
            "pipx",
            "scripts/benchmark.py",
            "scripts/profile_engine.py",
            "sudo",
            "--ui",
            "uv pip install",
            "uv sync",
            "wget",
        },
    )

    def __init__(
        self,
        *,
        project_root: pathlib.Path | None = None,
        timeout: float = 15.0,
        cwd: pathlib.Path | None = None,
        seeds: t.Iterable[SandboxSeed] = (),
        extra_env: t.Mapping[str, str] | None = None,
        blocked_words: t.Iterable[str] | None = None,
    ) -> None:
        """Create a temporary-home sandbox."""
        self.project_root = (project_root or pathlib.Path.cwd()).expanduser().resolve()
        self.timeout = timeout
        self.cwd = cwd.expanduser().resolve() if cwd is not None else None
        self.seeds = tuple(seeds)
        self.extra_env = dict(extra_env or {})
        if blocked_words is None:
            self.blocked_words = self._BLOCKED_WORDS
        else:
            self.blocked_words = frozenset(blocked_words)

    def run_script(
        self,
        script: str,
        *,
        example: DocumentationExample,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``script`` under a temporary home."""
        self._check_policy(script)
        with tempfile.TemporaryDirectory(prefix="pytest-documentation-") as sandbox_text:
            sandbox_root = pathlib.Path(sandbox_text)
            home = sandbox_root / "home"
            cwd = self.cwd or sandbox_root / "work"
            home.mkdir()
            cwd.mkdir(parents=True, exist_ok=True)
            self._copy_seeds(home)
            env = self._build_env(home)
            completed = subprocess.run(
                script,
                cwd=cwd,
                env=env,
                executable="/bin/sh",
                shell=True,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        return subprocess.CompletedProcess(
            args=redact_text(str(completed.args), project_root=self.project_root),
            returncode=completed.returncode,
            stdout=redact_text(completed.stdout, project_root=self.project_root),
            stderr=redact_text(completed.stderr, project_root=self.project_root),
        )

    def _check_policy(self, script: str) -> None:
        """Reject commands that should not run in docs tests."""
        lowered = script.lower()
        for blocked in self.blocked_words:
            if blocked in lowered:
                raise blocked_command_error(blocked)
        home = pathlib.Path.home().as_posix()
        if home and home in script:
            label = "real home path"
            raise blocked_command_error(label)

    def _copy_seeds(self, home: pathlib.Path) -> None:
        """Copy configured seed files into the sandbox home."""
        for seed in self.seeds:
            target = home / seed.target
            target.parent.mkdir(parents=True, exist_ok=True)
            if seed.source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(seed.source, target)
            else:
                shutil.copy2(seed.source, target)

    def _build_env(self, home: pathlib.Path) -> dict[str, str]:
        """Build a subprocess environment with redirected roots."""
        env = {key: value for key, value in os.environ.items() if not _looks_sensitive(key)}
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["CODEX_HOME"] = str(home / ".codex")
        env["CODEX_SQLITE_HOME"] = str(home / ".codex")
        env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        env["GEMINI_CLI_HOME"] = str(home / ".gemini")
        env["GROK_HOME"] = str(home / ".grok")
        env["PI_CODING_AGENT_DIR"] = str(home / ".pi" / "agent")
        env["PI_CODING_AGENT_SESSION_DIR"] = str(home / ".pi" / "agent" / "sessions")
        env["OPENCODE_DB"] = str(home / ".local" / "share" / "opencode" / "opencode.db")
        env["OPENCODE_CONFIG_DIR"] = str(home / ".config" / "opencode")
        env["PYTEST_DOCUMENTATION_SANDBOX"] = "1"
        env.update(self.extra_env)
        return env


def _looks_sensitive(key: str) -> bool:
    """Return whether an env var key is likely secret-bearing."""
    upper = key.upper()
    return any(token in upper for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH"))
