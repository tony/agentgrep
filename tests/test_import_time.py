"""Import-chain regression test for agentgrep CLI cold-start.

Verifies that ``import agentgrep`` does NOT eagerly load heavy
submodules (query, events) — those should only load on first
use of the subcommand that needs them. This is a deterministic
module-presence check, not a flaky timing test: it asserts against
``sys.modules`` in a subprocess with a clean module cache.

See ``AGENTS.md`` § *Lazy imports for CLI cold-start* for the
convention this test enforces.
"""

from __future__ import annotations

import subprocess
import sys
import typing as t

import pytest

_DEFERRED_MODULES: tuple[str, ...] = (
    "agentgrep.query",
    "agentgrep.query.parser",
    "agentgrep.query.compile",
    "agentgrep.query.ast",
    "agentgrep.events",
    # Insights stays lazy: importing agentgrep must not load the insights
    # package or any optional enrichment backend (ADR 0005 § Dependency Levels).
    "agentgrep.insights",
    "sklearn",
    "torch",
    "sentence_transformers",
    "model2vec",
    "tantivy",
    "sqlite_vec",
    "httpx",
    "jinja2",
    "huggingface_hub",
    "litert_lm",
)


class DeferredImportCase(t.NamedTuple):
    """One module that must NOT be in sys.modules after ``import agentgrep``."""

    test_id: str
    module: str


DEFERRED_CASES: tuple[DeferredImportCase, ...] = tuple(
    DeferredImportCase(test_id=mod.replace(".", "-"), module=mod) for mod in _DEFERRED_MODULES
)


@pytest.fixture(scope="module")
def modules_after_bare_import() -> frozenset[str]:
    """Modules present in ``sys.modules`` after a bare ``import agentgrep``.

    Captured once, in a single fresh interpreter, and shared across the
    per-module assertions below — one subprocess spawn instead of one per
    parametrized case. A fresh interpreter is required (you cannot un-import
    to re-test in-process), but all cases interrogate the *same* post-import
    snapshot, so one spawn suffices.

    Returns
    -------
    frozenset[str]
        The names in ``sys.modules`` after ``import agentgrep``.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agentgrep, sys; print('\\n'.join(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return frozenset(result.stdout.split())


@pytest.mark.parametrize(
    "case",
    DEFERRED_CASES,
    ids=[c.test_id for c in DEFERRED_CASES],
)
def test_import_agentgrep_does_not_eagerly_load(
    case: DeferredImportCase,
    modules_after_bare_import: frozenset[str],
) -> None:
    """``import agentgrep`` must not pull in heavy submodules at startup."""
    assert case.module not in modules_after_bare_import, (
        f"{case.module} was eagerly imported by `import agentgrep`; "
        f"it should be deferred to first use. "
        f"Check for top-level `from {case.module} import ...` in the "
        f"startup chain (cli/parser.py, cli/render.py, _engine/*.py)."
    )
