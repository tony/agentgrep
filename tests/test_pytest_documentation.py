"""Tests for the ``pytest_documentation`` package."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import typing as t
import uuid

import pytest

from pytest_documentation import (
    ConsoleCommandEvaluator,
    DocumentationExample,
    DocumentationSuite,
    EvaluationFailureKind,
    EvaluationStatus,
    FastMCPConfigCollector,
    FastMCPConfigEvaluator,
    JustfileRecipeCollector,
    MarkdownFenceCollector,
    MarkdownPythonPageCollector,
    PythonDocstringCollector,
    PythonPageEvaluator,
    SandboxExecution,
    SphinxDoctestEvaluator,
    TempHomeSandbox,
    collect_examples,
    redact_path,
)
from pytest_documentation.evaluators import _parse_console_source

_REPO_ROOT = pathlib.Path(__file__).parents[1]


def _run_git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in a test repository."""
    completed = subprocess.run(
        ("git", *args),
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed


class MarkdownFenceCase(t.NamedTuple):
    """Expected metadata for one Markdown fence extraction case."""

    test_id: str
    text: str
    expected_source: str
    expected_language: str
    expected_start_line: int
    expected_end_line: int
    expected_prefix: str
    expected_indent: str
    expected_group: str


MARKDOWN_FENCE_CASES: tuple[MarkdownFenceCase, ...] = (
    MarkdownFenceCase(
        test_id="basic-python-fence",
        text="# Title\n\n```python group=setup\nprint('ok')\n```\n",
        expected_source="print('ok')\n",
        expected_language="python",
        expected_start_line=4,
        expected_end_line=4,
        expected_prefix="",
        expected_indent="",
        expected_group="setup",
    ),
    MarkdownFenceCase(
        test_id="quoted-console-fence",
        text="> ```console group=cli\n> $ agentgrep find -0\n> ```\n",
        expected_source="$ agentgrep find -0\n",
        expected_language="console",
        expected_start_line=2,
        expected_end_line=2,
        expected_prefix="> ",
        expected_indent="",
        expected_group="cli",
    ),
    MarkdownFenceCase(
        test_id="indented-list-fence",
        text="1. Step\n\n   ```py\n   value = 3\n   ```\n",
        expected_source="value = 3\n",
        expected_language="py",
        expected_start_line=4,
        expected_end_line=4,
        expected_prefix="",
        expected_indent="   ",
        expected_group="",
    ),
    MarkdownFenceCase(
        test_id="myst-code-block-console-options",
        text=(
            "```{code-block} console group=cli\n:caption: CLI smoke\n\n$ agentgrep --help\n```\n"
        ),
        expected_source="$ agentgrep --help\n",
        expected_language="console",
        expected_start_line=4,
        expected_end_line=4,
        expected_prefix="",
        expected_indent="",
        expected_group="cli",
    ),
    MarkdownFenceCase(
        test_id="myst-sourcecode-python",
        text="```{sourcecode} python\nvalue = 3\n```\n",
        expected_source="value = 3\n",
        expected_language="python",
        expected_start_line=2,
        expected_end_line=2,
        expected_prefix="",
        expected_indent="",
        expected_group="",
    ),
)


@pytest.mark.parametrize(
    "case",
    MARKDOWN_FENCE_CASES,
    ids=[case.test_id for case in MARKDOWN_FENCE_CASES],
)
def test_markdown_fence_collector_preserves_source_locations(
    tmp_path: pathlib.Path,
    case: MarkdownFenceCase,
) -> None:
    """Markdown fences keep exact source, line, index, prefix, indent, and group metadata."""
    path = tmp_path / "docs" / "example.md"
    path.parent.mkdir()
    path.write_text(case.text, encoding="utf-8")

    examples = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector()],
        project_root=tmp_path,
    )

    assert len(examples) == 1
    example = examples[0]
    assert example.source == case.expected_source
    assert example.raw_source == case.expected_source
    assert example.language == case.expected_language
    assert example.location.start_line == case.expected_start_line
    assert example.location.end_line == case.expected_end_line
    assert example.location.prefix == case.expected_prefix
    assert example.location.indent == case.expected_indent
    assert example.location.group == case.expected_group
    assert (
        case.text[example.location.start_index : example.location.end_index] == case.expected_source
    )
    assert example.location.display_path == "docs/example.md"


