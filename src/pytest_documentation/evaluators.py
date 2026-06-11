"""Example evaluators for ``pytest_documentation``."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import traceback
import typing as t

from .core import DocumentationExample, EvaluationFailureKind, EvaluationResult, redact_text
from .sandbox import SandboxBackend, TempHomeSandbox


class PythonCodeEvaluator:
    """Evaluate Python examples with ``exec`` in an isolated namespace."""

    def __init__(self, *, globals_: dict[str, object] | None = None) -> None:
        """Create a Python evaluator.

        Parameters
        ----------
        globals_ : dict[str, object] | None
            Base globals copied into each example's exec namespace.
        """
        self.globals = dict(globals_ or {})

    def evaluate(self, example: DocumentationExample) -> EvaluationResult:
        """Evaluate a Python example."""
        namespace = dict(self.globals)
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = compile(example.source, example.location.display_path, "exec")
                exec(code, namespace, namespace)
        except BaseException as exc:
            message = "".join(traceback.format_exception(exc))
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.HARNESS_ERROR,
                stdout=stdout.getvalue(),
                stderr=stderr.getvalue(),
                message=redact_text(message, project_root=example.location.path.parent),
            )
        return EvaluationResult.passed_result(
            example,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )


class ConsoleCommandEvaluator:
    """Evaluate console examples as literal shell scripts."""

    def __init__(self, *, sandbox: SandboxBackend | None = None) -> None:
        """Create a console evaluator.

        Parameters
        ----------
        sandbox : SandboxBackend | None
            Sandbox used to run scripts. ``None`` creates a
            :class:`~pytest_documentation.sandbox.TempHomeSandbox`.
        """
        self.sandbox = sandbox or TempHomeSandbox()

    def evaluate(self, example: DocumentationExample) -> EvaluationResult:
        """Evaluate a console example in the configured sandbox."""
        script, expected_output = _parse_console_source(example.source)
        try:
            execution = self.sandbox.run_script(script, example=example)
        except TimeoutError as exc:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.LONG_RUNNING_COMMAND,
                message=str(exc),
            )
        except OSError as exc:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.HARNESS_ERROR,
                message=str(exc),
            )
        except Exception as exc:
            message = str(exc)
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.BLOCKED_BY_POLICY,
                message=message,
            )
        completed = execution.completed
        if execution.failure_kind is not None:
            return EvaluationResult.failed_result(
                example,
                failure_kind=execution.failure_kind,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                message=execution.message,
            )
        passed = completed.returncode == 0 or _expected_output_matches(
            expected_output,
            completed.stdout + completed.stderr,
        )
        if not passed and completed.returncode == 1 and execution.accept_empty_result:
            return EvaluationResult.passed_result(
                example,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                message=_join_messages(execution.message, "accepted data-dependent empty result"),
            )
        if passed:
            return EvaluationResult.passed_result(
                example,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                message=_join_messages(
                    execution.message,
                    (
                        "expected output matched non-zero command output"
                        if completed.returncode
                        else ""
                    ),
                ),
            )
        return EvaluationResult.failed_result(
            example,
            failure_kind=_failure_kind_for_returncode(script, completed.returncode),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            message=execution.message,
        )


class FastMCPConfigEvaluator:
    """Validate FastMCP config files without starting an MCP server."""

    _DEFAULT_ENTRYPOINTS = frozenset({"mcp", "server", "app"})

    def __init__(self, *, project_root: pathlib.Path | None = None) -> None:
        """Create a FastMCP config evaluator.

        Parameters
        ----------
        project_root : pathlib.Path | None
            Root the evaluator treats as the project checkout. ``None``
            uses the current working directory.
        """
        self.project_root = (project_root or pathlib.Path.cwd()).expanduser().resolve()

    def evaluate(self, example: DocumentationExample) -> EvaluationResult:
        """Evaluate a collected FastMCP config example."""
        try:
            payload = json.loads(example.source)
        except json.JSONDecodeError as exc:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message=f"invalid JSON: {exc}",
            )
        if not isinstance(payload, dict):
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message="fastmcp.json must contain a JSON object",
            )
        source = payload.get("source")
        if not isinstance(source, dict):
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message="fastmcp.json must contain an object-valued source field",
            )
        source_type = source.get("type", "filesystem")
        if source_type != "filesystem":
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message=f"unsupported FastMCP source type for docs validation: {source_type!r}",
            )
        source_path = source.get("path")
        if not isinstance(source_path, str) or not source_path:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message="filesystem FastMCP source must include a non-empty path",
            )
        path_text, inline_entrypoint = _split_fastmcp_path(source_path)
        entrypoint = source.get("entrypoint") or inline_entrypoint
        candidate = pathlib.Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = example.location.path.parent / candidate
        source_file = candidate.resolve()
        if not source_file.exists():
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message=f"source path does not exist: {path_text}",
            )
        if not source_file.is_file():
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message=f"source path is not a file: {path_text}",
            )
        defined_names = _top_level_names(source_file)
        expected_names = {str(entrypoint)} if entrypoint else set(self._DEFAULT_ENTRYPOINTS)
        if defined_names.isdisjoint(expected_names):
            expected = ", ".join(sorted(expected_names))
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.CONFIG_INVALID,
                message=f"source file does not define FastMCP entrypoint: {expected}",
            )
        return EvaluationResult.passed_result(example)


class SphinxDoctestEvaluator:
    """Evaluate Sphinx doctest recipes through ``just``."""

    def __init__(self, *, project_root: pathlib.Path | None = None, timeout: float = 60.0) -> None:
        """Create a Sphinx doctest evaluator.

        Parameters
        ----------
        project_root : pathlib.Path | None
            Root stripped from paths in redacted recipe output. ``None``
            uses the current working directory.
        timeout : float
            Recipe subprocess timeout in seconds.
        """
        self.project_root = (project_root or pathlib.Path.cwd()).expanduser().resolve()
        self.timeout = timeout

    def evaluate(self, example: DocumentationExample) -> EvaluationResult:
        """Evaluate a collected justfile doctest recipe."""
        docs_root = example.location.path.parent
        recipe = example.location.group
        if not recipe:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.DOCTEST_FAILED,
                message="justfile recipe example does not name a recipe",
            )
        if not example.location.path.exists():
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.DOCTEST_FAILED,
                message=f"justfile does not exist: {example.location.display_path}",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="pytest-documentation-doctest-") as temp_dir:
                builddir = pathlib.Path(temp_dir) / "build"
                completed = subprocess.run(
                    (
                        "just",
                        "-f",
                        str(example.location.path),
                        "--set",
                        "builddir",
                        str(builddir),
                        recipe,
                    ),
                    cwd=docs_root,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.LONG_RUNNING_COMMAND,
                stdout=redact_text(_output_to_text(exc.stdout), project_root=self.project_root),
                stderr=redact_text(_output_to_text(exc.stderr), project_root=self.project_root),
                message=f"just doctest recipe exceeded {self.timeout:g}s timeout",
            )
        except OSError as exc:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.HARNESS_ERROR,
                message=redact_text(str(exc), project_root=self.project_root),
            )
        stdout = redact_text(completed.stdout, project_root=self.project_root)
        stderr = redact_text(completed.stderr, project_root=self.project_root)
        if completed.returncode != 0:
            return EvaluationResult.failed_result(
                example,
                failure_kind=EvaluationFailureKind.DOCTEST_FAILED,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                message="just doctest recipe failed",
            )
        return EvaluationResult.passed_result(
            example,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            message="just doctest recipe passed",
        )


def _parse_console_source(source: str) -> tuple[str, list[str]]:
    """Convert a console transcript into a shell script and expected output."""
    script_lines: list[str] = []
    expected_output: list[str] = []
    for line in source.splitlines():
        if line.startswith(("$ ", "> ")):
            script_lines.append(line[2:])
        elif script_lines and (
            script_lines[-1].rstrip().endswith("\\") or line.startswith((" ", "\t"))
        ):
            script_lines.append(line)
        elif line.strip():
            expected_output.append(line)
    return "\n".join(script_lines) + ("\n" if script_lines else ""), expected_output


def _expected_output_matches(expected_lines: list[str], actual: str) -> bool:
    """Return whether expected transcript output appears in actual output."""
    meaningful_expected = [line for line in expected_lines if "[...]" not in line]
    if not meaningful_expected:
        return False
    normalized_actual = _normalize_output(actual)
    return all(_normalize_output(line) in normalized_actual for line in meaningful_expected)


def _normalize_output(text: str) -> str:
    """Normalize output for compact transcript matching."""
    return " ".join(text.split())


def _output_to_text(output: str | bytes | None) -> str:
    """Return subprocess output as text."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def _failure_kind_for_returncode(script: str, returncode: int) -> EvaluationFailureKind:
    """Return the most specific failure kind for a completed command."""
    if returncode == 1 and _looks_like_unaccepted_agentgrep_empty_result(script):
        return EvaluationFailureKind.DATA_DEPENDENT_EMPTY_RESULT
    return EvaluationFailureKind.COMMAND_FAILED


