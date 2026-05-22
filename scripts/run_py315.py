#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run a command inside a Python 3.15.0b1 venv with ecosystem workarounds.

Use this when you need 3.15-only behavior (the tachyon sampling profiler
in :mod:`profiling.sampling` is the headline reason) without disturbing
the project's main `.venv` — daily dev stays on the 3.14 venv that uv
syncs against the standard wheel set. This script materializes a side
venv at ``.venv-tachyon/`` against 3.15.0b1, syncs the project into it
with every known build workaround applied, then runs the command you
pass in inside that venv.

The first run takes a few minutes because pyo3-based dependencies
(``rpds-py``, ``pydantic-core``, ``rapidfuzz``) ship no 3.15 wheels yet
and must compile from sdist. Subsequent runs reuse the venv and the uv
build cache — they're near-instant.

Workarounds applied
-------------------
- ``PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`` — tells pyo3 it's OK to
  build against 3.15 via the CPython stable ABI (PEP 384). pyo3 0.27
  predates 3.15 and refuses to build by default; the env var unlocks
  the abi3 forward-compat path.
- ``--prerelease=allow`` — opts uv's resolver into prerelease
  candidates of transitive deps (e.g. ``pydantic`` 2.14.0a1, which is
  the first release line carrying pyo3 0.28+ sources that compile
  cleanly under 3.15).

Usage
-----
::

    python scripts/run_py315.py                       # interactive REPL
    python scripts/run_py315.py -c 'import sys; print(sys.version)'
    python scripts/run_py315.py -m agentgrep search bliss
    python scripts/run_py315.py -m profiling.sampling --help

The script is stdlib-only so it runs under any Python ≥3.10 — including
the project's own 3.14 venv. It does not depend on the very interpreter
it bootstraps.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
VENV = REPO_ROOT / ".venv-tachyon"
PYTHON_VERSION = "3.15.0b1"


def _build_env() -> dict[str, str]:
    """Compose the build/run env: parent env + workaround variables.

    Returns a fresh dict so the caller can mutate it without leaking
    back into ``os.environ``. ``UV_PROJECT_ENVIRONMENT`` redirects uv's
    sync target to our side venv instead of the project's default
    ``.venv``.
    """
    return {
        **os.environ,
        "PYO3_USE_ABI3_FORWARD_COMPATIBILITY": "1",
        "UV_PROJECT_ENVIRONMENT": str(VENV),
    }


def _ensure_venv() -> int:
    """Bootstrap ``.venv-tachyon/`` if it doesn't exist; return uv's exit code.

    Idempotent: subsequent calls short-circuit when the venv is already
    built. We don't try to detect a partially-built venv; running
    ``rm -rf .venv-tachyon`` and re-invoking the script is the supported
    recovery path.
    """
    python_path = VENV / "bin" / "python"
    if python_path.exists():
        return 0
    if shutil.which("uv") is None:
        sys.stderr.write("run_py315: uv not found in PATH\n")
        return 2
    sys.stderr.write(f"run_py315: bootstrapping {VENV} against {PYTHON_VERSION}\n")
    env = _build_env()
    rc = subprocess.run(
        ["uv", "venv", "--python", PYTHON_VERSION, str(VENV)],
        env=env,
        cwd=REPO_ROOT,
        check=False,
    ).returncode
    if rc != 0:
        return rc
    return subprocess.run(
        [
            "uv",
            "sync",
            "--all-extras",
            "--dev",
            "--prerelease=allow",
        ],
        env=env,
        cwd=REPO_ROOT,
        check=False,
    ).returncode


def main(argv: list[str]) -> int:
    """Bootstrap the venv if needed, then run ``argv`` under its python."""
    rc = _ensure_venv()
    if rc != 0:
        return rc
    python = VENV / "bin" / "python"
    if not argv:
        # No args: drop into the venv's interactive REPL.
        return subprocess.run([str(python)], check=False).returncode
    return subprocess.run([str(python), *argv], check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