def test_markdown_fence_collector_reports_unclosed_fence(tmp_path: pathlib.Path) -> None:
    """Unclosed fences fail during collection with a privacy-preserving location."""
    path = tmp_path / "docs" / "broken.md"
    path.parent.mkdir()
    path.write_text("```python\nprint('unterminated')\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"docs/broken\.md:1"):
        collect_examples([path], collectors=[MarkdownFenceCollector()], project_root=tmp_path)


def test_markdown_fence_collector_does_not_collect_eval_rst_as_console(
    tmp_path: pathlib.Path,
) -> None:
    """Only MyST code directives map to executable languages."""
    path = tmp_path / "docs" / "example.md"
    path.parent.mkdir()
    path.write_text(
        "```{eval-rst}\n.. code-block:: console\n\n   $ agentgrep --help\n```\n",
        encoding="utf-8",
    )

    examples = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )

    assert examples == []


class NestedFenceCase(t.NamedTuple):
    """Console sources expected from a document with an excluded outer fence."""

    test_id: str
    text: str
    expected_sources: tuple[str, ...]


NESTED_FENCE_CASES: tuple[NestedFenceCase, ...] = (
    NestedFenceCase(
        test_id="literal-console-in-backtick-demo",
        text="````markdown\n```console\n$ should-not-run\n```\n````\n",
        expected_sources=(),
    ),
    NestedFenceCase(
        test_id="literal-console-in-tilde-demo",
        text="~~~markdown\n```console\n$ should-not-run\n```\n~~~\n",
        expected_sources=(),
    ),
    NestedFenceCase(
        test_id="real-top-level-console-still-collected",
        text="```console\n$ agentgrep find foo\n```\n",
        expected_sources=("$ agentgrep find foo\n",),
    ),
)


@pytest.mark.parametrize(
    "case",
    NESTED_FENCE_CASES,
    ids=[case.test_id for case in NESTED_FENCE_CASES],
)
def test_markdown_fence_collector_skips_console_nested_in_excluded_fence(
    tmp_path: pathlib.Path,
    case: NestedFenceCase,
) -> None:
    """A literal console fence inside an excluded outer fence is not collected."""
    path = tmp_path / "docs" / "example.md"
    path.parent.mkdir()
    path.write_text(case.text, encoding="utf-8")

    examples = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )

    assert tuple(example.source for example in examples) == case.expected_sources


def test_python_page_collector_combines_library_examples_with_line_padding(
    tmp_path: pathlib.Path,
) -> None:
    """Python page examples share one narrative namespace while preserving line numbers."""
    path = tmp_path / "docs" / "library" / "event-stream.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Event stream\n"
        "\n"
        "```python\n"
        "value = 3\n"
        "```\n"
        "\n"
        "Narrative text.\n"
        "\n"
        "```py\n"
        "assert value == 3\n"
        "```\n",
        encoding="utf-8",
    )

    examples = collect_examples(
        [path],
        collectors=[MarkdownPythonPageCollector()],
        project_root=tmp_path,
    )

    assert len(examples) == 1
    example = examples[0]
    assert example.language == "python-page"
    assert example.kind == "code"
    assert example.location.start_line == 4
    assert example.location.end_line == 10
    assert example.test_id == "docs/library/event-stream.md:python-page"
    assert example.source.splitlines()[3] == "value = 3"
    assert example.source.splitlines()[9] == "assert value == 3"


def test_python_page_collector_keeps_non_library_docs_out_of_scope(
    tmp_path: pathlib.Path,
) -> None:
    """Python execution is limited to README and public library docs."""
    path = tmp_path / "docs" / "dev" / "adr" / "0001.md"
    path.parent.mkdir(parents=True)
    path.write_text("```python\nassert False\n```\n", encoding="utf-8")

    examples = collect_examples(
        [path],
        collectors=[MarkdownPythonPageCollector()],
        project_root=tmp_path,
    )

    assert examples == []


def test_python_docstring_collector_tracks_docstring_code_blocks(tmp_path: pathlib.Path) -> None:
    """Python docstrings are collected through AST locations instead of regex-only scanning."""
    path = tmp_path / "src" / "pkg" / "module.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '"""Module docs.\n\n'
        "```python group=module\n"
        "answer = 42\n"
        "```\n"
        '"""\n\n'
        "def demo() -> None:\n"
        '    """Function docs.\n\n'
        "    ```python group=function\n"
        "    print(answer)\n"
        "    ```\n"
        '    """\n',
        encoding="utf-8",
    )

    examples = collect_examples(
        [path],
        collectors=[PythonDocstringCollector()],
        project_root=tmp_path,
    )

    assert [example.location.group for example in examples] == ["module", "function"]
    assert [example.source for example in examples] == ["answer = 42\n", "print(answer)\n"]
    assert all(example.location.display_path == "src/pkg/module.py" for example in examples)


