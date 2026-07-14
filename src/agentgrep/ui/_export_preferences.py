"""Bounded TUI export preferences and filename compilation.

This module is deliberately Textual-free. Callers offload its filesystem I/O
and pass the resulting immutable values into the TUI.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import ntpath
import os
import pathlib
import tempfile
import typing as t
import unicodedata

DEFAULT_FILENAME_TEMPLATE = "{date} {time} - {title}.md"
MAX_PREFERENCES_BYTES = 16 * 1024
MAX_TEMPLATE_CHARS = 256
MAX_FILENAME_BYTES = 180

_PREFERENCES_WARNING = "Export preferences could not be read"
_PREFERENCES_SAVE_ERROR = "Export preferences could not be saved"
_FILENAME_ERROR = "Export filename is invalid"
_SCHEMA_KEYS = frozenset({"version", "directory", "filename_template"})

__all__ = [
    "DEFAULT_FILENAME_TEMPLATE",
    "MAX_FILENAME_BYTES",
    "MAX_PREFERENCES_BYTES",
    "MAX_TEMPLATE_CHARS",
    "ExportPreferences",
    "ExportPreferencesError",
    "ExportPreferencesLoad",
    "default_export_directory",
    "export_preferences_path",
    "load_export_preferences",
    "render_export_filename",
    "resolve_export_directory",
    "save_export_preferences",
]


@dataclasses.dataclass(frozen=True, slots=True)
class ExportPreferences:
    """Persisted values for the TUI export dialog."""

    directory: str
    filename_template: str = DEFAULT_FILENAME_TEMPLATE


@dataclasses.dataclass(frozen=True, slots=True)
class ExportPreferencesLoad:
    """Loaded preferences plus an optional path-free warning."""

    preferences: ExportPreferences
    warning: str | None = None


class ExportPreferencesError(Exception):
    """A path-free preference or filename failure."""


def _xdg_path(variable: str, fallback: pathlib.Path) -> pathlib.Path:
    """Return a configured non-empty XDG root or ``fallback``."""
    configured = os.environ.get(variable)
    return pathlib.Path(configured) if configured else fallback


def export_preferences_path(home: pathlib.Path) -> pathlib.Path:
    """Return the TUI export-preference file path.

    Parameters
    ----------
    home : pathlib.Path
        Current user's home directory used for the XDG fallback.

    Returns
    -------
    pathlib.Path
        ``agentgrep/tui-export.json`` below the active configuration root.
    """
    root = _xdg_path("XDG_CONFIG_HOME", home / ".config")
    return root / "agentgrep" / "tui-export.json"


def default_export_directory(home: pathlib.Path) -> pathlib.Path:
    """Return the default private TUI export directory.

    Parameters
    ----------
    home : pathlib.Path
        Current user's home directory used for the XDG fallback.

    Returns
    -------
    pathlib.Path
        ``agentgrep/exports`` below the active data root.
    """
    root = _xdg_path("XDG_DATA_HOME", home / ".local" / "share")
    return root / "agentgrep" / "exports"


def resolve_export_directory(value: str, home: pathlib.Path) -> pathlib.Path:
    """Resolve only the current-user tilde spelling in ``value``.

    Parameters
    ----------
    value : str
        Literal directory value from the export dialog.
    home : pathlib.Path
        Current user's home directory.

    Returns
    -------
    pathlib.Path
        The supplied path, with bare ``~`` or ``~/`` expanded against ``home``.

    Raises
    ------
    ExportPreferencesError
        If an other-user tilde spelling is supplied.
    """
    if value == "~" or value == f"~{os.sep}":
        return home
    current_home_prefix = f"~{os.sep}"
    if value.startswith(current_home_prefix):
        return home / value[len(current_home_prefix) :]
    if value.startswith("~"):
        raise ExportPreferencesError(_FILENAME_ERROR)
    return pathlib.Path(value)


def _slug(value: str) -> str:
    """Return a bounded NFKC/casefolded Unicode-alphanumeric slug."""
    normalized = unicodedata.normalize("NFKC", value[:MAX_TEMPLATE_CHARS]).casefold()
    pieces: list[str] = []
    pending_separator = False
    for character in normalized:
        if character.isalnum():
            if pending_separator and pieces:
                pieces.append("-")
            pieces.append(character)
            pending_separator = False
        else:
            pending_separator = True
    return "".join(pieces)


def _validate_filename(filename: str) -> None:
    """Reject an unsafe or unreviewable compiled filename."""
    if "{" in filename or "}" in filename:
        raise ExportPreferencesError(_FILENAME_ERROR)
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in filename):
        raise ExportPreferencesError(_FILENAME_ERROR)
    if "/" in filename or "\\" in filename:
        raise ExportPreferencesError(_FILENAME_ERROR)
    if filename in {".", ".."} or filename.endswith((" ", ".")):
        raise ExportPreferencesError(_FILENAME_ERROR)
    if not filename.endswith(".md") or not filename.removesuffix(".md"):
        raise ExportPreferencesError(_FILENAME_ERROR)
    if ntpath.isreserved(filename):
        raise ExportPreferencesError(_FILENAME_ERROR)
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError:
        raise ExportPreferencesError(_FILENAME_ERROR) from None
    if len(encoded) > MAX_FILENAME_BYTES:
        raise ExportPreferencesError(_FILENAME_ERROR)


def render_export_filename(
    template: str,
    title: str,
    fallback_title: str,
    timestamp: datetime.datetime,
) -> str:
    """Compile one reviewed Markdown filename from the tiny token grammar.

    Parameters
    ----------
    template : str
        Template containing only the ``date``, ``time``, and ``title`` tokens.
    title : str
        Record title used to build the filename slug.
    fallback_title : str
        Non-sensitive fallback used when ``title`` produces an empty slug.
    timestamp : datetime.datetime
        Frozen local timestamp captured when the dialog opened.

    Returns
    -------
    str
        Validated Markdown basename.

    Raises
    ------
    ExportPreferencesError
        If the template or compiled basename is unsafe or outside its bounds.
    """
    if not isinstance(template, str) or len(template) > MAX_TEMPLATE_CHARS:
        raise ExportPreferencesError(_FILENAME_ERROR)
    slug = _slug(title) or _slug(fallback_title)
    if not slug:
        raise ExportPreferencesError(_FILENAME_ERROR)
    filename = template
    substitutions = {
        "date": timestamp.strftime("%Y-%m-%d"),
        "time": timestamp.strftime("%H-%M-%S"),
        "title": slug,
    }
    for token, value in substitutions.items():
        filename = filename.replace(f"{{{token}}}", value)
    _validate_filename(filename)
    return filename


def _default_preferences(home: pathlib.Path) -> ExportPreferences:
    """Return first-run preferences for ``home``."""
    return ExportPreferences(directory=str(default_export_directory(home)))


def _unique_object(pairs: list[tuple[str, t.Any]]) -> dict[str, t.Any]:
    """Build one JSON object while rejecting duplicate keys."""
    result: dict[str, t.Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _parse_preferences(payload: bytes) -> ExportPreferences:
    """Parse an exact-version preference payload."""
    data = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or frozenset(data) != _SCHEMA_KEYS:
        raise ValueError
    version = data["version"]
    directory = data["directory"]
    filename_template = data["filename_template"]
    if type(version) is not int or version != 1:
        raise ValueError
    if not isinstance(directory, str) or not isinstance(filename_template, str):
        raise TypeError
    render_export_filename(
        filename_template,
        title="Title",
        fallback_title="record",
        timestamp=datetime.datetime(2000, 1, 1),
    )
    return ExportPreferences(directory=directory, filename_template=filename_template)


def _read_preferences(path: pathlib.Path) -> bytes:
    """Read one payload without crossing the preference byte limit."""
    with path.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_PREFERENCES_BYTES:
            raise ValueError
        return handle.read(MAX_PREFERENCES_BYTES)


def load_export_preferences(home: pathlib.Path) -> ExportPreferencesLoad:
    """Load a bounded exact-schema preference file.

    Parameters
    ----------
    home : pathlib.Path
        Current user's home directory used by path defaults.

    Returns
    -------
    ExportPreferencesLoad
        Stored preferences, or defaults with a path-free warning on invalid I/O
        or content. A missing file returns defaults without a warning.
    """
    defaults = _default_preferences(home)
    path = export_preferences_path(home)
    try:
        preferences = _parse_preferences(_read_preferences(path))
    except FileNotFoundError:
        return ExportPreferencesLoad(defaults)
    except ExportPreferencesError, OSError, UnicodeError, ValueError, TypeError:
        return ExportPreferencesLoad(defaults, _PREFERENCES_WARNING)
    return ExportPreferencesLoad(preferences)


def _write_all(file_descriptor: int, payload: bytes) -> None:
    """Write every byte, retrying positive short writes."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(file_descriptor, view[offset:])
        if written <= 0:
            raise OSError
        offset += written


