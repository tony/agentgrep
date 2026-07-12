"""Deterministic rendering and private file output for normalized records."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import dataclasses
import json
import os
import pathlib
import re
import secrets
import stat
import typing as t

from agentgrep.conversations import ConversationFidelity, _group_prepared_conversation_units
from agentgrep.identity import RecordIdentity, record_identity
from agentgrep.records import SCHEMA_VERSION, SearchRecord

__all__ = (
    "ExportArtifact",
    "ExportEncodingError",
    "ExportError",
    "ExportExistsError",
    "ExportFormat",
    "ExportFormatError",
    "ExportSafetyError",
    "ExportSelection",
    "ExportSelectionError",
    "ExportWriteError",
    "render_export",
    "write_export",
    "write_private_export",
)

type ExportFormat = t.Literal["ndjson", "markdown"]
type ExportSelection = t.Literal["records", "thread"]
type _ProtectedPaths = cabc.Iterable[str | os.PathLike[str]]


class ExportError(Exception):
    """Base class for path-free export failures."""


class ExportFormatError(ExportError):
    """The requested export format is unsupported."""


class ExportSelectionError(ExportError):
    """The selected records cannot form the requested export unit."""


class ExportEncodingError(ExportError):
    """The selected values cannot be represented by the export format."""


class ExportExistsError(ExportError):
    """The destination already exists and overwrite was not requested."""


class ExportSafetyError(ExportError):
    """The destination violates an export path-safety invariant."""


class ExportWriteError(ExportError):
    """The artifact could not be durably written."""


@dataclasses.dataclass(frozen=True, slots=True)
class ExportArtifact:
    """One frontend-neutral rendered export."""

    format: ExportFormat
    selection: ExportSelection
    record_count: int
    thread_id: str | None
    fidelity: ConversationFidelity | None
    text: str
    byte_count: int


class _ExportRecordPayload(t.TypedDict):
    """Allowlisted portable fields for one normalized record."""

    schema_version: str
    agent: str
    store: str
    kind: str
    role: str | None
    timestamp: str | None
    model: str | None
    content_id: str
    record_id: str | None
    record_id_stability: str | None
    thread_id: str | None
    text: t.NotRequired[str]


@dataclasses.dataclass(frozen=True, slots=True)
class _PreparedRecord:
    """One record paired with its cached identity and portable payload."""

    record: SearchRecord
    identity: RecordIdentity
    payload: _ExportRecordPayload


def _prepare_records(
    records: cabc.Iterable[SearchRecord],
    *,
    include_bodies: bool,
) -> tuple[_PreparedRecord, ...]:
    """Prepare each selected record identity and payload once."""
    prepared: list[_PreparedRecord] = []
    for record in records:
        identity = record_identity(record)
        payload: _ExportRecordPayload = {
            "schema_version": SCHEMA_VERSION,
            "agent": record.agent,
            "store": record.store,
            "kind": record.kind,
            "role": record.role,
            "timestamp": record.timestamp,
            "model": record.model,
            "content_id": identity.content_id,
            "record_id": identity.record_id,
            "record_id_stability": identity.record_id_stability,
            "thread_id": identity.thread_id,
        }
        if include_bodies:
            payload["text"] = record.text
        prepared.append(_PreparedRecord(record, identity, payload))
    return tuple(prepared)


def _canonical_json(payload: _ExportRecordPayload) -> str:
    """Return stable ASCII JSON, including lone-surrogate escapes."""
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _record_sort_key(prepared: _PreparedRecord) -> tuple[object, ...]:
    """Return the export-owned total ordering key."""
    identity = prepared.identity
    timestamp = prepared.record.timestamp
    return (
        identity.thread_id is None,
        identity.thread_id or "",
        timestamp is None,
        timestamp or "",
        identity.record_id is None,
        identity.record_id or identity.content_id,
        identity.content_id,
        _canonical_json(prepared.payload),
    )


def _render_ndjson(records: tuple[_PreparedRecord, ...]) -> str:
    """Render one canonical object per line."""
    return "".join(f"{_canonical_json(record.payload)}\n" for record in records)


def _require_utf8(value: str) -> None:
    """Reject a non-UTF-8 Unicode scalar with a path-free error."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        message = "markdown export contains text that is not valid UTF-8"
        raise ExportEncodingError(message) from None


def _markdown_scalar(value: str | None) -> str:
    """Render one nullable scalar without admitting Markdown structure."""
    if value is None:
        return "null"
    _require_utf8(value)
    encoded = json.dumps(value, ensure_ascii=False)[1:-1]
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", encoded)


