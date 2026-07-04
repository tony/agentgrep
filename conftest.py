"""Root pytest configuration for ``agentgrep``."""

from __future__ import annotations

import os
import pathlib

from pytest_documentation import (
    ConsoleCommandEvaluator,
    DocumentationSuite,
    FastMCPConfigCollector,
    FastMCPConfigEvaluator,
    JustfileRecipeCollector,
    MarkdownFenceCollector,
    MarkdownPythonPageCollector,
    PythonPageEvaluator,
    SandboxSeed,
    SphinxDoctestEvaluator,
    TempHomeSandbox,
)

_REPO_ROOT = pathlib.Path(__file__).parent.resolve()
_SAMPLES_ROOT = _REPO_ROOT / "tests" / "samples"


def _sample_seed(source: pathlib.Path, target: str) -> SandboxSeed:
    """Return a sandbox seed rooted at ``tests/samples``."""
    return SandboxSeed(source=source, target=pathlib.Path(target))


def _real_uv_cache_dir() -> pathlib.Path:
    """Resolve the developer's real (warm) uv cache, mirroring uv's defaults.

    Resolved before the sandbox rewrites ``XDG_CACHE_HOME`` to a temp home,
    so documentation commands hit a warm wheel cache instead of a cold
    per-sandbox one.

    Returns
    -------
    pathlib.Path
        ``$UV_CACHE_DIR``, else ``$XDG_CACHE_HOME/uv``, else ``~/.cache/uv``.
    """
    explicit = os.environ.get("UV_CACHE_DIR")
    if explicit:
        return pathlib.Path(explicit)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".cache"
    return base / "uv"


# Reuse the project's prebuilt venv (read-only) and the warm uv cache so doc
# examples skip the cold per-sandbox `uv run` build. This is what lets the
# docs suite run under pytest-xdist (`just test-fast`) without timing out; it
# also speeds serial runs. Falls back to per-sandbox isolation when the venv
# is absent. HOME / agent-data roots stay isolated either way.
_PROJECT_VENV = _REPO_ROOT / ".venv"
_SHARED_PROJECT_ENV = _PROJECT_VENV if _PROJECT_VENV.is_dir() else None

_DOCS_SANDBOX = TempHomeSandbox(
    project_root=_REPO_ROOT,
    cwd=_REPO_ROOT,
    uv_cache_dir=_real_uv_cache_dir(),
    uv_project_environment=_SHARED_PROJECT_ENV,
    # The VS Code / Cursor IDE WSL bridge probes the Windows users mount, which
    # is independent of the sandbox's temp ``$HOME`` (ADR 0009). Point it at a
    # nonexistent path so doc examples never read the developer's real host-side
    # chat, mirroring the ``tests/conftest.py`` autouse fixture.
    extra_env={"AGENTGREP_WSL_USERS_ROOT": str(_REPO_ROOT / "no-windows-mount")},
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
    include_paths=("README.md", "docs", "fastmcp.json"),
    # AGENTS.md (and its CLAUDE.md symlink) is agent guidance, not a runnable
    # doc page; keep the doc-suite from executing it, mirroring the Sphinx
    # exclude_patterns in docs/conf.py.
    exclude_parts=("_build", "AGENTS.md", "CLAUDE.md"),
)
_DOCS_SUITE.register_collector(MarkdownFenceCollector(languages={"console"}))
_DOCS_SUITE.register_collector(MarkdownPythonPageCollector())
_DOCS_SUITE.register_collector(FastMCPConfigCollector())
_DOCS_SUITE.register_collector(JustfileRecipeCollector(recipe_names={"doctest"}))
_DOCS_SUITE.register_evaluator("console", ConsoleCommandEvaluator(sandbox=_DOCS_SANDBOX))
_DOCS_SUITE.register_evaluator("python-page", PythonPageEvaluator(sandbox=_DOCS_SANDBOX))
_DOCS_SUITE.register_evaluator("fastmcp-config", FastMCPConfigEvaluator(project_root=_REPO_ROOT))
_DOCS_SUITE.register_evaluator("just-recipe", SphinxDoctestEvaluator(project_root=_REPO_ROOT))

pytest_collect_file = _DOCS_SUITE.pytest_collect_file


def pytest_ignore_collect(collection_path: pathlib.Path, config: object) -> bool | None:
    """Ignore generated documentation artifacts during pytest collection."""
    if "_build" in collection_path.parts:
        return True
    return None
