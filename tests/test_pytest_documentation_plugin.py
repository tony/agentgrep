"""Pytest integration tests for ``pytest_documentation``."""

from __future__ import annotations

import pytest

pytest_plugins = ("pytester",)


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

    result = pytester.runpytest("-q")

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

    result = pytester.runpytest("-q")

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

    result = pytester.runpytest("-q")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*classified documentation example failure*",
            "*failure_kind: command_failed*",
            "*returncode: 9*",
        ],
    )


def test_plugin_entrypoint_is_dormant_without_configured_suite(pytester: pytest.Pytester) -> None:
    """Loading the plugin alone does not start collecting every documentation file."""
    pytester.makeini(
        """
        [pytest]
        """,
    )
    pytester.makefile(".md", docs="```python\nassert False\n```\n")

    result = pytester.runpytest("-q")

    result.assert_outcomes()
