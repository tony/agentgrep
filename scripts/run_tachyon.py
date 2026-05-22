#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Profile a Python module under the tachyon sampling profiler.

Thin wrapper over :mod:`profiling.sampling` (new in Python 3.15) that
routes through :file:`scripts/run_py315.py` so the profiled process and
the profiler itself both run inside the ``.venv-tachyon/`` side venv.
The wrapper exists to spare callers the two-step ritual of "bootstrap
the side venv, then remember the profiler's invocation syntax."

The default target is ``agentgrep`` (i.e. the ``-m agentgrep`` console
entry). Any module that resolves inside the 3.15 venv works — pass
``--target`` to override.

The tachyon profiler is a sampling profiler from `PEP 768`_: it walks
the interpreter's frame stack at a fixed cadence rather than
instrumenting every call, so per-sample overhead is low and the output
shape lines up with flamegraph viewers natively. ``--format`` defaults
to the HTML flamegraph for one-shot visual exploration; pass
``--format pstats`` (etc.) for programmatic consumers.

.. _PEP 768: https://peps.python.org/pep-0768/

Usage
-----
::

    # Default: profile `agentgrep --help` to a flamegraph (HTML)
    python scripts/run_tachyon.py -- --help

    # Profile a real search
    python scripts/run_tachyon.py -- search agent:codex bliss

    # Custom output, different format
    python scripts/run_tachyon.py --format pstats -o profile.txt -- search bliss

    # Profile a non-agentgrep module
    python scripts/run_tachyon.py --target mymodule -- arg1 arg2

Output formats
--------------
- ``flamegraph`` (default) — HTML flamegraph, ``--browser`` to auto-open
- ``pstats`` — plain-text profile suitable for ``pstats.Stats``
- ``jsonl`` — newline-delimited JSON for programmatic consumers
- ``binary`` — high-perf binary format (use ``profiling.sampling
  replay`` to convert later)

The profiler's other knobs (``--rate``, ``--duration``, ``--mode``,
``--native``, ``--async-aware``, …) are passthrough — anything not
recognized as a wrapper flag is forwarded verbatim to
``profiling.sampling run``.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN_PY315 = REPO_ROOT / "scripts" / "run_py315.py"


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Split argv into wrapper args and target args (everything after ``--``)."""
    parser = argparse.ArgumentParser(
        prog="run_tachyon",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        default="agentgrep",
        help="Module to profile (default: agentgrep)",
    )
    parser.add_argument(
        "--format",
        choices=["flamegraph", "pstats", "jsonl", "binary", "collapsed", "gecko"],
        default="flamegraph",
        help="Profiler output format (default: flamegraph)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output path (default: profiler picks based on format)",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Open HTML output in a browser when format=flamegraph",
    )
    parser.add_argument(
        "--rate",
        default=None,
        help="Sampling rate (samples per second; passthrough to profiler)",
    )
    parser.add_argument(
        "passthrough",
        nargs=argparse.REMAINDER,
        help="Args after `--` are forwarded to the target module",
    )
    args = parser.parse_args(argv)
    target_args = list(args.passthrough)
    if target_args and target_args[0] == "--":
        target_args = target_args[1:]
    return args, target_args


def _build_profiler_argv(args: argparse.Namespace, target_args: list[str]) -> list[str]:
    """Assemble the ``profiling.sampling run`` argv from parsed wrapper args."""
    cmd: list[str] = ["-m", "profiling.sampling", "run", f"--{args.format}"]
    if args.output is not None:
        cmd += ["-o", args.output]
    if args.browser:
        cmd.append("--browser")
    if args.rate is not None:
        cmd += ["--rate", args.rate]
    cmd += ["-m", args.target]
    cmd += target_args
    return cmd


def main(argv: list[str]) -> int:
    """Parse wrapper args and dispatch through ``run_py315.py``."""
    args, target_args = _parse_args(argv)
    profiler_argv = _build_profiler_argv(args, target_args)
    return subprocess.run(
        [sys.executable, str(RUN_PY315), *profiler_argv],
        check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
