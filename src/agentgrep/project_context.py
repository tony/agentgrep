"""Current-project context detection for opt-in origin filters."""

from __future__ import annotations

import dataclasses
import pathlib
import typing as t

from agentgrep.records import RecordOrigin

__all__ = ["ProjectContext", "detect_project_context"]


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectContext:
    """Best-effort description of the invoking project."""

    cwd: pathlib.Path
    repo: pathlib.Path | None = None
    worktree: pathlib.Path | None = None
    git_dir: pathlib.Path | None = None
    branch: str | None = None

    @property
    def origin(self) -> RecordOrigin:
        """Return this context as a record-origin filter/boost."""
        return RecordOrigin(
            cwd=str(self.cwd),
            repo=str(self.repo) if self.repo is not None else None,
            worktree=str(self.worktree) if self.worktree is not None else None,
            branch=self.branch,
        )


def detect_project_context(cwd: pathlib.Path | None = None) -> ProjectContext:
    """Detect the current project without spawning subprocesses.

    The detector only walks upward from ``cwd`` looking for ``.git``. It reads
    ``.git/HEAD`` directly when present, and supports both normal repositories
    and worktrees whose ``.git`` is a ``gitdir:`` pointer file.
    """
    current = (cwd or pathlib.Path.cwd()).expanduser().resolve(strict=False)
    worktree, git_dir = _find_git_root(current)
    if worktree is None:
        return ProjectContext(cwd=current)
    branch = _read_git_branch(git_dir)
    repo = _read_common_git_worktree(git_dir) or worktree
    return ProjectContext(
        cwd=current,
        repo=repo,
        worktree=worktree,
        git_dir=git_dir,
        branch=branch,
    )


def _find_git_root(start: pathlib.Path) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return candidate, dot_git
        if dot_git.is_file():
            git_dir = _read_gitdir_file(dot_git, base=candidate)
            if git_dir is not None:
                return candidate, git_dir
    return None, None


def _read_gitdir_file(path: pathlib.Path, *, base: pathlib.Path) -> pathlib.Path | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not raw.startswith(prefix):
        return None
    value = raw[len(prefix) :].strip()
    if not value:
        return None
    git_dir = pathlib.Path(value)
    if not git_dir.is_absolute():
        git_dir = base / git_dir
    return git_dir.expanduser().resolve(strict=False)


def _read_git_branch(git_dir: pathlib.Path | None) -> str | None:
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if head.startswith(prefix):
        return head[len(prefix) :].strip() or None
    if head:
        return head[:12]
    return None


def _read_common_git_worktree(git_dir: pathlib.Path | None) -> pathlib.Path | None:
    if git_dir is None:
        return None
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not common:
        return None
    common_path = pathlib.Path(common)
    if not common_path.is_absolute():
        common_path = git_dir / common_path
    common_path = common_path.expanduser().resolve(strict=False)
    if common_path.name == ".git":
        return common_path.parent
    return t.cast("pathlib.Path", None)
