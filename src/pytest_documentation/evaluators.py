"""Example evaluators for ``pytest_documentation``."""

from __future__ import annotations

import contextlib
import io
import traceback
import typing as t

from .core import DocumentationExample, EvaluationResult, redact_text
from .sandbox import SandboxBackend, TempHomeSandbox


class PythonCodeEvaluator:
    """Evaluate Python examples with ``exec`` in an isolated namespace."""

    def __init__(self, *, globals_: dict[str, object] | None = None) -> None:
        """Create a Python evaluator."""
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
            return EvaluationResult(
                passed=False,
                example=example,
                stdout=stdout.getvalue(),
                stderr=stderr.getvalue(),
                message=redact_text(message, project_root=example.location.path.parent),
            )
        return EvaluationResult(
            passed=True,
            example=example,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )


class ConsoleCommandEvaluator:
    """Evaluate console examples as literal shell scripts."""

    def __init__(self, *, sandbox: SandboxBackend | None = None) -> None:
        """Create a console evaluator."""
        self.sandbox = sandbox or TempHomeSandbox()

    def evaluate(self, example: DocumentationExample) -> EvaluationResult:
        """Evaluate a console example in the configured sandbox."""
        script, expected_output = _parse_console_source(example.source)
        completed = self.sandbox.run_script(script, example=example)
        passed = completed.returncode == 0 or _expected_output_matches(
            expected_output,
            completed.stdout + completed.stderr,
        )
        return EvaluationResult(
            passed=passed,
            example=example,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            message=(
                "expected output matched non-zero command output"
                if passed and completed.returncode
                else ""
            ),
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


def evaluate_many(
    examples: t.Iterable[DocumentationExample],
    evaluator: ConsoleCommandEvaluator | PythonCodeEvaluator,
) -> list[EvaluationResult]:
    """Evaluate many examples with one evaluator."""
    return [evaluator.evaluate(example) for example in examples]
