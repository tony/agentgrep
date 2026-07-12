"""Shared opaque-ref resolution for MCP drilldown and export tools."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import hashlib
import pathlib
import typing as t

from agentgrep.mcp import refs
from agentgrep.mcp._library import SearchRecordLike, SourceHandleLike, agentgrep


class RecordRefResolverError(RuntimeError):
    """A resolver-wide failure that cannot be assigned to one ref."""


@dataclasses.dataclass(frozen=True, slots=True)
class PhysicalRecordSelection:
    """Privacy-safe physical source key and scan ordinal for one record."""

    source_key: str
    record_ordinal: int


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedRecordRef:
    """Records resolved from one opaque MCP ref."""

    ref: str
    kind: t.Literal["search", "find"] | None
    records: tuple[SearchRecordLike, ...] = ()
    physical_selection: PhysicalRecordSelection | None = None
    error_message: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _ParsedRequest:
    """One parsed ref paired with its request position."""

    index: int
    ref: str
    parsed: refs.ParsedRecordRef
    path_key: pathlib.Path


def _path_key(path: pathlib.Path) -> pathlib.Path | None:
    """Return a normalized lookup path or ``None`` on unsafe input."""
    try:
        return path.resolve()
    except OSError, RuntimeError, ValueError:
        return None


def _discover_sources(home: pathlib.Path) -> tuple[SourceHandleLike, ...]:
    """Discover every inspectable source behind a path-free error boundary."""
    try:
        backends = agentgrep.select_backends()
        return tuple(
            agentgrep.discover_sources(
                home,
                agentgrep.AGENT_CHOICES,
                backends,
                include_non_default=True,
                version_detail="none",
            )
        )
    except Exception:
        message = "source discovery failed"
        raise RecordRefResolverError(message) from None


def _source_index(
    sources: cabc.Iterable[SourceHandleLike],
) -> dict[tuple[str, pathlib.Path], SourceHandleLike]:
    """Index discovered sources by the same adapter/path pair encoded by refs."""
    indexed: dict[tuple[str, pathlib.Path], SourceHandleLike] = {}
    for source in sources:
        path = _path_key(pathlib.Path(source.path))
        if path is not None:
            indexed.setdefault((source.adapter_id, path), source)
    return indexed


def _physical_source_key(adapter_id: str, path: pathlib.Path) -> str:
    """Return a path-hiding key for one adapter and resolved source path."""
    raw = f"{adapter_id}\0{path}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _resolve_source_group(
    source: SourceHandleLike,
    requests: cabc.Sequence[_ParsedRequest],
    results: list[ResolvedRecordRef | None],
    *,
    source_key: str,
    sample_size: int,
) -> None:
    """Resolve every request for one source in a single record scan."""
    search_requests = [item for item in requests if item.parsed.kind == "search"]
    find_requests = [item for item in requests if item.parsed.kind == "find"]
    unresolved_search: dict[str, list[_ParsedRequest]] = {}
    for item in search_requests:
        unresolved_search.setdefault(item.parsed.fingerprint, []).append(item)
    unresolved_count = len(search_requests)
    find_records: list[SearchRecordLike] = []
    read_failed = False
    try:
        for record_ordinal, record in enumerate(agentgrep.iter_source_records(source)):
            if len(find_records) < sample_size:
                find_records.append(record)
            for fingerprint in refs.search_record_fingerprint_candidates(record):
                for item in unresolved_search.pop(fingerprint, ()):
                    results[item.index] = ResolvedRecordRef(
                        ref=item.ref,
                        kind="search",
                        records=(record,),
                        physical_selection=PhysicalRecordSelection(
                            source_key=source_key,
                            record_ordinal=record_ordinal,
                        ),
                    )
                    unresolved_count -= 1
            if unresolved_count == 0 and (not find_requests or len(find_records) >= sample_size):
                break
    except Exception:
        read_failed = True

    for items in unresolved_search.values():
        for item in items:
            results[item.index] = ResolvedRecordRef(
                ref=item.ref,
                kind="search",
                error_message="source could not be read" if read_failed else "record not found",
            )
    for item in find_requests:
        if read_failed:
            results[item.index] = ResolvedRecordRef(
                ref=item.ref,
                kind="find",
                error_message="source could not be read",
            )
        elif find_records:
            results[item.index] = ResolvedRecordRef(
                ref=item.ref,
                kind="find",
                records=tuple(find_records),
            )
        else:
            results[item.index] = ResolvedRecordRef(
                ref=item.ref,
                kind="find",
                error_message="record not found",
            )


def resolve_record_refs(
    ref_values: cabc.Sequence[str],
    *,
    sample_size: int = 1,
) -> tuple[ResolvedRecordRef, ...]:
    """Resolve opaque refs with one discovery and one scan per source.

    Parameters
    ----------
    ref_values
        Opaque ``agref1:`` values in caller order.
    sample_size
        Number of records returned for a find/source ref.

    Returns
    -------
    tuple[ResolvedRecordRef, ...]
        One path-free resolution in the same order as ``ref_values``.
    """
    if not 1 <= sample_size <= 20:
        message = "sample_size must be between 1 and 20"
        raise ValueError(message)

    home = pathlib.Path.home()
    results: list[ResolvedRecordRef | None] = [None] * len(ref_values)
    parsed_requests: list[_ParsedRequest] = []
    for index, ref in enumerate(ref_values):
        try:
            parsed = refs.parse_record_ref(ref, home=home)
        except refs.McpTokenError as exc:
            results[index] = ResolvedRecordRef(
                ref=ref,
                kind=None,
                error_message=f"invalid ref: {exc}",
            )
        else:
            path = _path_key(parsed.path)
            if path is None:
                results[index] = ResolvedRecordRef(
                    ref=ref,
                    kind=parsed.kind,
                    error_message="source not found",
                )
            else:
                parsed_requests.append(
                    _ParsedRequest(
                        index=index,
                        ref=ref,
                        parsed=parsed,
                        path_key=path,
                    ),
                )

    if parsed_requests:
        sources = _discover_sources(home)
        indexed_sources = _source_index(sources)
        grouped: dict[
            tuple[str, pathlib.Path],
            list[_ParsedRequest],
        ] = {}
        for item in parsed_requests:
            key = (item.parsed.adapter_id, item.path_key)
            source = indexed_sources.get(key)
            if source is None:
                results[item.index] = ResolvedRecordRef(
                    ref=item.ref,
                    kind=item.parsed.kind,
                    error_message="source not found",
                )
                continue
            grouped.setdefault(key, []).append(item)
        for key, group in grouped.items():
            _resolve_source_group(
                indexed_sources[key],
                group,
                results,
                source_key=_physical_source_key(*key),
                sample_size=sample_size,
            )

    return tuple(t.cast("ResolvedRecordRef", result) for result in results)