def test_redact_path_prefers_project_relative_then_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path redaction uses project-relative paths first and falls back to home-safe rendering."""
    project_root = pathlib.Path("/home/d/work/python/agentgrep")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert redact_path(project_root / "docs" / "cli.md", project_root=project_root) == "docs/cli.md"
    assert (
        redact_path(home / ".codex" / "history.jsonl", project_root=project_root)
        == "~/.codex/history.jsonl"
    )
    assert (
        redact_path(pathlib.Path("/home/alice/secrets/token.txt"), project_root=project_root)
        == "/home/<user>/secrets/token.txt"
    )


def test_literal_shell_evaluator_uses_temp_home_and_preserves_real_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console examples run as literal shell in a temp home, not the user's real home."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    path = tmp_path / "README.md"
    path.write_text("```console\n$ printf '%s' \"$HOME\" > touched-home\n```\n", encoding="utf-8")
    example = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    sandbox = TempHomeSandbox(project_root=tmp_path)
    evaluator = ConsoleCommandEvaluator(sandbox=sandbox)

    result = evaluator.evaluate(example)

    assert result.passed is True
    assert not (real_home / "touched-home").exists()
    assert result.stdout == ""
    assert result.stderr == ""


def test_sandbox_isolates_uv_build_env_by_default(tmp_path: pathlib.Path) -> None:
    """By default the sandbox builds an isolated uv cache and project venv."""
    sandbox = TempHomeSandbox(project_root=tmp_path)
    sandbox_root, home, project, shim_bin = sandbox._ensure_world()

    env = sandbox._build_env(home, sandbox_root=sandbox_root, project=project, shim_bin=shim_bin)

    assert env["UV_CACHE_DIR"] == str(sandbox_root / "uv-cache")
    assert env["UV_PROJECT_ENVIRONMENT"] == str(project / ".venv-docs-sandbox")
    assert "UV_NO_SYNC" not in env


def test_sandbox_reuses_shared_uv_build_env_when_configured(tmp_path: pathlib.Path) -> None:
    """A configured shared uv cache and project env are reused read-only.

    HOME stays isolated regardless; only the build artifacts (cache, venv)
    are shared, and UV_NO_SYNC marks the shared env read-only so parallel
    workers cannot race a sync.
    """
    shared_cache = tmp_path / "shared-uv-cache"
    shared_env = tmp_path / "shared-venv"
    sandbox = TempHomeSandbox(
        project_root=tmp_path,
        uv_cache_dir=shared_cache,
        uv_project_environment=shared_env,
    )
    sandbox_root, home, project, shim_bin = sandbox._ensure_world()

    env = sandbox._build_env(home, sandbox_root=sandbox_root, project=project, shim_bin=shim_bin)

    assert env["UV_CACHE_DIR"] == str(shared_cache)
    assert env["UV_PROJECT_ENVIRONMENT"] == str(shared_env)
    assert env["UV_NO_SYNC"] == "1"
    # HOME isolation is preserved even when build artifacts are shared.
    assert env["HOME"] == str(home)


def test_literal_shell_evaluator_fails_unsupported_cli_option(tmp_path: pathlib.Path) -> None:
    """Command examples are executed literally, so real CLI failures are reported."""
    path = tmp_path / "README.md"
    path.write_text("```console\n$ python -c 'import sys; sys.exit(7)'\n```\n", encoding="utf-8")
    example = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    evaluator = ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=tmp_path))

    result = evaluator.evaluate(example)

    assert result.passed is False
    assert result.status is EvaluationStatus.FAILED
    assert result.failure_kind is EvaluationFailureKind.COMMAND_FAILED
    assert result.returncode == 7


def test_python_page_evaluator_shares_namespace_in_temp_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python page examples run in a temp home with one namespace per page."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    path = tmp_path / "README.md"
    path.write_text(
        "```python\n"
        "import pathlib\n"
        "home = pathlib.Path.home()\n"
        "value = 3\n"
        "```\n"
        "\n"
        "```python\n"
        "assert value == 3\n"
        "assert home != pathlib.Path.home() or not (home / 'real-marker').exists()\n"
        "(pathlib.Path.home() / 'page-marker').write_text('ok', encoding='utf-8')\n"
        "```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [path],
        collectors=[MarkdownPythonPageCollector()],
        project_root=tmp_path,
    )[0]

    result = PythonPageEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert not (real_home / "page-marker").exists()


def test_python_page_evaluator_failure_uses_document_line_numbers(
    tmp_path: pathlib.Path,
) -> None:
    """Python page tracebacks point at the original documentation line."""
    path = tmp_path / "README.md"
    path.write_text(
        "# README\n\n```python\nvalue = 3\n```\n\n```python\nassert value == 4\n```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [path],
        collectors=[MarkdownPythonPageCollector()],
        project_root=tmp_path,
    )[0]

    result = PythonPageEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.FAILED
    assert result.failure_kind is EvaluationFailureKind.COMMAND_FAILED
    assert result.returncode == 1
    assert 'File "README.md", line 8' in result.stderr


def test_temp_home_sandbox_redirects_uvx_agentgrep_to_local_checkout(
    tmp_path: pathlib.Path,
) -> None:
    """README ``uvx agentgrep`` smoke examples use the local project."""
    doc = tmp_path / "README.md"
    doc.write_text("```console\n$ uvx agentgrep --help\n```\n", encoding="utf-8")
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        # This case deliberately exercises the cold-build uvx->local redirect:
        # a real `uv run agentgrep` resolve+build runs here (no shared env).
        # Allow ample time so it does not flake under parallel CPU contention.
        sandbox=TempHomeSandbox(project_root=_REPO_ROOT, timeout=120.0),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert "uvx agentgrep redirected to local checkout" in result.message


class ConsoleSourceCase(t.NamedTuple):
    """Expected script / expected-output split for one console transcript."""

    test_id: str
    source: str
    expected_script: str
    expected_output: list[str]


CONSOLE_SOURCE_CASES: tuple[ConsoleSourceCase, ...] = (
    ConsoleSourceCase(
        test_id="indented-output-is-not-script",
        source="$ agentgrep find -agent:claude\n  -- positional separator\n  NOT agent:claude\n",
        expected_script="agentgrep find -agent:claude\n",
        expected_output=["  -- positional separator", "  NOT agent:claude"],
    ),
    ConsoleSourceCase(
        test_id="backslash-continuation-joins-script",
        source="$ cmd --a \\\n    --b \\\n    tail\n",
        expected_script="cmd --a \\\n    --b \\\n    tail\n",
        expected_output=[],
    ),
    ConsoleSourceCase(
        test_id="ps2-prompt-continuation-joins-script",
        source="$ for f in a b; do\n> echo $f\n> done\n",
        expected_script="for f in a b; do\necho $f\ndone\n",
        expected_output=[],
    ),
    ConsoleSourceCase(
        test_id="plain-output-is-captured",
        source="$ echo hi\nhi\n",
        expected_script="echo hi\n",
        expected_output=["hi"],
    ),
)


@pytest.mark.parametrize(
    "case",
    CONSOLE_SOURCE_CASES,
    ids=[case.test_id for case in CONSOLE_SOURCE_CASES],
)
def test_parse_console_source_keeps_indented_output_out_of_script(
    case: ConsoleSourceCase,
) -> None:
    """Indented transcript lines are expected output, not executed script."""
    script, expected_output = _parse_console_source(case.source)

    assert script == case.expected_script
    assert expected_output == case.expected_output


class _RaisingSandbox:
    """SandboxBackend stub that raises a fixed error from ``run_script``."""

    def __init__(self, error: BaseException) -> None:
        """Store the error to raise."""
        self._error = error

    def run_script(self, script: str, *, example: DocumentationExample) -> SandboxExecution:
        """Raise the configured error instead of running a script."""
        raise self._error


class SandboxErrorCase(t.NamedTuple):
    """Expected failure kind for one exception raised by the sandbox."""

    test_id: str
    error: BaseException
    expected_kind: EvaluationFailureKind


SANDBOX_ERROR_CASES: tuple[SandboxErrorCase, ...] = (
    SandboxErrorCase(
        test_id="subprocess-timeout-is-long-running",
        error=subprocess.TimeoutExpired(cmd="sleep 99", timeout=0.01),
        expected_kind=EvaluationFailureKind.LONG_RUNNING_COMMAND,
    ),
    SandboxErrorCase(
        test_id="os-error-is-harness-error",
        error=PermissionError("denied"),
        expected_kind=EvaluationFailureKind.HARNESS_ERROR,
    ),
    SandboxErrorCase(
        test_id="other-error-is-blocked-by-policy",
        error=RuntimeError("boom"),
        expected_kind=EvaluationFailureKind.BLOCKED_BY_POLICY,
    ),
)


@pytest.mark.parametrize(
    "case",
    SANDBOX_ERROR_CASES,
    ids=[case.test_id for case in SANDBOX_ERROR_CASES],
)
def test_console_evaluator_maps_sandbox_exceptions_to_failure_kinds(
    tmp_path: pathlib.Path,
    case: SandboxErrorCase,
) -> None:
    """A timing-out console command is long_running_command, not blocked_by_policy.

    A real timeout would need a slow subprocess, so a lightweight
    ``SandboxBackend`` stub raises the exception types ``run_script`` surfaces.
    """
    path = tmp_path / "README.md"
    path.write_text("```console\n$ sleep 99\n```\n", encoding="utf-8")
    example = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    evaluator = ConsoleCommandEvaluator(sandbox=_RaisingSandbox(case.error))

    result = evaluator.evaluate(example)

    assert result.passed is False
    assert result.failure_kind is case.expected_kind


class ExceptionRedactionCase(t.NamedTuple):
    """One sandbox exception whose surfaced message must be redacted."""

    test_id: str
    error: BaseException


EXCEPTION_REDACTION_CASES: tuple[ExceptionRedactionCase, ...] = (
    ExceptionRedactionCase(
        test_id="os-error-home-path",
        error=OSError(f"cannot open {pathlib.Path.home()}/secret"),
    ),
    ExceptionRedactionCase(
        test_id="generic-error-home-path",
        error=RuntimeError(f"boom near {pathlib.Path.home()}/secret"),
    ),
)


@pytest.mark.parametrize(
    "case",
    EXCEPTION_REDACTION_CASES,
    ids=[case.test_id for case in EXCEPTION_REDACTION_CASES],
)
def test_console_evaluator_redacts_home_in_exception_messages(
    tmp_path: pathlib.Path,
    case: ExceptionRedactionCase,
) -> None:
    """Sandbox exception messages must not leak the real home directory."""
    path = tmp_path / "README.md"
    path.write_text("```console\n$ true\n```\n", encoding="utf-8")
    example = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    evaluator = ConsoleCommandEvaluator(sandbox=_RaisingSandbox(case.error))

    result = evaluator.evaluate(example)

    assert result.passed is False
    assert str(pathlib.Path.home()) not in result.message


def test_temp_home_sandbox_copies_dirty_git_project(tmp_path: pathlib.Path) -> None:
    """Dirty worktree content is what documentation examples execute against."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text(
        "```console\n"
        "$ python -c 'import pathlib, sys; "
        'sys.exit(0 if pathlib.Path("tracked.txt").read_text(encoding="utf-8") '
        '== "new" else 3)\' && git rev-parse --verify HEAD >/dev/null\n'
        "```\n",
        encoding="utf-8",
    )
    (project / "tracked.txt").write_text("old", encoding="utf-8")
    _run_git(project, "init", "-b", "main")
    _run_git(project, "add", ".")
    _run_git(
        project,
        "-c",
        "user.name=agentgrep tests",
        "-c",
        "user.email=agentgrep-tests@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    (project / "tracked.txt").write_text("new", encoding="utf-8")
    example = collect_examples(
        [project / "README.md"],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=project,
    )[0]

    result = ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=project)).evaluate(
        example,
    )

    assert result.passed is True
    assert result.returncode == 0


