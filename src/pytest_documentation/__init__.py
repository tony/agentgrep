"""Typed pytest collection for documentation examples."""

from __future__ import annotations

from .collectors import MarkdownFenceCollector, PythonDocstringCollector
from .core import (
    CollectionContext,
    DocumentationCollector,
    DocumentationExample,
    DocumentationExampleFailure,
    DocumentationSuite,
    EvaluationResult,
    ExampleDocument,
    ExampleEvaluator,
    ExampleLocation,
    collect_examples,
    redact_path,
)
from .evaluators import ConsoleCommandEvaluator, PythonCodeEvaluator
from .sandbox import SandboxBackend, SandboxSeed, TempHomeSandbox

__all__ = [
    "CollectionContext",
    "ConsoleCommandEvaluator",
    "DocumentationCollector",
    "DocumentationExample",
    "DocumentationExampleFailure",
    "DocumentationSuite",
    "EvaluationResult",
    "ExampleDocument",
    "ExampleEvaluator",
    "ExampleLocation",
    "MarkdownFenceCollector",
    "PythonCodeEvaluator",
    "PythonDocstringCollector",
    "SandboxBackend",
    "SandboxSeed",
    "TempHomeSandbox",
    "collect_examples",
    "redact_path",
]
