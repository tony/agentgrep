"""Command-line interface for agentgrep.

The CLI surface (argument parsing, subcommand dispatch, output rendering)
lives in this subpackage. The library surface (search/find engines,
record types, source discovery) stays in :mod:`agentgrep`.

This module currently re-exports :func:`agentgrep.main` so console-script
and ``python -m agentgrep`` entry points can resolve through a single
canonical path. The internals will migrate into ``cli/parser.py`` and
``cli/render.py`` in subsequent commits.
"""

from __future__ import annotations

from agentgrep import main

__all__ = ["main"]
