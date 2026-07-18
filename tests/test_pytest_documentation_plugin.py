"""Pytest integration tests for ``pytest_documentation``."""

from __future__ import annotations

import pytest

pytest_plugins = ("pytester",)

# Nested pytester runs configure pytest-asyncio explicitly; their isolated
# rootdir never reads this repository's pyproject.toml, so an unset
# asyncio_default_fixture_loop_scope would warn on every inner run. The
# documentation plugin ships no pytest11 entry point, so nested runs load it
# explicitly the same way the repository's addopts do.
_NESTED_RUN_ARGS = (
    "-q",
    "-o",
    "asyncio_default_fixture_loop_scope=function",
    "-p",
    "pytest_documentation.plugin",
)


def test_suite_collects_markdown_examples_as_pytest_items(pytester: pytest.Pytester) -> None:
    """A configured suite turns documentation examples into normal pytest items."""
    pytester.makeconftest(
        """
        from __future__ import annotations

        import pathlib

        from pytest_documentation import (
            DocumentationSuite,
            MarkdownFenceCollector,
            PythonCodeEvaluator,
        )

        suite = DocumentationSuite(project_root=pathlib.Path(__file__).parent)
        suite.register_collector(MarkdownFenceCollector(languages={"python"}))
        suite.register_evaluator("python", PythonCodeEvaluator())
        pytest_collect_file = suite.pytest_collect_file
        """,
    )
    pytester.makefile(".md", docs="```python\nassert 1 == 1\n```\n")

    result = pytester.runpytest(*_NESTED_RUN_ARGS)

    result.assert_outcomes(passed=1)


def test_pytest_failure_uses_document_location(pytester: pytest.Pytester) -> None:
    """Failure output points at the documentation file and exact example line."""
    pytester.makeconftest(
        """
        from __future__ import annotations

        import pathlib

        from pytest_documentation import (
            DocumentationSuite,
            MarkdownFenceCollector,
            PythonCodeEvaluator,
        )

        suite = DocumentationSuite(project_root=pathlib.Path(__file__).parent)
        suite.register_collector(MarkdownFenceCollector(languages={"python"}))
        suite.register_evaluator("python", PythonCodeEvaluator())
        pytest_collect_file = suite.pytest_collect_file
        """,
    )
    pytester.makefile(".md", docs="# Docs\n\n```python group=broken\nassert 1 == 2\n```\n")

    result = pytester.runpytest(*_NESTED_RUN_ARGS)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*docs.md:4*broken*", "*assert 1 == 2*"])


def test_pytest_failure_renders_classified_documentation_reason(
    pytester: pytest.Pytester,
) -> None:
    """Classified evaluator failures render their failure kind in pytest output."""
    pytester.makeconftest(
        """
        from __future__ import annotations

        import pathlib

        from pytest_documentation import (
            ConsoleCommandEvaluator,
            DocumentationSuite,
            MarkdownFenceCollector,
            TempHomeSandbox,
        )

        root = pathlib.Path(__file__).parent
        suite = DocumentationSuite(project_root=root)
        suite.register_collector(MarkdownFenceCollector(languages={"console"}))
        suite.register_evaluator(
            "console",
            ConsoleCommandEvaluator(sandbox=TempHomeSandbox(project_root=root)),
        )
        pytest_collect_file = suite.pytest_collect_file
        """,
    )
    pytester.makefile(".md", docs="```console\n$ python -c 'import sys; sys.exit(9)'\n```\n")

    result = pytester.runpytest(*_NESTED_RUN_ARGS)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*classified documentation example failure*",
            "*failure_kind: command_failed*",
            "*returncode: 9*",
        ],
    )


def test_suite_collects_python_page_examples_as_single_pytest_item(
    pytester: pytest.Pytester,
) -> None:
    """A Python page narrative runs as one pytest item with shared state."""
    pytester.makeconftest(
        """
        from __future__ import annotations

        import pathlib

        from pytest_documentation import (
            DocumentationSuite,
            MarkdownPythonPageCollector,
            PythonPageEvaluator,
            TempHomeSandbox,
        )

        root = pathlib.Path(__file__).parent
        suite = DocumentationSuite(project_root=root, include_paths=("README.md",))
        suite.register_collector(MarkdownPythonPageCollector())
        suite.register_evaluator(
            "python-page",
            PythonPageEvaluator(sandbox=TempHomeSandbox(project_root=root)),
        )
        pytest_collect_file = suite.pytest_collect_file
        """,
    )
    pytester.makefile(
        ".md",
        README=("```python\nvalue = 3\n```\n\n```python\nassert value == 3\n```\n"),
    )

    result = pytester.runpytest(*_NESTED_RUN_ARGS)

    result.assert_outcomes(passed=1)


def test_plugin_is_dormant_without_configured_suite(pytester: pytest.Pytester) -> None:
    """Loading the plugin alone does not start collecting every documentation file."""
    pytester.makeini(
        """
        [pytest]
        """,
    )
    pytester.makefile(".md", docs="```python\nassert False\n```\n")

    result = pytester.runpytest(*_NESTED_RUN_ARGS)

    result.assert_outcomes()
