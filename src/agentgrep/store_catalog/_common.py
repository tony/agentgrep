"""Shared dated constants and the gemini project-hash helper."""

from __future__ import annotations

import datetime
import hashlib
import pathlib
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.stores import StoreDescriptor

OBSERVED_AT = datetime.date(2026, 5, 17)
_CATALOG_CAPTURED_AT = datetime.date(2026, 10, 25)

# Every agent below was re-observed against its installed version on
# 2026-10-25. The recorded shapes live in
# ``docs/_observations/<agent>/<version>.toml``; re-run
# ``scripts/observe_stores.py check --agent all`` to test these dates against
# live disk rather than trusting them.
_GROK_OBSERVED_AT = datetime.date(2026, 10, 25)
_CLAUDE_OBSERVED_AT = datetime.date(2026, 10, 25)
_CURSOR_IDE_OBSERVED_AT = datetime.date(2026, 10, 25)
_PI_OBSERVED_AT = datetime.date(2026, 10, 25)
_OPENCODE_OBSERVED_AT = datetime.date(2026, 10, 25)
_ANTIGRAVITY_CLI_OBSERVED_AT = datetime.date(2026, 10, 25)
_ANTIGRAVITY_IDE_OBSERVED_AT = datetime.date(2026, 10, 25)
_GEMINI_OBSERVED_AT = datetime.date(2026, 10, 25)
_CURSOR_CLI_OBSERVED_AT = datetime.date(2026, 10, 25)
_CODEX_OBSERVED_AT = datetime.date(2026, 10, 25)
_VSCODE_OBSERVED_AT = datetime.date(2026, 10, 25)

# Windsurf keeps its own date. Its newest build (2.3.15, read from the
# remote server's product.json) wrote none of the catalogued data: the
# newest transcript is from 2025, so no stamp can claim that build.
_WINDSURF_OBSERVED_AT = datetime.date(2026, 8, 8)

# ChatGPT desktop was checked on macOS and Windows on 2026-11-14; both
# builds write no version into their data (see store_catalog/chatgpt.py).
_CHATGPT_OBSERVED_AT = datetime.date(2026, 11, 14)


def gemini_project_hash(project_root: pathlib.Path) -> str:
    """Reproduce Gemini CLI's project-hash derivation.

    Mirrors the ``getProjectHash`` helper at
    ``packages/core/src/utils/paths.ts:187-189`` in
    ``github.com/google-gemini/gemini-cli`` (HEAD ``927170fc``):

    .. code-block:: typescript

       export function getProjectHash(projectRoot: string): string {
         return crypto.createHash('sha256').update(projectRoot).digest('hex');
       }

    Parameters
    ----------
    project_root : pathlib.Path
        Absolute project root path.

    Returns
    -------
    str
        Lower-case hex SHA-256 of the absolute path string.

    Examples
    --------
    >>> gemini_project_hash(pathlib.Path("/example"))
    '99d0533064c83d0483dc07145a0aa887cb104311dac8cc2ca57843c6723a5b69'
    """
    return hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()


def with_windows_home_variants(stores: tuple[StoreDescriptor, ...]) -> tuple[StoreDescriptor, ...]:
    """Add a ``win32`` location to every home-relative row that has none.

    For agents that resolve their data directory from the user's home on
    Windows exactly as on Linux, the Windows location is the row's pattern with
    ``%USERPROFILE%`` in place of ``${HOME}``. Rows that are not home-relative
    (a project tree) keep no Windows variant.

    Examples
    --------
    >>> from agentgrep.store_catalog import CATALOG
    >>> CATALOG.by_id("grok.config").platform_variants["win32"]
    '${GROK_HOME or %USERPROFILE%/.grok}/config.toml'
    """
    return tuple(
        store.model_copy(
            update={
                "platform_variants": {
                    **store.platform_variants,
                    "win32": store.path_pattern.replace("${HOME}", "%USERPROFILE%"),
                },
            },
        )
        if "${HOME}" in store.path_pattern and "win32" not in store.platform_variants
        else store
        for store in stores
    )
