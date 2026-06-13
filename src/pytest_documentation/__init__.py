"""Typed pytest collection for documentation examples."""

from __future__ import annotations

from .collectors import (
    FastMCPConfigCollector,
    JustfileRecipeCollector,
    MarkdownFenceCollector,
    MarkdownPythonPageCollector,
    PythonDocstringCollector,
)
from .core import (
    CollectionContext,
    DocumentationCollector,
    DocumentationExample,
    DocumentationExampleFailure,
    DocumentationSuite,
    EvaluationFailureKind,
    EvaluationResult,
    EvaluationStatus,
    ExampleDocument,
    ExampleEvaluator,
    ExampleLocation,
    collect_examples,
    redact_path,
)
from .evaluators import (
    ConsoleCommandEvaluator,
    FastMCPConfigEvaluator,
    PythonCodeEvaluator,
    PythonPageEvaluator,
    SphinxDoctestEvaluator,
)
from .sandbox import (
    SandboxBackend,
    SandboxCommandPlan,
    SandboxExecution,
    SandboxSeed,
    TempHomeSandbox,
)

__all__ = [
    "CollectionContext",
    "ConsoleCommandEvaluator",
    "DocumentationCollector",
    "DocumentationExample",
    "DocumentationExampleFailure",
    "DocumentationSuite",
    "EvaluationFailureKind",
    "EvaluationResult",
    "EvaluationStatus",
    "ExampleDocument",
    "ExampleEvaluator",
    "ExampleLocation",
    "FastMCPConfigCollector",
    "FastMCPConfigEvaluator",
    "JustfileRecipeCollector",
    "MarkdownFenceCollector",
    "MarkdownPythonPageCollector",
    "PythonCodeEvaluator",
    "PythonDocstringCollector",
    "PythonPageEvaluator",
    "SandboxBackend",
    "SandboxCommandPlan",
    "SandboxExecution",
    "SandboxSeed",
    "SphinxDoctestEvaluator",
    "TempHomeSandbox",
    "collect_examples",
    "redact_path",
]