def _looks_like_unaccepted_agentgrep_empty_result(script: str) -> bool:
    """Return whether ``script`` is an agentgrep search where rc=1 means no matches."""
    normalized = _normalize_script(script)
    if "path:~" in normalized or "xargs" in normalized:
        return False
    return any(
        command in normalized
        for command in (
            "agentgrep search",
            "agentgrep grep",
            "agentgrep find",
            "uv run agentgrep search",
            "uv run agentgrep grep",
            "uv run agentgrep find",
        )
    )


def _normalize_script(script: str) -> str:
    """Normalize a shell script for conservative substring classification."""
    return " ".join(script.lower().split())


def _join_messages(first: str, second: str) -> str:
    """Join non-empty evaluator messages with a semicolon."""
    messages = [message for message in (first, second) if message]
    return "; ".join(messages)


def _split_fastmcp_path(path_text: str) -> tuple[str, str | None]:
    """Split FastMCP ``path:entrypoint`` syntax."""
    if ":" not in path_text:
        return path_text, None
    has_windows_drive = len(path_text) > 1 and path_text[1] == ":"
    search_text = path_text[2:] if has_windows_drive else path_text
    if ":" not in search_text:
        return path_text, None
    file_text, entrypoint = path_text.rsplit(":", 1)
    return file_text, entrypoint or None


def _top_level_names(path: pathlib.Path) -> set[str]:
    """Return top-level names defined in a Python module without executing it."""
    module = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_target_names(node.target))
    return names


def _target_names(target: ast.expr) -> set[str]:
    """Return names assigned by a top-level assignment target."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple | ast.List):
        names: set[str] = set()
        for element in target.elts:
            names.update(_target_names(element))
        return names
    return set()


def evaluate_many(
    examples: t.Iterable[DocumentationExample],
    evaluator: ConsoleCommandEvaluator
    | FastMCPConfigEvaluator
    | PythonCodeEvaluator
    | SphinxDoctestEvaluator,
) -> list[EvaluationResult]:
    """Evaluate many examples with one evaluator.

    Parameters
    ----------
    examples : t.Iterable[DocumentationExample]
        Examples to evaluate in order.
    evaluator
        Evaluator applied to every example; any evaluator class from
        this module.

    Returns
    -------
    list[EvaluationResult]
        One result per example, in input order.
    """
    return [evaluator.evaluate(example) for example in examples]
