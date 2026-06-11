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
    DocumentationSuite,
    EvaluationFailureKind,
    EvaluationStatus,
    FastMCPConfigCollector,
    FastMCPConfigEvaluator,
    JustfileRecipeCollector,
    MarkdownFenceCollector,
    PythonDocstringCollector,
    SphinxDoctestEvaluator,
    TempHomeSandbox,
    collect_examples,
    redact_path,
)

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


def test_redact_path_prefers_project_relative_then_home() -> None:
    """Path redaction uses project-relative paths first and falls back to home-safe rendering."""
    project_root = pathlib.Path("/home/d/work/python/agentgrep")

    assert redact_path(project_root / "docs" / "cli.md", project_root=project_root) == "docs/cli.md"
    assert (
        redact_path(pathlib.Path("/home/d/.codex/history.jsonl"), project_root=project_root)
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
