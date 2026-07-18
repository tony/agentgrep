"""Small Textual-free persistence seam for UI preferences."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import json
import os
import pathlib
import tempfile
import typing as t

__all__ = ["load_theme_name", "save_theme_name", "theme_config_path"]

_MAX_PREFERENCES_BYTES = 64 * 1024


def theme_config_path(
    *,
    environ: cabc.Mapping[str, str] | None = None,
    home: pathlib.Path | None = None,
) -> pathlib.Path:
    """Return the XDG file used for UI preferences.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str] | None
        Environment mapping; defaults to :data:`os.environ`.
    home : pathlib.Path | None
        Home directory fallback; defaults to :meth:`pathlib.Path.home`.

    Returns
    -------
    pathlib.Path
        The dedicated ``agentgrep/preferences.json`` path.
    """
    environment = os.environ if environ is None else environ
    root = environment.get("XDG_CONFIG_HOME")
    config_home = pathlib.Path(root) if root else (home or pathlib.Path.home()) / ".config"
    return config_home / "agentgrep" / "preferences.json"


def _load_payload(source: pathlib.Path) -> tuple[dict[str, t.Any], bool]:
    """Return one bounded JSON object and whether it is safe to update."""
    try:
        with source.open("rb") as stream:
            raw = stream.read(_MAX_PREFERENCES_BYTES + 1)
    except FileNotFoundError:
        return {}, True
    except OSError:
        return {}, False
    if len(raw) > _MAX_PREFERENCES_BYTES:
        return {}, False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeError, json.JSONDecodeError:
        return {}, False
    return (payload, True) if isinstance(payload, dict) else ({}, False)


def load_theme_name(path: pathlib.Path | None = None) -> str | None:
    """Load the persisted theme name, returning ``None`` for bad state.

    Parameters
    ----------
    path : pathlib.Path | None
        Explicit preferences file for tests or embedding.

    Returns
    -------
    str | None
        The persisted non-empty theme identifier, if one can be read.
    """
    source = theme_config_path() if path is None else path
    payload, _can_update = _load_payload(source)
    ui = payload.get("ui")
    if not isinstance(ui, dict):
        return None
    value = ui.get("theme")
    return value if isinstance(value, str) and value else None


def _fsync_directory(path: pathlib.Path) -> None:
    """Flush the parent directory after an atomic replacement."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_theme_name(theme_name: str, path: pathlib.Path | None = None) -> bool:
    """Atomically merge and persist one selected theme name.

    Parameters
    ----------
    theme_name : str
        Stable identifier from the owned theme catalog.
    path : pathlib.Path | None
        Explicit preferences file for tests or embedding.

    Returns
    -------
    bool
        ``True`` after a durable replacement, otherwise ``False``.
    """
    destination = theme_config_path() if path is None else path
    payload, can_update = _load_payload(destination)
    if not can_update:
        return False
    current_ui = payload.get("ui")
    ui = dict(current_ui) if isinstance(current_ui, dict) else {}
    ui["theme"] = theme_name
    payload["ui"] = ui

    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    except OSError:
        return False

    temporary = pathlib.Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor_open = False
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(destination)
        with contextlib.suppress(OSError):
            _fsync_directory(destination.parent)
    except OSError, TypeError, ValueError:
        if descriptor_open:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        return False
    return True
