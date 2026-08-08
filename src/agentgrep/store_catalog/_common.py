"""Shared dated constants and the gemini project-hash helper."""

from __future__ import annotations

import datetime
import hashlib
import pathlib

OBSERVED_AT = datetime.date(2026, 5, 17)
_CATALOG_CAPTURED_AT = datetime.date(2026, 8, 8)

# Every agent below was re-observed against its installed version on
# 2026-08-08. The recorded shapes live in ``observations/<agent>/<version>.toml``;
# re-run ``scripts/observe_stores.py check --agent all`` to test these dates
# against live disk rather than trusting them.
_GROK_OBSERVED_AT = datetime.date(2026, 8, 8)
_CLAUDE_OBSERVED_AT = datetime.date(2026, 8, 8)
_CURSOR_IDE_OBSERVED_AT = datetime.date(2026, 8, 8)
_PI_OBSERVED_AT = datetime.date(2026, 8, 8)
_OPENCODE_OBSERVED_AT = datetime.date(2026, 8, 8)
_ANTIGRAVITY_OBSERVED_AT = datetime.date(2026, 8, 8)
_GEMINI_OBSERVED_AT = datetime.date(2026, 8, 8)
_CURSOR_CLI_OBSERVED_AT = datetime.date(2026, 8, 8)
_CODEX_OBSERVED_AT = datetime.date(2026, 8, 8)
_VSCODE_OBSERVED_AT = datetime.date(2026, 8, 8)

# Windsurf is catalogued but not installed here: the ${HOME}/.codeium/windsurf
# tree exists and was walked, so the date is a real observation of the layout,
# but no app version was readable to stamp against it.
_WINDSURF_OBSERVED_AT = datetime.date(2026, 8, 8)


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
