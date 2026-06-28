"""Cache and model directory resolution for insights.

Implements the ADR 0005 § *Model Provisioning* precedence with the
standard library only, so the base install never depends on
``platformdirs``:

1. ``AGENTGREP_MODEL_DIR`` for model artifacts.
2. ``AGENTGREP_CACHE_DIR`` for indexes and report caches.
3. Platform cache directories.
4. ``XDG_CACHE_HOME`` on Unix-like systems.
5. ``LOCALAPPDATA`` on Windows.
6. ``~/Library/Caches`` on macOS.
7. ``~/.cache/agentgrep`` as the final fallback.
"""

from __future__ import annotations

import os
import pathlib
import sys
import typing as t

_APP_NAME = "agentgrep"


def _env_path(name: str) -> pathlib.Path | None:
    """Return ``$name`` as an expanded path, or ``None`` when unset/empty."""
    value = os.environ.get(name)
    if not value:
        return None
    return pathlib.Path(value).expanduser()


def platform_cache_root() -> pathlib.Path:
    """Return the platform cache root for agentgrep (no env overrides).

    This is precedence steps 3-7: ``XDG_CACHE_HOME`` on Unix,
    ``LOCALAPPDATA`` on Windows, ``~/Library/Caches`` on macOS, and
    ``~/.cache/agentgrep`` as the final fallback.
    """
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Caches" / _APP_NAME
    if sys.platform == "win32":
        local = _env_path("LOCALAPPDATA")
        if local is not None:
            return local / _APP_NAME / "Cache"
        return pathlib.Path.home() / "AppData" / "Local" / _APP_NAME / "Cache"
    xdg = _env_path("XDG_CACHE_HOME")
    if xdg is not None:
        return xdg / _APP_NAME
    return pathlib.Path.home() / ".cache" / _APP_NAME


def cache_dir() -> pathlib.Path:
    """Return the cache root for indexes and report caches.

    ``AGENTGREP_CACHE_DIR`` wins; otherwise the platform cache root.
    """
    override = _env_path("AGENTGREP_CACHE_DIR")
    if override is not None:
        return override
    return platform_cache_root()


def model_cache_dir() -> pathlib.Path:
    """Return the directory for downloaded model artifacts.

    ``AGENTGREP_MODEL_DIR`` wins; otherwise ``<cache_dir>/models``.
    """
    override = _env_path("AGENTGREP_MODEL_DIR")
    if override is not None:
        return override
    return cache_dir() / "models"


def index_cache_dir() -> pathlib.Path:
    """Return the directory for persistent insights indexes."""
    return cache_dir() / "index"


def ensure_dir(path: pathlib.Path) -> pathlib.Path:
    """Create ``path`` (and parents) if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def directory_size_bytes(path: pathlib.Path) -> int:
    """Return the total size in bytes of every file under ``path``.

    Returns ``0`` when ``path`` does not exist. Broken symlinks and
    files removed mid-walk are skipped rather than raising.
    """
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def human_size(num_bytes: int) -> str:
    """Format a byte count as a short human-readable string.

    Examples
    --------
    >>> human_size(0)
    '0 B'
    >>> human_size(1536)
    '1.5 KiB'
    >>> human_size(5 * 1024 * 1024)
    '5.0 MiB'
    """
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    # Unreachable: the loop always returns at "TiB".
    return f"{size:.1f} TiB"


class CachePruneResult(t.NamedTuple):
    """Outcome of a cache prune pass."""

    removed_paths: tuple[pathlib.Path, ...]
    reclaimed_bytes: int


def prune_cache(*, dry_run: bool = False) -> CachePruneResult:
    """Remove insights index/report caches, leaving model artifacts.

    Model downloads are expensive and explicitly provisioned, so prune
    only the regenerable index/report caches by default. Returns the
    paths that were (or would be) removed and the bytes reclaimed.
    """
    import shutil

    targets = [index_cache_dir(), cache_dir() / "reports"]
    removed: list[pathlib.Path] = []
    reclaimed = 0
    for target in targets:
        if not target.exists():
            continue
        reclaimed += directory_size_bytes(target)
        removed.append(target)
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
    return CachePruneResult(removed_paths=tuple(removed), reclaimed_bytes=reclaimed)
