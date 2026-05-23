"""Import-chain regression test for agentgrep CLI cold-start.

Verifies that ``import agentgrep.cli`` does NOT eagerly load heavy
submodules (query, events, fuzzy) — those should only load on first
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
    "agentgrep.fuzzy",
)


class DeferredImportCase(t.NamedTuple):
    """One module that must NOT be in sys.modules after ``import agentgrep``."""

    test_id: str
    module: str


DEFERRED_CASES: tuple[DeferredImportCase, ...] = tuple(
    DeferredImportCase(test_id=mod.replace(".", "-"), module=mod) for mod in _DEFERRED_MODULES
)


@pytest.mark.parametrize(
    "case",
    DEFERRED_CASES,
    ids=[c.test_id for c in DEFERRED_CASES],
)
def test_import_agentgrep_does_not_eagerly_load(case: DeferredImportCase) -> None:
    """``import agentgrep`` must not pull in heavy submodules at startup."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (f"import agentgrep, sys; exit(0 if {case.module!r} not in sys.modules else 1)"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{case.module} was eagerly imported by `import agentgrep`; "
        f"it should be deferred to first use. "
        f"Check for top-level `from {case.module} import ...` in the "
        f"startup chain (cli/parser.py, cli/render.py, _engine/*.py)."
    )
