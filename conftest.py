"""Root pytest configuration for ``agentgrep``."""

from __future__ import annotations

import pathlib

from pytest_documentation import (
    ConsoleCommandEvaluator,
    DocumentationSuite,
    MarkdownFenceCollector,
    SandboxSeed,
    TempHomeSandbox,
)

_REPO_ROOT = pathlib.Path(__file__).parent.resolve()
_SAMPLES_ROOT = _REPO_ROOT / "tests" / "samples"


def _sample_seed(source: pathlib.Path, target: str) -> SandboxSeed:
    """Return a sandbox seed rooted at ``tests/samples``."""
    return SandboxSeed(source=source, target=pathlib.Path(target))


_DOCS_SANDBOX = TempHomeSandbox(
    project_root=_REPO_ROOT,
    cwd=_REPO_ROOT,
    seeds=(
        _sample_seed(
            _SAMPLES_ROOT / "docs" / "codex-history.jsonl",
            ".codex/history.jsonl",
        ),
        _sample_seed(
            _SAMPLES_ROOT / "docs" / "codex-session.jsonl",
            ".codex/sessions/2026/02/01/rollout-2026-02-01T12-00-00-docs.jsonl",
        ),
        _sample_seed(
            _SAMPLES_ROOT / "claude" / "claude.history" / "history.jsonl",
            ".claude/history.jsonl",
        ),
    ),
)
_DOCS_SUITE = DocumentationSuite(
    project_root=_REPO_ROOT,
    include_paths=("docs",),
    exclude_parts=("_build",),
)
_DOCS_SUITE.register_collector(MarkdownFenceCollector(languages={"console"}))
_DOCS_SUITE.register_evaluator("console", ConsoleCommandEvaluator(sandbox=_DOCS_SANDBOX))

pytest_collect_file = _DOCS_SUITE.pytest_collect_file


def pytest_ignore_collect(collection_path: pathlib.Path, config: object) -> bool | None:
    """Ignore generated documentation artifacts during pytest collection."""
    if "_build" in collection_path.parts:
        return True
    return None
