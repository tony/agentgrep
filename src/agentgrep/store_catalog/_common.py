"""Shared dated constants and the gemini project-hash helper."""

from __future__ import annotations

import datetime
import hashlib
import pathlib

OBSERVED_AT = datetime.date(2026, 5, 17)
_GROK_OBSERVED_AT = datetime.date(2026, 6, 21)
_CLAUDE_OBSERVED_AT = datetime.date(2026, 6, 21)
_CURSOR_IDE_OBSERVED_AT = datetime.date(2026, 6, 21)
_PI_OBSERVED_AT = datetime.date(2026, 6, 21)
_OPENCODE_OBSERVED_AT = datetime.date(2026, 6, 21)
_ANTIGRAVITY_OBSERVED_AT = datetime.date(2026, 6, 21)
_GEMINI_OBSERVED_AT = datetime.date(2026, 6, 21)
_CURSOR_CLI_OBSERVED_AT = datetime.date(2026, 6, 21)
_CODEX_OBSERVED_AT = datetime.date(2026, 6, 21)
_WINDSURF_OBSERVED_AT = datetime.date(2026, 6, 21)
_VSCODE_OBSERVED_AT = datetime.date(2026, 6, 21)


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
