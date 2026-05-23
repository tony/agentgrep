"""Command-line interface for agentgrep.

The CLI surface (argument parsing, subcommand dispatch, output rendering)
lives in this subpackage. The library surface (search/find engines,
record types, source discovery) stays in :mod:`agentgrep`.

This module re-exports :func:`agentgrep.main` so the console-script
entry point (``agentgrep.cli:main``) resolves through a single
canonical path. Argument parsing lives in :mod:`agentgrep.cli.parser`;
output rendering and subcommand dispatch in :mod:`agentgrep.cli.render`.
"""

from __future__ import annotations

from agentgrep import main

__all__ = ["main"]