def _serialize_preferences(preferences: ExportPreferences) -> bytes:
    """Validate and serialize one exact-schema preference payload."""
    if not isinstance(preferences.directory, str) or not isinstance(
        preferences.filename_template,
        str,
    ):
        raise ExportPreferencesError(_PREFERENCES_SAVE_ERROR)
    render_export_filename(
        preferences.filename_template,
        title="Title",
        fallback_title="record",
        timestamp=datetime.datetime(2000, 1, 1),
    )
    payload = json.dumps(
        {
            "version": 1,
            "directory": preferences.directory,
            "filename_template": preferences.filename_template,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_PREFERENCES_BYTES:
        raise ExportPreferencesError(_PREFERENCES_SAVE_ERROR)
    return payload


def save_export_preferences(home: pathlib.Path, preferences: ExportPreferences) -> None:
    """Atomically save private TUI export preferences.

    Parameters
    ----------
    home : pathlib.Path
        Current user's home directory used by the config-path fallback.
    preferences : ExportPreferences
        Reviewed directory and filename template to persist.

    Raises
    ------
    ExportPreferencesError
        If validation, creation, writing, synchronization, or installation fails.
    """
    try:
        payload = _serialize_preferences(preferences)
        destination = export_preferences_path(home)
        config_directory = destination.parent
        config_directory.mkdir(mode=0o700, exist_ok=True)
        config_directory.chmod(0o700)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=config_directory,
            prefix=".tui-export-",
            suffix=".tmp",
        )
    except ExportPreferencesError, OSError, UnicodeError, ValueError, TypeError:
        raise ExportPreferencesError(_PREFERENCES_SAVE_ERROR) from None

    temporary = pathlib.Path(temporary_name)
    installed = False
    try:
        try:
            os.fchmod(file_descriptor, 0o600)
            _write_all(file_descriptor, payload)
            os.fsync(file_descriptor)
        finally:
            with contextlib.suppress(OSError):
                os.close(file_descriptor)
        os.replace(temporary, destination)  # noqa: PTH105 -- required atomic primitive
        installed = True
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(config_directory, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(directory_fd)
    except OSError:
        raise ExportPreferencesError(_PREFERENCES_SAVE_ERROR) from None
    finally:
        if not installed:
            with contextlib.suppress(OSError):
                temporary.unlink()
