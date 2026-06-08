"""Tests for the ``pytest_documentation`` package."""

from __future__ import annotations

import pathlib
import subprocess
import typing as t

import pytest

from pytest_documentation import (
    ConsoleCommandEvaluator,
    DocumentationSuite,
    MarkdownFenceCollector,
    PythonDocstringCollector,
    TempHomeSandbox,
    collect_examples,
    redact_path,
)


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
    assert result.returncode == 7


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


def test_sandbox_rejects_blocked_commands(tmp_path: pathlib.Path) -> None:
    """The default command policy blocks install and network-shaped commands."""
    doc = tmp_path / "README.md"
    doc.write_text("```console\n$ pip install agentgrep\n```\n", encoding="utf-8")
    example = collect_examples(
        [doc],
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=tmp_path,
    )[0]

    with pytest.raises(subprocess.SubprocessError, match="blocked command"):
        ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=tmp_path)).evaluate(example)
