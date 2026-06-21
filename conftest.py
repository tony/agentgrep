"""Root pytest configuration for ``agentgrep``."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import os
import pathlib
import typing as t

import pytest

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
    exclude_parts=("_build",),
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

_AGENTGREP_OTEL_PYTEST_HANDLE: object | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Configure OTel once for explicitly instrumented pytest runs."""
    del session
    global _AGENTGREP_OTEL_PYTEST_HANDLE
    if "AGENTGREP_OTEL" not in os.environ:
        return
    from agentgrep import _telemetry

    _AGENTGREP_OTEL_PYTEST_HANDLE = _telemetry.setup(
        repo_root=_REPO_ROOT,
        service_name="agentgrep-pytest",
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Flush OTel after an explicitly instrumented pytest run."""
    del session, exitstatus
    global _AGENTGREP_OTEL_PYTEST_HANDLE
    handle = _AGENTGREP_OTEL_PYTEST_HANDLE
    _AGENTGREP_OTEL_PYTEST_HANDLE = None
    if handle is not None:
        t.cast("t.Any", handle).shutdown()


def _agentgrep_pytest_span_attributes(item: object) -> dict[str, object]:
    """Return low-cardinality pytest span attributes."""
    attributes: dict[str, object] = {
        "agentgrep_surface": "pytest",
        "agentgrep_pytest_test": getattr(item, "nodeid", "<unknown>"),
    }
    config = getattr(item, "config", None)
    workerinput = getattr(config, "workerinput", None)
    option = getattr(config, "option", None)
    dist = getattr(option, "dist", None)
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is None and isinstance(workerinput, cabc.Mapping):
        raw_worker_id = workerinput.get("workerid")
        if isinstance(raw_worker_id, str):
            worker_id = raw_worker_id
    xdist_active = bool(worker_id or workerinput or dist)
    attributes["agentgrep_pytest_xdist"] = xdist_active
    if worker_id:
        attributes["agentgrep_pytest_worker_id"] = worker_id
    if dist:
        attributes["agentgrep_pytest_dist"] = str(dist)
    return attributes


@contextlib.contextmanager
def _agentgrep_otel_pytest_item_span(item: object) -> cabc.Iterator[None]:
    """Create a non-single-root trace for one collected pytest item."""
    from agentgrep import _telemetry

    if _telemetry.active_backend() is None:
        yield
        return
    attributes = _agentgrep_pytest_span_attributes(item)
    with (
        _telemetry.span(
            "agentgrep.pytest.test",
            **attributes,
        ),
        _telemetry.span(
            "agentgrep.pytest.call",
            **attributes,
        ),
    ):
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item,
    nextitem: pytest.Item | None,
) -> cabc.Iterator[None]:
    """Wrap every collected item, including custom documentation items."""
    del nextitem
    if _AGENTGREP_OTEL_PYTEST_HANDLE is None:
        yield
        return
    with _agentgrep_otel_pytest_item_span(item):
        yield


def pytest_ignore_collect(collection_path: pathlib.Path, config: object) -> bool | None:
    """Ignore generated documentation artifacts during pytest collection."""
    if "_build" in collection_path.parts:
        return True
    return None
