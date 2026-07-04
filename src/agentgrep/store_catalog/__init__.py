"""Concrete catalogue of agentgrep's known stores.

Assembles the per-agent store descriptors into the single :data:`CATALOG`
that adapters and discovery consume. The per-agent tables live in sibling
modules; this package facade only aggregates them.
"""

from __future__ import annotations

from agentgrep.store_catalog._common import _VSCODE_OBSERVED_AT, OBSERVED_AT, gemini_project_hash
from agentgrep.store_catalog.antigravity_cli import _ANTIGRAVITY_CLI_STORES
from agentgrep.store_catalog.antigravity_ide import _ANTIGRAVITY_IDE_STORES
from agentgrep.store_catalog.claude import _CLAUDE_STORES
from agentgrep.store_catalog.codex import _CODEX_STORES
from agentgrep.store_catalog.cursor_cli import _CURSOR_CLI_STORES
from agentgrep.store_catalog.cursor_ide import _CURSOR_IDE_STORES
from agentgrep.store_catalog.gemini import _GEMINI_STORES
from agentgrep.store_catalog.grok import _GROK_STORES
from agentgrep.store_catalog.opencode import _OPENCODE_STORES
from agentgrep.store_catalog.pi import _PI_STORES
from agentgrep.store_catalog.vscode import _VSCODE_STORES
from agentgrep.store_catalog.windsurf import _WINDSURF_STORES
from agentgrep.stores import StoreCatalog

CATALOG = StoreCatalog(
    catalog_version=34,
    captured_at=_VSCODE_OBSERVED_AT,
    stores=(
        *_CLAUDE_STORES,
        *_CURSOR_CLI_STORES,
        *_CURSOR_IDE_STORES,
        *_CODEX_STORES,
        *_GEMINI_STORES,
        *_ANTIGRAVITY_CLI_STORES,
        *_ANTIGRAVITY_IDE_STORES,
        *_GROK_STORES,
        *_PI_STORES,
        *_OPENCODE_STORES,
        *_WINDSURF_STORES,
        *_VSCODE_STORES,
    ),
)
"""The canonical agentgrep store catalogue.

This is the single source of truth for *where* agent data lives on disk and
*what shape* its records take. Adapters consume :class:`CATALOG`; the
catalogue itself does not depend on any adapter code.
"""


__all__ = (
    "CATALOG",
    "OBSERVED_AT",
    "gemini_project_hash",
)