class CloneFallbackCase(t.NamedTuple):
    """One failed-clone fallback scenario for ``_prepare_project``."""

    test_id: str
    leave_stale_destination: bool


CLONE_FALLBACK_CASES: tuple[CloneFallbackCase, ...] = (
    CloneFallbackCase(test_id="stale-partial-clone", leave_stale_destination=True),
    CloneFallbackCase(test_id="clean-destination", leave_stale_destination=False),
)


@pytest.mark.parametrize(
    "case",
    CLONE_FALLBACK_CASES,
    ids=[case.test_id for case in CLONE_FALLBACK_CASES],
)
def test_prepare_project_copy_fallback_survives_failed_clone(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: CloneFallbackCase,
) -> None:
    """A failed clone's leftover destination does not break the copy fallback.

    ``monkeypatch`` intercepts only the ``git clone`` subprocess because a
    clone that dies after creating its destination directory (the bug
    trigger) cannot be provoked reliably with a real git invocation.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "tracked.txt").write_text("content", encoding="utf-8")
    _run_git(source, "init", "-b", "main")
    _run_git(source, "add", ".")
    _run_git(
        source,
        "-c",
        "user.name=agentgrep tests",
        "-c",
        "user.email=agentgrep-tests@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    sandbox = TempHomeSandbox(project_root=source)
    destination = tmp_path / "world" / "project" / source.name
    if case.leave_stale_destination:
        destination.mkdir(parents=True)
        (destination / "partial.txt").write_text("partial", encoding="utf-8")
    real_run = subprocess.run

    def fake_run(
        command: tuple[str, ...],
        **kwargs: t.Any,
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] == ("git", "clone"):
            return subprocess.CompletedProcess(
                args=command,
                returncode=128,
                stdout="",
                stderr="fatal: simulated clone interruption",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr("pytest_documentation.sandbox.subprocess.run", fake_run)

    sandbox._prepare_project(destination)

    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "content"
    assert not (destination / "partial.txt").exists()


def test_literal_shell_evaluator_accepts_expected_error_output(tmp_path: pathlib.Path) -> None:
    """Console transcripts can document expected non-zero command output."""
    path = tmp_path / "README.md"
    path.write_text(
        "```console\n"
        "$ python -c 'import sys; print(\"known error\", file=sys.stderr); sys.exit(4)'\n"
        "known error\n"
        "```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    evaluator = ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=tmp_path))

    result = evaluator.evaluate(example)

    assert result.passed is True
    assert result.status is EvaluationStatus.PASSED
    assert result.returncode == 4


def test_documentation_suite_registers_class_and_function_collectors(
    tmp_path: pathlib.Path,
) -> None:
    """Suites accept class-based collectors and functional collectors together."""
    path = tmp_path / "docs.md"
    path.write_text("```python\nprint('from class collector')\n```\n", encoding="utf-8")
    suite = DocumentationSuite(project_root=tmp_path)
    suite.register_collector(MarkdownFenceCollector(languages={"python"}))
    suite.register_function_collector(
        name="synthetic",
        suffixes={".md"},
        collect=lambda document: (),
    )

    examples = suite.collect([path])

    assert [example.source for example in examples] == ["print('from class collector')\n"]


def test_console_evaluator_redacts_paths_in_failures(tmp_path: pathlib.Path) -> None:
    """Failure messages redact absolute paths before pytest renders them."""
    secret_path = pathlib.Path("/home/alice/private/token.txt")
    doc = tmp_path / "README.md"
    doc.write_text(f"```console\n$ printf '%s\\n' '{secret_path}'; exit 2\n```\n", encoding="utf-8")
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.passed is False
    assert "/home/alice" not in result.failure_message()
    assert "/home/<user>/private/token.txt" in result.failure_message()


def test_sandbox_reports_blocked_commands_as_classified_failures(
    tmp_path: pathlib.Path,
) -> None:
    """Stateful install commands are rewritten into safe dry runs."""
    doc = tmp_path / "README.md"
    doc.write_text("```console\n$ uv sync --all-groups\n```\n", encoding="utf-8")
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=_REPO_ROOT)).evaluate(
        example,
    )

    assert result.status is EvaluationStatus.PASSED
    assert result.returncode == 0
    assert "uv sync dry-run" in result.message


class HomePolicyScriptCase(t.NamedTuple):
    """One script template for the real-home policy boundary check."""

    test_id: str
    script_template: str
    expected_blocked: bool


HOME_POLICY_SCRIPT_CASES: tuple[HomePolicyScriptCase, ...] = (
    HomePolicyScriptCase(
        test_id="real-home-subpath",
        script_template="cat {home}/notes.txt",
        expected_blocked=True,
    ),
    HomePolicyScriptCase(
        test_id="real-home-bare",
        script_template="echo {home}",
        expected_blocked=True,
    ),
    HomePolicyScriptCase(
        test_id="real-home-quoted",
        script_template='printf "%s" "{home}"',
        expected_blocked=True,
    ),
    HomePolicyScriptCase(
        test_id="sibling-prefix-path",
        script_template="cat {home}xtra/notes.txt",
        expected_blocked=False,
    ),
    HomePolicyScriptCase(
        test_id="no-home-mention",
        script_template="printf ok",
        expected_blocked=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    HOME_POLICY_SCRIPT_CASES,
    ids=[case.test_id for case in HOME_POLICY_SCRIPT_CASES],
)
def test_sandbox_blocks_real_home_only_at_path_boundaries(
    tmp_path: pathlib.Path,
    case: HomePolicyScriptCase,
) -> None:
    """The real-home policy ignores sibling paths that merely share a prefix."""
    sandbox = TempHomeSandbox(project_root=tmp_path)
    script = case.script_template.format(home=pathlib.Path.home().as_posix())

    if case.expected_blocked:
        with pytest.raises(subprocess.SubprocessError, match="real home path"):
            sandbox._check_policy(script)
    else:
        sandbox._check_policy(script)


def test_console_evaluator_classifies_data_dependent_empty_results(
    tmp_path: pathlib.Path,
) -> None:
    """Configured no-match docs examples pass as data-dependent empty results."""
    doc = tmp_path / "README.md"
    doc.write_text(
        "```console\n$ agentgrep search --threshold 70 migration\n```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert result.returncode == 1
    assert "accepted data-dependent empty result" in result.message


def test_console_evaluator_keeps_path_tilde_empty_result_as_failure(
    tmp_path: pathlib.Path,
) -> None:
    """The real ``path:~`` failure is not hidden by empty-result policy."""
    doc = tmp_path / "README.md"
    doc.write_text(
        "```console\n$ agentgrep find 'path:~/.codex agent:codex'\n```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.FAILED
    assert result.failure_kind is EvaluationFailureKind.COMMAND_FAILED
    assert result.returncode == 1


def test_temp_home_sandbox_keeps_profile_artifacts_inside_temp_project(
    tmp_path: pathlib.Path,
) -> None:
    """Relative redirections from docs examples do not write into the real project."""
    output_name = f"docs-profile-{uuid.uuid4().hex}.json"
    real_output = tmp_path / ".tmp" / output_name
    doc = tmp_path / "README.md"
    doc.write_text(
        "```console\n"
        "$ python -c 'from pathlib import Path; Path(\".tmp/"
        f'{output_name}").write_text("ok", encoding="utf-8")\'\n'
        "```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert not real_output.exists()


def test_temp_home_sandbox_records_claude_mcp_add_in_temp_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client registration examples use a shim that writes only to temp HOME."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    doc = tmp_path / "README.md"
    doc.write_text(
        "```console\n$ claude mcp add agentgrep -- uv run agentgrep-mcp\n```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert not (real_home / ".claude" / "mcp-additions.jsonl").exists()
    assert "claude mcp add shim" in result.message


def test_temp_home_sandbox_treats_standalone_cd_agentgrep_as_sequence_step(
    tmp_path: pathlib.Path,
) -> None:
    """Install-sequence ``cd agentgrep`` examples no longer count as real bugs."""
    doc = tmp_path / "README.md"
    doc.write_text("```console\n$ cd agentgrep\n```\n", encoding="utf-8")
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=tmp_path),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert result.returncode == 0
    assert "standalone sequence step accepted" in result.message


def test_temp_home_sandbox_accepts_ref_dependent_benchmark_recipes(
    tmp_path: pathlib.Path,
) -> None:
    """Benchmark examples with trunk/master refs are recipes, not local-env failures."""
    doc = tmp_path / "README.md"
    doc.write_text(
        "```console\n$ uv run scripts/benchmark.py run --target trunk\n```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    result = ConsoleCommandEvaluator(
        sandbox=TempHomeSandbox(project_root=_REPO_ROOT),
    ).evaluate(example)

    assert result.status is EvaluationStatus.PASSED
    assert result.returncode == 0
    assert "benchmark ref-dependent recipe accepted" in result.message


def test_fastmcp_config_evaluator_reports_missing_source_path(tmp_path: pathlib.Path) -> None:
    """FastMCP config examples validate source loading without starting a server."""
    config = tmp_path / "fastmcp.json"
    config.write_text(
        """
        {
          "$schema": "https://gofastmcp.com/public/schemas/fastmcp.json/v1.json",
          "source": {
            "type": "filesystem",
            "path": "src/pkg/mcp.py",
            "entrypoint": "build_mcp_server"
          }
        }
        """,
        encoding="utf-8",
    )

    example = collect_examples(
        [config],
        collectors=[FastMCPConfigCollector()],
        project_root=tmp_path,
    )[0]
    result = FastMCPConfigEvaluator(project_root=tmp_path).evaluate(example)

    assert result.status is EvaluationStatus.FAILED
    assert result.failure_kind is EvaluationFailureKind.CONFIG_INVALID
    assert "source path does not exist" in result.message
    assert "src/pkg/mcp.py" in result.message


def test_fastmcp_config_evaluator_accepts_existing_entrypoint(tmp_path: pathlib.Path) -> None:
    """FastMCP config validation checks the filesystem source and named entrypoint."""
    source = tmp_path / "src" / "pkg" / "mcp.py"
    source.parent.mkdir(parents=True)
    source.write_text("def build_mcp_server():\n    return object()\n", encoding="utf-8")
    config = tmp_path / "fastmcp.json"
    config.write_text(
        """
        {
          "source": {
            "type": "filesystem",
            "path": "src/pkg/mcp.py",
            "entrypoint": "build_mcp_server"
          }
        }
        """,
        encoding="utf-8",
    )

    example = collect_examples(
        [config],
        collectors=[FastMCPConfigCollector()],
        project_root=tmp_path,
    )[0]
    result = FastMCPConfigEvaluator(project_root=tmp_path).evaluate(example)

    assert result.status is EvaluationStatus.PASSED


class SphinxDoctestRecipeCase(t.NamedTuple):
    """Expected result for one Sphinx doctest recipe evaluation."""

    test_id: str
    conf_text: str
    expected_status: EvaluationStatus
    expected_failure_kind: EvaluationFailureKind
    expected_message: str


SPHINX_DOCTEST_RECIPE_CASES: tuple[SphinxDoctestRecipeCase, ...] = (
    SphinxDoctestRecipeCase(
        test_id="missing-doctest-extension",
        conf_text="project = 'pytest-documentation-test'\n",
        expected_status=EvaluationStatus.FAILED,
        expected_failure_kind=EvaluationFailureKind.DOCTEST_FAILED,
        expected_message="Builder name doctest",
    ),
    SphinxDoctestRecipeCase(
        test_id="missing-global-setup",
        conf_text=("project = 'pytest-documentation-test'\nextensions = ['sphinx.ext.doctest']\n"),
        expected_status=EvaluationStatus.FAILED,
        expected_failure_kind=EvaluationFailureKind.DOCTEST_FAILED,
        expected_message="NameError",
    ),
    SphinxDoctestRecipeCase(
        test_id="configured-doctest",
        conf_text=(
            "project = 'pytest-documentation-test'\n"
            "extensions = ['sphinx.ext.doctest']\n"
            "doctest_global_setup = 'from agentgrep import format_timestamp_tig'\n"
        ),
        expected_status=EvaluationStatus.PASSED,
        expected_failure_kind=EvaluationFailureKind.NONE,
        expected_message="",
    ),
)


@pytest.mark.parametrize(
    "case",
    SPHINX_DOCTEST_RECIPE_CASES,
    ids=[case.test_id for case in SPHINX_DOCTEST_RECIPE_CASES],
)
def test_justfile_recipe_collector_and_doctest_evaluator(
    tmp_path: pathlib.Path,
    case: SphinxDoctestRecipeCase,
) -> None:
    """Justfile doctest recipes are collected and evaluated with Sphinx."""
    docs = tmp_path / "docs"
    docs.mkdir()
    justfile = docs / "justfile"
    justfile.write_text(
        f'sphinxbuild := "{sys.executable} -m sphinx"\n'
        'builddir := "_build"\n'
        'allsphinxopts := "-d " + builddir + "/doctrees ."\n\n'
        "doctest:\n"
        "    {{ sphinxbuild }} -b doctest {{ allsphinxopts }} {{ builddir }}/doctest\n"
        "\n"
        "[group: 'misc']\n"
        "clean:\n"
        "    rm -rf _build\n",
        encoding="utf-8",
    )
    (docs / "conf.py").write_text(case.conf_text, encoding="utf-8")
    (docs / "index.rst").write_text(
        "Doctest fixture\n"
        "===============\n"
        "\n"
        ".. doctest::\n"
        "\n"
        "   >>> format_timestamp_tig(None)\n"
        "   ''\n",
        encoding="utf-8",
    )

    example = collect_examples(
        [justfile],
        collectors=[JustfileRecipeCollector(recipe_names={"doctest"})],
        project_root=tmp_path,
    )[0]
    result = SphinxDoctestEvaluator(project_root=tmp_path, timeout=30.0).evaluate(example)

    assert example.language == "just-recipe"
    assert example.location.group == "doctest"
    assert "[group:" not in example.source
    assert result.status is case.expected_status
    assert result.failure_kind is case.expected_failure_kind
    assert not (docs / "_build").exists()
    if case.expected_message:
        diagnostic = result.message + result.stdout + result.stderr
        assert case.expected_message in diagnostic
    if case.expected_status is EvaluationStatus.PASSED:
        assert result.returncode == 0


def test_fastmcp_config_collector_yields_malformed_json_for_evaluation(
    tmp_path: pathlib.Path,
) -> None:
    """Malformed fastmcp.json is collected so the evaluator can fail it (not skipped)."""
    config = tmp_path / "fastmcp.json"
    config.write_text("{ not valid json ", encoding="utf-8")

    examples = collect_examples(
        [config],
        collectors=[FastMCPConfigCollector()],
        project_root=tmp_path,
    )

    assert len(examples) == 1
    result = FastMCPConfigEvaluator(project_root=tmp_path).evaluate(examples[0])
    assert result.status is EvaluationStatus.FAILED
    assert result.failure_kind is EvaluationFailureKind.CONFIG_INVALID
    assert "invalid JSON" in result.message


class ExpectedOutputCase(t.NamedTuple):
    """Whether a successful console example passes given its documented output."""

    test_id: str
    documented_output: str
    expected_passed: bool


EXPECTED_OUTPUT_CASES: tuple[ExpectedOutputCase, ...] = (
    ExpectedOutputCase(
        test_id="matching-output-passes",
        documented_output="right",
        expected_passed=True,
    ),
    ExpectedOutputCase(
        test_id="wrong-output-fails-despite-exit-zero",
        documented_output="wrong",
        expected_passed=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    EXPECTED_OUTPUT_CASES,
    ids=[case.test_id for case in EXPECTED_OUTPUT_CASES],
)
def test_console_evaluator_checks_expected_output_on_success(
    tmp_path: pathlib.Path,
    case: ExpectedOutputCase,
) -> None:
    """Documented console output must match even when the command exits 0."""
    path = tmp_path / "docs" / "example.md"
    path.parent.mkdir()
    path.write_text(
        f"```console\n$ printf right\n{case.documented_output}\n```\n",
        encoding="utf-8",
    )
    example = collect_examples(
        [path],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]
    result = ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=tmp_path)).evaluate(
        example
    )

    assert result.passed is case.expected_passed
