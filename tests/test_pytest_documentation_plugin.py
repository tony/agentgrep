"""Pytest integration tests for ``pytest_documentation``."""

from __future__ import annotations

import pathlib

import pytest

from pytest_documentation import DocumentationExample, ExampleLocation, TempHomeSandbox

pytest_plugins = ("pytester",)

# Nested pytester runs configure pytest-asyncio explicitly; their isolated
# rootdir never reads this repository's pyproject.toml, so an unset
# asyncio_default_fixture_loop_scope would warn on every inner run.
_NESTED_RUN_ARGS = ("-q", "-o", "asyncio_default_fixture_loop_scope=function")


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


def test_plugin_entrypoint_is_dormant_without_configured_suite(pytester: pytest.Pytester) -> None:
    """Loading the plugin alone does not start collecting every documentation file."""
    pytester.makeini(
        """
        [pytest]
        """,
    )
    pytester.makefile(".md", docs="```python\nassert False\n```\n")

    result = pytester.runpytest(*_NESTED_RUN_ARGS)

    result.assert_outcomes()


def test_temp_home_sandbox_emits_subprocess_telemetry(tmp_path: pathlib.Path) -> None:
    """Documentation subprocesses should report cost without raw command text."""
    import agentgrep._telemetry as telemetry

    example_path = tmp_path / "docs.md"
    example_path.write_text("```console\n$ python -c 'print(123)'\n```\n")
    example = DocumentationExample(
        kind="fence",
        language="console",
        source="python -c 'print(123)'",
        raw_source="$ python -c 'print(123)'",
        location=ExampleLocation(
            path=example_path,
            display_path="docs.md",
            start_line=1,
            end_line=3,
            start_index=0,
            end_index=36,
        ),
    )
    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    try:
        with telemetry.span("agentgrep.pytest.test", agentgrep_surface="pytest"):
            execution = TempHomeSandbox(project_root=tmp_path).run_script(
                "python -c 'print(123)'",
                example=example,
            )
    finally:
        telemetry.configure_backend(None)

    assert execution.completed.returncode == 0
    subprocess_span = next(
        span
        for span in backend.finished_spans
        if span.name == "agentgrep.pytest.documentation.subprocess"
    )
    assert subprocess_span.parent_id is not None
    assert subprocess_span.attributes["agentgrep_subprocess_kind"] == "documentation_example"
    assert "print(123)" not in str(subprocess_span.attributes)
    metric_names = {
        metric.name
        for metric in backend.metric_records
        if metric.attributes.get("agentgrep_subprocess_kind") == "documentation_example"
    }
    assert metric_names >= {
        "agentgrep.pytest.documentation.subprocess.count",
        "agentgrep.pytest.documentation.subprocess.duration",
    }
