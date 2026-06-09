"""Root pytest configuration for ``agentgrep``."""

from __future__ import annotations

import pathlib

from pytest_documentation import (
    ConsoleCommandEvaluator,
    DocumentationSuite,
    FastMCPConfigCollector,
    FastMCPConfigEvaluator,
    JustfileRecipeCollector,
    MarkdownFenceCollector,
    SandboxSeed,
    SphinxDoctestEvaluator,
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
        _sample_seed(
            _SAMPLES_ROOT / "cursor-cli" / "cursor-cli.prompt_history" / "prompt_history.json",
            ".config/cursor/prompt_history.json",
        ),
        _sample_seed(
            _SAMPLES_ROOT / "cursor-cli" / "cursor-cli.transcripts" / "example.jsonl",
            ".cursor/projects/docs/agent-transcripts/docs/docs.jsonl",
        ),
    ),
)
_DOCS_SUITE = DocumentationSuite(
    project_root=_REPO_ROOT,
    include_paths=("docs", "fastmcp.json"),
    exclude_parts=("_build",),
)
_DOCS_SUITE.register_collector(MarkdownFenceCollector(languages={"console"}))
_DOCS_SUITE.register_collector(FastMCPConfigCollector())
_DOCS_SUITE.register_collector(JustfileRecipeCollector(recipe_names={"doctest"}))
_DOCS_SUITE.register_evaluator("console", ConsoleCommandEvaluator(sandbox=_DOCS_SANDBOX))
_DOCS_SUITE.register_evaluator("fastmcp-config", FastMCPConfigEvaluator(project_root=_REPO_ROOT))
_DOCS_SUITE.register_evaluator("just-recipe", SphinxDoctestEvaluator(project_root=_REPO_ROOT))

pytest_collect_file = _DOCS_SUITE.pytest_collect_file


def pytest_ignore_collect(collection_path: pathlib.Path, config: object) -> bool | None:
    """Ignore generated documentation artifacts during pytest collection."""
    if "_build" in collection_path.parts:
        return True
    return None
