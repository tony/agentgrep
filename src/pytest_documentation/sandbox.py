"""Sandbox backends for command documentation examples."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import tempfile
import typing as t

from .core import DocumentationExample, EvaluationFailureKind, blocked_command_error, redact_text


@dataclasses.dataclass(frozen=True, slots=True)
class SandboxSeed:
    """File or directory copied into each temporary sandbox."""

    source: pathlib.Path
    target: pathlib.Path


@dataclasses.dataclass(frozen=True, slots=True)
class SandboxCommandPlan:
    """Executable plan for one shell documentation example."""

    original_script: str
    script: str
    reason: str = "literal"
    execute: bool = True
    accept_empty_result: bool = False
    failure_kind: EvaluationFailureKind | None = None
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class SandboxExecution:
    """Completed sandbox run plus policy metadata."""

    plan: SandboxCommandPlan
    completed: subprocess.CompletedProcess[str]
    message: str = ""
    failure_kind: EvaluationFailureKind | None = None
    accept_empty_result: bool = False


class SandboxBackend(t.Protocol):
    """Protocol for sandboxed command execution."""

    def run_script(
        self,
        script: str,
        *,
        example: DocumentationExample,
    ) -> SandboxExecution:
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
            "pipx",
            "sudo",
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
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def run_script(
        self,
        script: str,
        *,
        example: DocumentationExample,
    ) -> SandboxExecution:
        """Run ``script`` under temporary home, cache, and project roots."""
        sandbox_root, home, project, shim_bin = self._ensure_world()
        env = self._build_env(
            home,
            sandbox_root=sandbox_root,
            project=project,
            shim_bin=shim_bin,
        )
        plan = self._plan_script(script, sandbox_root=sandbox_root, project=project)
        cwd = self._sandbox_cwd(project)
        if plan.execute:
            completed = subprocess.run(
                plan.script,
                cwd=cwd,
                env=env,
                executable="/bin/sh",
                shell=True,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        else:
            completed = subprocess.CompletedProcess(
                args=plan.script,
                returncode=plan.returncode,
                stdout=plan.stdout,
                stderr=plan.stderr,
            )
        redacted_completed = subprocess.CompletedProcess(
            args=redact_text(str(completed.args), project_root=self.project_root),
            returncode=completed.returncode,
            stdout=redact_text(completed.stdout, project_root=self.project_root),
            stderr=redact_text(completed.stderr, project_root=self.project_root),
        )
        return SandboxExecution(
            plan=plan,
            completed=redacted_completed,
            message=plan.reason if plan.reason != "literal" else "",
            failure_kind=plan.failure_kind,
            accept_empty_result=plan.accept_empty_result,
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

    def _ensure_world(self) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        """Return a reusable temp world for this sandbox instance."""
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="pytest-documentation-",
            )
            sandbox_root = pathlib.Path(self._temporary_directory.name)
            home = sandbox_root / "home"
            project = sandbox_root / "project" / self.project_root.name
            shim_bin = sandbox_root / "bin"
            home.mkdir()
            shim_bin.mkdir()
            self._prepare_project(project)
            (project / ".tmp").mkdir(exist_ok=True)
            self._copy_seeds(home)
            self._write_shims(shim_bin)
        sandbox_root = pathlib.Path(self._temporary_directory.name)
        return (
            sandbox_root,
            sandbox_root / "home",
            sandbox_root / "project" / self.project_root.name,
            sandbox_root / "bin",
        )

    def _prepare_project(self, project: pathlib.Path) -> None:
        """Create an isolated project tree for relative command side effects."""
        project.parent.mkdir(parents=True, exist_ok=True)
        has_git_metadata = (self.project_root / ".git").exists()
        if has_git_metadata and not self._project_is_dirty():
            completed = subprocess.run(
                (
                    "git",
                    "clone",
                    "--local",
                    "--no-hardlinks",
                    "--quiet",
                    str(self.project_root),
                    str(project),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode == 0:
                return
        self._copy_project(project)
        if has_git_metadata:
            self._initialize_copied_git_repo(project)

    def _project_is_dirty(self) -> bool:
        """Return whether the source git worktree has uncommitted content."""
        completed = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=self.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            return True
        return bool(completed.stdout.strip())

    def _copy_project(self, project: pathlib.Path) -> None:
        """Copy the source project without git metadata or generated caches."""
        ignore = shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".tmp",
            ".venv",
            "__pycache__",
            "_build",
        )
        shutil.copytree(self.project_root, project, ignore=ignore)

    def _initialize_copied_git_repo(self, project: pathlib.Path) -> None:
        """Create a harmless git HEAD for copied dirty projects."""
        init = subprocess.run(
            ("git", "init", "--quiet"),
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
        )
        if init.returncode != 0:
            return
        _ = subprocess.run(
            (
                "git",
                "-c",
                "user.name=pytest-documentation",
                "-c",
                "user.email=pytest-documentation@example.invalid",
                "commit",
                "--allow-empty",
                "--quiet",
                "-m",
                "sandbox project",
            ),
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
        )

    def _sandbox_cwd(self, project: pathlib.Path) -> pathlib.Path:
        """Return the command cwd inside the isolated project tree."""
        if self.cwd is None:
            return project
        try:
            relative = self.cwd.relative_to(self.project_root)
        except ValueError:
            return project
        return project / relative

    def _plan_script(
        self,
        script: str,
        *,
        sandbox_root: pathlib.Path,
        project: pathlib.Path,
    ) -> SandboxCommandPlan:
        """Convert known stateful commands into safe executable forms."""
        self._check_policy(script)
        normalized = _normalize_script(script)
        words = _split_script_words(script)
        if _is_standalone_cd_agentgrep(script):
            return SandboxCommandPlan(
                original_script=script,
                script=":",
                reason="standalone sequence step accepted: cd agentgrep",
                execute=False,
            )
        if words[:2] == ["uv", "sync"]:
            safe_words = _append_missing(words, ("--dry-run", "--frozen"))
            return SandboxCommandPlan(
                original_script=script,
                script=shlex.join(safe_words),
                reason="uv sync dry-run",
            )
        if words[:3] == ["uv", "pip", "install"]:
            target = sandbox_root / "uv-pip-target"
            safe_words = [*words[:3], "--dry-run", "--target", str(target), *words[3:]]
            return SandboxCommandPlan(
                original_script=script,
                script=shlex.join(safe_words),
                reason="uv pip install dry-run",
            )
        if words[:2] == ["pip", "install"]:
            safe_words = words[:2] + _append_missing(words[2:], ("--dry-run",))
            return SandboxCommandPlan(
                original_script=script,
                script=shlex.join(safe_words),
                reason="pip install dry-run",
            )
        if words[:2] == ["git", "clone"]:
            return SandboxCommandPlan(
                original_script=script,
                script=shlex.join(
                    [
                        "git",
                        "clone",
                        "--local",
                        "--no-hardlinks",
                        str(self.project_root),
                        "agentgrep",
                    ],
                ),
                reason="git clone redirected to local temp project",
            )
        if "claude mcp" in normalized:
            return SandboxCommandPlan(
                original_script=script,
                script=script,
                reason="claude mcp add shim",
            )
        if "scripts/benchmark.py analyze" in normalized:
            return SandboxCommandPlan(
                original_script=script,
                script=":",
                reason="benchmark analysis requires a prior artifact and is accepted as a recipe",
                execute=False,
            )
        if _is_ref_dependent_benchmark(words):
            return SandboxCommandPlan(
                original_script=script,
                script=":",
                reason="benchmark ref-dependent recipe accepted without resolving local refs",
                execute=False,
            )
        if _is_benchmark_run_or_compare(words):
            safe_words = _append_missing(
                words,
                ("--dry-run", "--allow-dirty", "--no-sync", "--no-progress"),
            )
            return SandboxCommandPlan(
                original_script=script,
                script=shlex.join(safe_words),
                reason="benchmark dry-run",
            )
        if "fastmcp run" in normalized or "fastmcp inspect" in normalized:
            return SandboxCommandPlan(
                original_script=script,
                script=":",
                reason="fastmcp command covered by fastmcp.json config example",
                execute=False,
            )
        if _is_agentgrep_mcp_command(words):
            return SandboxCommandPlan(
                original_script=script,
                script=(
                    "uv run python -c "
                    + shlex.quote(
                        "from agentgrep.mcp import build_mcp_server; "
                        "build_mcp_server(); "
                        "print('agentgrep-mcp startup ok')",
                    )
                ),
                reason="agentgrep-mcp startup probe",
            )
        if _is_ui_command(normalized):
            return SandboxCommandPlan(
                original_script=script,
                script=":",
                reason="interactive ui command accepted as bounded smoke policy",
                execute=False,
            )
        return SandboxCommandPlan(
            original_script=script,
            script=script,
            accept_empty_result=_accepts_data_dependent_empty_result(script),
        )

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

    def _build_env(
        self,
        home: pathlib.Path,
        *,
        sandbox_root: pathlib.Path,
        project: pathlib.Path,
        shim_bin: pathlib.Path,
    ) -> dict[str, str]:
        """Build a subprocess environment with redirected roots."""
        env = {key: value for key, value in os.environ.items() if not _looks_sensitive(key)}
        env.pop("VIRTUAL_ENV", None)
        env.pop("CONDA_PREFIX", None)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["CODEX_HOME"] = str(home / ".codex")
        env["CODEX_SQLITE_HOME"] = str(home / ".codex")
        env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        env["GEMINI_CLI_HOME"] = str(home / ".gemini")
        env["GROK_HOME"] = str(home / ".grok")
        env["PI_CODING_AGENT_DIR"] = str(home / ".pi" / "agent")
        env["PI_CODING_AGENT_SESSION_DIR"] = str(home / ".pi" / "agent" / "sessions")
        env["OPENCODE_DB"] = str(home / ".local" / "share" / "opencode" / "opencode.db")
        env["OPENCODE_CONFIG_DIR"] = str(home / ".config" / "opencode")
        env["PIP_CACHE_DIR"] = str(sandbox_root / "pip-cache")
        env["PYTHONPYCACHEPREFIX"] = str(sandbox_root / "pycache")
        env["PYTEST_DOCUMENTATION_SANDBOX"] = "1"
        env["UV_CACHE_DIR"] = str(sandbox_root / "uv-cache")
        env["UV_PROJECT_ENVIRONMENT"] = str(project / ".venv-docs-sandbox")
        env["PATH"] = os.pathsep.join((str(shim_bin), env.get("PATH", "")))
        env.update(self.extra_env)
        return env

    def _write_shims(self, shim_bin: pathlib.Path) -> None:
        """Write command shims used by safe documentation plans."""
        _write_claude_shim(shim_bin / "claude")
        _write_pip_shim(shim_bin / "pip")


def _looks_sensitive(key: str) -> bool:
    """Return whether an env var key is likely secret-bearing."""
    upper = key.upper()
    return any(token in upper for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH"))


def _normalize_script(script: str) -> str:
    """Normalize shell text for conservative command-shape checks."""
    return " ".join(script.lower().split())


def _split_script_words(script: str) -> list[str]:
    """Split a simple shell command, returning an empty list for complex syntax."""
    try:
        return shlex.split(script)
    except ValueError:
        return []


def _append_missing(words: t.Sequence[str], additions: t.Iterable[str]) -> list[str]:
    """Append option words that are not already present."""
    safe_words = list(words)
    for addition in additions:
        if addition not in safe_words:
            safe_words.append(addition)
    return safe_words


def _is_standalone_cd_agentgrep(script: str) -> bool:
    """Return whether a script is exactly an install-sequence ``cd`` step."""
    return script.strip() == "cd agentgrep"


def _is_benchmark_run_or_compare(words: t.Sequence[str]) -> bool:
    """Return whether ``words`` invoke a benchmark run-shaped command."""
    return (
        len(words) >= 4
        and words[:3] == ["uv", "run", "scripts/benchmark.py"]
        and words[3]
        in {
            "run",
            "compare",
        }
    )


def _is_ref_dependent_benchmark(words: t.Sequence[str]) -> bool:
    """Return whether a benchmark command depends on non-current git refs."""
    if len(words) < 4 or words[:3] != ["uv", "run", "scripts/benchmark.py"]:
        return False
    if words[3] == "compare":
        return True
    ref_dependent_tokens = {
        "--head-vs-trunk",
        "--from-trunk-back",
        "--tags",
        "abc1234,def5678",
        "master..HEAD",
        "trunk",
    }
    return any(word in ref_dependent_tokens for word in words)


def _is_agentgrep_mcp_command(words: t.Sequence[str]) -> bool:
    """Return whether ``words`` directly start the agentgrep MCP server."""
    if words == ["agentgrep-mcp"]:
        return True
    return words[:3] == ["uv", "run", "agentgrep-mcp"]


def _is_ui_command(normalized: str) -> bool:
    """Return whether a command starts the interactive UI."""
    return (
        normalized.startswith("agentgrep ui")
        or " --ui" in normalized
        or normalized.endswith(" --ui")
    )


def _accepts_data_dependent_empty_result(script: str) -> bool:
    """Return whether ``rc=1`` means an acceptable fixture-dependent no-match."""
    normalized = _normalize_script(script)
    if "path:~" in normalized or "xargs" in normalized:
        return False
    return any(
        token in normalized
        for token in (
            "agentgrep search",
            "agentgrep grep",
            "agentgrep find",
            "uv run agentgrep search",
            "uv run agentgrep grep",
            "uv run agentgrep find",
        )
    )


def _write_claude_shim(path: pathlib.Path) -> None:
    """Write a small ``claude mcp add`` recorder."""
    _write_executable(
        path,
        """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import sys

home = pathlib.Path(os.environ["HOME"])
record_path = home / ".claude" / "mcp-additions.jsonl"
record_path.parent.mkdir(parents=True, exist_ok=True)
with record_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:]}, sort_keys=True) + "\\n")
print("recorded claude mcp configuration")
""",
    )


def _write_pip_shim(path: pathlib.Path) -> None:
    """Write a ``pip install --dry-run`` shim for environments without pip."""
    _write_executable(
        path,
        """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import sys

home = pathlib.Path(os.environ["HOME"])
record_path = home / ".pip-install-dry-runs.jsonl"
with record_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:]}, sort_keys=True) + "\\n")
if "install" in sys.argv and "--dry-run" in sys.argv:
    print("recorded pip install dry-run")
    raise SystemExit(0)
print("pip shim only supports install --dry-run", file=sys.stderr)
raise SystemExit(2)
""",
    )


def _write_executable(path: pathlib.Path, text: str) -> None:
    """Write an executable script."""
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