def _body_fence(body: str) -> str:
    """Return a backtick fence longer than every run in ``body``."""
    longest = max((len(match.group()) for match in re.finditer(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def _render_markdown(
    records: tuple[_PreparedRecord, ...],
    *,
    selection: ExportSelection,
    thread_id: str | None,
    fidelity: ConversationFidelity | None,
) -> str:
    """Render allowlisted human-readable Markdown."""
    noun = "observed thread" if selection == "thread" else "record"
    lines = [
        f"# agentgrep {noun} export",
        "",
        f"- Schema version: {SCHEMA_VERSION}",
        f"- Selection: {selection}",
    ]
    lines.append(f"- Record count: {len(records)}")
    if thread_id is not None:
        lines.append(f"- Thread ID: {_markdown_scalar(thread_id)}")
    if fidelity is not None:
        lines.append(f"- Fidelity: {fidelity}")

    for index, prepared in enumerate(records, start=1):
        payload = prepared.payload
        lines.extend(
            (
                "",
                f"## Record {index}",
                "",
                f"- Agent: {_markdown_scalar(payload['agent'])}",
                f"- Store: {_markdown_scalar(payload['store'])}",
                f"- Kind: {_markdown_scalar(payload['kind'])}",
                f"- Role: {_markdown_scalar(payload['role'])}",
                f"- Timestamp: {_markdown_scalar(payload['timestamp'])}",
                f"- Model: {_markdown_scalar(payload['model'])}",
                f"- Content ID: {_markdown_scalar(payload['content_id'])}",
                f"- Record ID: {_markdown_scalar(payload['record_id'])}",
                f"- Record ID stability: {_markdown_scalar(payload['record_id_stability'])}",
                f"- Thread ID: {_markdown_scalar(payload['thread_id'])}",
            ),
        )
        if "text" in payload:
            body = payload["text"]
            _require_utf8(body)
            fence = _body_fence(body)
            lines.extend(("", "### Body", "", f"{fence}text", body, fence))
    text = "\n".join(lines) + "\n"
    _require_utf8(text)
    return text


def render_export(
    records: cabc.Iterable[SearchRecord],
    *,
    format: ExportFormat,  # noqa: A002 - required public keyword.
    include_bodies: bool,
    selection: ExportSelection = "records",
) -> ExportArtifact:
    """Render records into one deterministic portable artifact.

    Parameters
    ----------
    records
        Normalized records to consume once.
    format
        ``ndjson`` or ``markdown``.
    include_bodies
        Whether to include exact record text.
    selection
        Flat records or one observed canonical thread.

    Returns
    -------
    ExportArtifact
        Immutable rendered text and byte metadata.
    """
    if format not in {"ndjson", "markdown"}:
        message = "unsupported export format"
        raise ExportFormatError(message)
    if selection not in {"records", "thread"}:
        message = "unsupported export selection"
        raise ExportSelectionError(message)

    selected = tuple(records)
    prepared = _prepare_records(selected, include_bodies=include_bodies)
    thread_id: str | None = None
    fidelity: ConversationFidelity | None = None
    if selection == "thread":
        units = _group_prepared_conversation_units(
            (item.record, item.identity) for item in prepared
        )
        if len(units) != 1 or len(units[0].records) != len(selected):
            message = "thread export requires exactly one observed thread"
            raise ExportSelectionError(message)
        thread_id = units[0].thread_id
        fidelity = units[0].fidelity

    prepared = tuple(sorted(prepared, key=_record_sort_key))
    text = (
        _render_ndjson(prepared)
        if format == "ndjson"
        else _render_markdown(
            prepared,
            selection=selection,
            thread_id=thread_id,
            fidelity=fidelity,
        )
    )
    return ExportArtifact(
        format,
        selection,
        len(prepared),
        thread_id,
        fidelity,
        text,
        len(text.encode("utf-8")),
    )


def _validated_artifact_bytes(artifact: ExportArtifact) -> bytes:
    """Validate the public artifact contract and return its exact bytes."""
    if artifact.format not in ("ndjson", "markdown"):
        message = "unsupported export format"
        raise ExportFormatError(message)
    if artifact.selection not in ("records", "thread"):
        message = "unsupported export selection"
        raise ExportSelectionError(message)
    try:
        payload = artifact.text.encode("utf-8")
    except UnicodeEncodeError:
        message = "export artifact is not valid UTF-8"
        raise ExportWriteError(message) from None
    if len(payload) != artifact.byte_count:
        message = "export artifact byte count is inconsistent"
        raise ExportWriteError(message)
    return payload


def _absolute(path: pathlib.Path) -> pathlib.Path:
    """Return a normalized absolute path without resolving symlinks."""
    # ``Path.resolve()`` would follow a path before the safety walk.
    return pathlib.Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100


def _directory_flags() -> int:
    """Return directory flags that reject a final symlink."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory:
        message = "export destination is unsafe on this platform"
        raise ExportSafetyError(message)
    return os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)


def _close_quietly(fd: int) -> None:
    """Close a cleanup descriptor without masking the primary result."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _unlink_quietly(directory_fd: int, name: str) -> None:
    """Remove temporary cleanup debris when it still exists."""
    with contextlib.suppress(OSError):
        os.unlink(name, dir_fd=directory_fd)


def _require_directory(component_stat: os.stat_result) -> None:
    """Reject a symlink or non-directory path component."""
    if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
        message = "export destination is unsafe"
        raise ExportSafetyError(message)


def _open_directory(path: pathlib.Path, *, create_private: bool) -> int:
    """Open or create a directory tree without traversing symlinks."""
    absolute = _absolute(path)
    if create_private and absolute == pathlib.Path(os.sep):
        message = "private export directory is unsafe"
        raise ExportSafetyError(message)
    flags = _directory_flags()
    try:
        current_fd = os.open(os.sep, flags)
    except OSError:
        message = "export destination could not be written"
        raise ExportWriteError(message) from None
    try:
        for component in absolute.parts[1:]:
            try:
                component_stat = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create_private:
                    message = "export destination could not be written"
                    raise ExportWriteError(message) from None
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    component_stat = os.stat(
                        component,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    message = "private export directory could not be created"
                    raise ExportWriteError(message) from None
            except OSError:
                message = "export destination is unsafe"
                raise ExportSafetyError(message) from None
            _require_directory(component_stat)
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError:
                message = "export destination is unsafe"
                raise ExportSafetyError(message) from None
            _close_quietly(current_fd)
            current_fd = next_fd
        if create_private:
            try:
                os.fchmod(current_fd, 0o700)
            except OSError:
                message = "private export directory could not be created"
                raise ExportWriteError(message) from None
    except BaseException:
        _close_quietly(current_fd)
        raise
    return current_fd


def _destination_stat(directory_fd: int, name: str) -> os.stat_result | None:
    """Inspect a final component without following it."""
    try:
        result = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        message = "export destination could not be inspected"
        raise ExportWriteError(message) from None
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISREG(result.st_mode):
        message = "export destination is unsafe"
        raise ExportSafetyError(message)
    return result


def _reject_protected_alias(
    destination: pathlib.Path,
    destination_stat: os.stat_result | None,
    protected_paths: _ProtectedPaths,
) -> None:
    """Reject lexical, resolved, and inode aliases of source paths."""
    lexical = os.path.normcase(os.fspath(_absolute(destination)))
    resolved = os.path.normcase(os.path.realpath(lexical))
    for value in protected_paths:
        protected = pathlib.Path(value)
        protected_lexical = os.path.normcase(os.fspath(_absolute(protected)))
        if lexical == protected_lexical or resolved == os.path.normcase(
            os.path.realpath(protected_lexical),
        ):
            message = "export destination aliases a protected source"
            raise ExportSafetyError(message)
        if destination_stat is None:
            continue
        try:
            protected_stat = protected.stat()
        except FileNotFoundError:
            continue
        except OSError:
            message = "export destination aliases a protected source"
            raise ExportSafetyError(message) from None
        if (destination_stat.st_dev, destination_stat.st_ino) == (
            protected_stat.st_dev,
            protected_stat.st_ino,
        ):
            message = "export destination aliases a protected source"
            raise ExportSafetyError(message)


def _new_temporary(directory_fd: int) -> tuple[str, int]:
    """Create a private same-directory temporary file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(128):
        name = f".agentgrep-export-{secrets.token_hex(12)}.tmp"
        try:
            file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        except OSError:
            message = "export destination could not be written"
            raise ExportWriteError(message) from None
        try:
            os.fchmod(file_fd, 0o600)
        except OSError:
            _close_quietly(file_fd)
            _unlink_quietly(directory_fd, name)
            message = "export destination could not be written"
            raise ExportWriteError(message) from None
        return name, file_fd
    message = "export destination could not be written"
    raise ExportWriteError(message)


def _write_all(file_fd: int, payload: bytes) -> None:
    """Write every byte, retrying positive short writes."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(file_fd, view[offset:])
        if written <= 0:
            raise OSError
        offset += written


def _install_export(
    payload: bytes,
    directory_fd: int,
    name: str,
    *,
    force: bool,
    destination: pathlib.Path,
    protected_paths: _ProtectedPaths,
) -> None:
    """Install artifact bytes relative to one secured directory descriptor."""
    temporary: str | None = None
    try:
        existing = _destination_stat(directory_fd, name)
        _reject_protected_alias(destination, existing, protected_paths)
        if existing is not None and not force:
            message = "export destination already exists"
            raise ExportExistsError(message)

        temporary, file_fd = _new_temporary(directory_fd)
        try:
            _write_all(file_fd, payload)
            os.fsync(file_fd)
        finally:
            _close_quietly(file_fd)

        if force:
            current = _destination_stat(directory_fd, name)
            _reject_protected_alias(destination, current, protected_paths)
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        else:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                message = "export destination already exists"
                raise ExportExistsError(message) from None
            os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
    except ExportError:
        raise
    except OSError:
        message = "export destination could not be written"
        raise ExportWriteError(message) from None
    finally:
        if temporary is not None:
            _unlink_quietly(directory_fd, temporary)


def write_export(
    artifact: ExportArtifact,
    destination: str | os.PathLike[str],
    *,
    force: bool = False,
    protected_paths: _ProtectedPaths = (),
) -> pathlib.Path:
    """Atomically write an artifact without following destination links.

    Parameters
    ----------
    artifact
        Fully rendered portable artifact.
    destination
        Explicit destination file.
    force
        Whether to replace an existing regular file.
    protected_paths
        Source paths that the destination must not alias.

    Returns
    -------
    pathlib.Path
        The caller-supplied destination value.
    """
    payload = _validated_artifact_bytes(artifact)
    result = pathlib.Path(destination)
    absolute = _absolute(result)
    if not absolute.name:
        message = "export destination is unsafe"
        raise ExportSafetyError(message)

    protected = tuple(protected_paths)
    _reject_protected_alias(absolute, None, protected)
    directory_fd = _open_directory(absolute.parent, create_private=False)
    try:
        _install_export(
            payload,
            directory_fd,
            absolute.name,
            force=force,
            destination=absolute,
            protected_paths=protected,
        )
    finally:
        _close_quietly(directory_fd)
    return result


_CANONICAL_ID = re.compile(r"ag[ctr]1:[0-9a-v]{26}")


def _artifact_slug(artifact: ExportArtifact) -> str:
    """Return a slug sourced only from structural canonical IDs."""
    canonical_id = (
        artifact.thread_id
        if artifact.thread_id is not None and re.fullmatch(r"agt1:[0-9a-v]{26}", artifact.thread_id)
        else None
    )
    if canonical_id is None and artifact.format == "ndjson":
        for line in artifact.text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError, TypeError:
                continue
            if not isinstance(row, dict):
                continue
            for key in ("record_id", "content_id"):
                candidate = row.get(key)
                if isinstance(candidate, str) and _CANONICAL_ID.fullmatch(candidate):
                    canonical_id = candidate
                    break
            if canonical_id is not None:
                break
    elif canonical_id is None:
        metadata = artifact.text.partition("\n### Body\n")[0]
        for label, prefix in (("Record", "agr"), ("Content", "agc")):
            match = re.search(
                rf"^- {label} ID: (?P<id>{prefix}1:[0-9a-v]{{26}})$",
                metadata,
                flags=re.MULTILINE,
            )
            if match is not None:
                canonical_id = match.group("id")
                break
    return "empty" if canonical_id is None else canonical_id.replace(":", "-")


def _default_private_directory() -> pathlib.Path:
    """Return the user-private default export directory."""
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return pathlib.Path(data_home) / "agentgrep" / "exports"
    return pathlib.Path.home() / ".local" / "share" / "agentgrep" / "exports"


def write_private_export(
    artifact: ExportArtifact,
    directory: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """Write an artifact under a private collision-free canonical name."""
    private_directory = (
        _default_private_directory() if directory is None else pathlib.Path(directory)
    )
    payload = _validated_artifact_bytes(artifact)
    extension = "ndjson" if artifact.format == "ndjson" else "md"
    basename = f"agentgrep-{_artifact_slug(artifact)}"
    directory_fd = _open_directory(private_directory, create_private=True)
    try:
        index = 1
        while True:
            suffix = "" if index == 1 else f"-{index}"
            destination = private_directory / f"{basename}{suffix}.{extension}"
            try:
                _install_export(
                    payload,
                    directory_fd,
                    destination.name,
                    force=False,
                    destination=_absolute(destination),
                    protected_paths=(),
                )
            except ExportExistsError:
                index += 1
                continue
            return destination
    finally:
        _close_quietly(directory_fd)
