"""Pytest integration for ``pytest_documentation``."""

from __future__ import annotations

import os
import subprocess
import typing as t

import pytest
from _pytest._code.code import ExceptionInfo, TerminalRepr, TracebackStyle
from _pytest._io import TerminalWriter
from _pytest._io.saferepr import saferepr
from _pytest.reports import BaseReport
from _pytest.terminal import TerminalReporter

from .core import (
    DocumentationExample,
    DocumentationExampleFailure,
    DocumentationSuite,
    EvaluationFailureKind,
    EvaluationResult,
)

if t.TYPE_CHECKING:
    import collections.abc as cabc


class DocumentationFailureRepr(TerminalRepr):
    """Terminal representation for documentation example failures."""

    def __init__(self, failure: DocumentationExampleFailure) -> None:
        """Store the failure to render."""
        self.failure = failure

    def toterminal(self, tw: TerminalWriter) -> None:
        """Render the failure to the terminal."""
        tw.line(self.failure.result.failure_message(), red=True)


class DocumentationItem(pytest.Item):
    """Pytest item for one documentation example."""

    def __init__(
        self,
        *,
        parent: pytest.Collector,
        example: DocumentationExample,
        suite: DocumentationSuite,
    ) -> None:
        """Create a documentation item."""
        super().__init__(name=example.test_id, parent=parent)
        self.example = example
        self.suite = suite
        self.add_marker("documentation")
        if example.language:
            self.add_marker(f"documentation_{example.language.replace('-', '_')}")
        if example.language == "fastmcp-config":
            self.add_marker("mcp")
            self.add_marker("setup")
        else:
            self.add_marker("slow")

    def runtest(self) -> None:
        """Run the registered evaluator for this example."""
        evaluator = self.suite.evaluator_for(self.example)
        if evaluator is None:
            message = f"no evaluator registered for {self.example.language!r}"
            result = self._failure_result(message)
            raise DocumentationExampleFailure(result)
        try:
            result = evaluator.evaluate(self.example)
        except subprocess.SubprocessError as exc:
            result = self._failure_result(str(exc))
        if not result.passed:
            raise DocumentationExampleFailure(result)

    def reportinfo(self) -> tuple[os.PathLike[str] | str, int | None, str]:
        """Return pytest report location."""
        return (
            self.example.location.path,
            self.example.location.start_line - 1,
            self.example.location.label(),
        )

    def repr_failure(
        self,
        excinfo: ExceptionInfo[BaseException],
        style: TracebackStyle | None = None,
    ) -> str | TerminalRepr:
        """Render documentation failures with source locations."""
        if isinstance(excinfo.value, DocumentationExampleFailure):
            return DocumentationFailureRepr(excinfo.value)
        return super().repr_failure(excinfo, style=style)

    def _failure_result(self, message: str) -> EvaluationResult:
        """Create an evaluator-shaped failure for internal pytest errors."""
        return EvaluationResult.failed_result(
            self.example,
            failure_kind=EvaluationFailureKind.HARNESS_ERROR,
            message=message,
        )


class DocumentationFile(pytest.File):
    """Pytest file collector for documentation examples."""

    def __init__(self, *, suite: DocumentationSuite, **kwargs: t.Any) -> None:
        """Create a documentation file collector."""
        super().__init__(**kwargs)
        self.suite = suite

    def collect(self) -> cabc.Iterable[DocumentationItem]:
        """Collect examples from this file."""
        for example in self.suite.collect_file(self.path):
            yield DocumentationItem.from_parent(self, example=example, suite=self.suite)


def pytest_configure(config: pytest.Config) -> None:
    """Register documentation markers."""
    config.addinivalue_line("markers", "mcp: MCP protocol and schema coverage")
    config.addinivalue_line("markers", "setup: repository infrastructure coverage")
    config.addinivalue_line("markers", "slow: essential opt-in local coverage")
    config.addinivalue_line(
        "markers",
        "documentation: documentation example collected by pytest-documentation",
    )
    config.addinivalue_line("markers", "documentation_python: Python documentation example")
    config.addinivalue_line(
        "markers",
        "documentation_python_page: page-level Python documentation example",
    )
    config.addinivalue_line("markers", "documentation_console: console documentation example")
    config.addinivalue_line(
        "markers",
        "documentation_fastmcp_config: FastMCP config documentation example",
    )
    config.addinivalue_line(
        "markers",
        "documentation_just_recipe: justfile recipe documentation example",
    )


def pytest_terminal_summary(
    terminalreporter: TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Add a compact documentation example summary when examples fail."""
    failed_docs = [
        report
        for report in terminalreporter.stats.get("failed", [])
        if isinstance(report, BaseReport) and "documentation" in report.keywords
    ]
    if not failed_docs:
        return
    terminalreporter.write_sep("=", "documentation example failures")
    for report in failed_docs[:10]:
        terminalreporter.write_line(saferepr(report.nodeid))
