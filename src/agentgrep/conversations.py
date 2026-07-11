"""Frontend-neutral grouping of observed conversation topology."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import struct
import typing as t

from agentgrep.identity import RecordIdentity, record_identity
from agentgrep.records import SearchRecord

__all__ = (
    "ConversationFidelity",
    "ConversationUnit",
    "group_conversation_units",
)

type ConversationFidelity = t.Literal["native_tree", "source_order", "unordered"]


@dataclasses.dataclass(frozen=True, slots=True)
class ConversationUnit:
    """Observed records sharing one canonical thread identity."""

    thread_id: str
    records: tuple[SearchRecord, ...]
    linear_records: tuple[SearchRecord, ...] | None
    fidelity: ConversationFidelity


@dataclasses.dataclass(frozen=True, slots=True)
class _PreparedRecord:
    """One record with its identity and prepared topology coordinates."""

    record: SearchRecord
    identity: RecordIdentity
    ordinal_domain: tuple[str, str]
    ordinal: int | None
    raw_ordinal_key: _RawTopologyKey
    native_id: str | None
    raw_native_id_key: _RawTopologyKey
    parent_native_id: str | None
    raw_parent_native_id_key: _RawTopologyKey


class _RawTopologyKey(t.NamedTuple):
    """Stable key for one raw topology scalar."""

    type_rank: int
    numeric_value: int
    text_value: str


class _InventoryKey(t.NamedTuple):
    """Deterministic, non-chronological ordering key for one physical view."""

    ordinal_missing: bool
    ordinal: int
    raw_ordinal: _RawTopologyKey
    native_id_missing: bool
    native_id: str
    raw_native_id: _RawTopologyKey
    parent_native_id_missing: bool
    parent_native_id: str
    raw_parent_native_id: _RawTopologyKey
    record_id_missing: bool
    record_id: str
    content_id: str
    agent: str
    identity_namespace: str
    session_id: str
    conversation_id: str
    store: str
    adapter_id: str
    path: str
    kind: str
    role: str
    title: str
    model: str


def _raw_topology_sort_key(value: object) -> _RawTopologyKey:
    """Return a stable key for a supported raw topology scalar.

    Parameters
    ----------
    value
        Raw source coordinate before validation.

    Returns
    -------
    _RawTopologyKey
        Fixed-field, type-ranked scalar key. Unsupported objects intentionally
        share one fallback key rather than relying on unstable representations.
    """
    if value is None:
        return _RawTopologyKey(0, 0, "")
    if type(value) is str:
        return _RawTopologyKey(1, 0, value)
    if type(value) is bool:
        return _RawTopologyKey(2, 1 if value else 0, "")
    if type(value) is int:
        return _RawTopologyKey(3, value, "")
    if type(value) is float:
        float_bits = int.from_bytes(struct.pack("!d", value), "big")
        return _RawTopologyKey(4, float_bits, "")
    return _RawTopologyKey(5, 0, "")


def _validated_ordinal(value: object) -> int | None:
    """Return a non-negative, non-boolean ordinal when valid.

    Parameters
    ----------
    value
        Candidate normalized source ordinal.

    Returns
    -------
    int | None
        Validated ordinal, or ``None`` when unavailable or malformed.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _validated_native_coordinate(value: object) -> str | None:
    """Return a non-empty native coordinate when valid.

    Parameters
    ----------
    value
        Candidate native occurrence or parent coordinate.

    Returns
    -------
    str | None
        Validated coordinate, or ``None`` when unavailable or malformed.
    """
    return value if isinstance(value, str) and bool(value) else None


def _prepare_record(record: SearchRecord, identity: RecordIdentity) -> _PreparedRecord:
    """Pair one record with its prepared identity and validated topology.

    Parameters
    ----------
    record
        Normalized record to prepare.
    identity
        Identity bundle computed once by the caller.

    Returns
    -------
    _PreparedRecord
        Cached values used by grouping, validation, and sorting.
    """
    position = record.position
    raw_ordinal = position.ordinal if position is not None else None
    raw_native_id = position.native_id if position is not None else None
    raw_parent_native_id = position.parent_native_id if position is not None else None
    return _PreparedRecord(
        record=record,
        identity=identity,
        ordinal_domain=(record.store, record.adapter_id),
        ordinal=_validated_ordinal(raw_ordinal),
        raw_ordinal_key=_raw_topology_sort_key(raw_ordinal),
        native_id=_validated_native_coordinate(raw_native_id),
        raw_native_id_key=_raw_topology_sort_key(raw_native_id),
        parent_native_id=_validated_native_coordinate(raw_parent_native_id),
        raw_parent_native_id_key=_raw_topology_sort_key(raw_parent_native_id),
    )


def _inventory_sort_key(prepared: _PreparedRecord) -> _InventoryKey:
    """Return an input-independent inventory key without implying chronology.

    Parameters
    ----------
    prepared
        Record and cached identity/topology values.

    Returns
    -------
    _InventoryKey
        Conversation-relevant identity and physical-view tie-breakers.
    """
    record = prepared.record
    record_id = prepared.identity.record_id
    return _InventoryKey(
        ordinal_missing=prepared.ordinal is None,
        ordinal=prepared.ordinal or 0,
        raw_ordinal=prepared.raw_ordinal_key,
        native_id_missing=prepared.native_id is None,
        native_id=prepared.native_id or "",
        raw_native_id=prepared.raw_native_id_key,
        parent_native_id_missing=prepared.parent_native_id is None,
        parent_native_id=prepared.parent_native_id or "",
        raw_parent_native_id=prepared.raw_parent_native_id_key,
        record_id_missing=record_id is None,
        record_id=record_id or "",
        content_id=prepared.identity.content_id,
        agent=record.agent,
        identity_namespace=record.identity_namespace or "",
        session_id=record.session_id or "",
        conversation_id=record.conversation_id or "",
        store=record.store,
        adapter_id=record.adapter_id,
        path=record.path.as_posix(),
        kind=record.kind,
        role=record.role.casefold() if record.role else "",
        title=record.title or "",
        model=record.model or "",
    )


def _can_linearize(records: list[_PreparedRecord]) -> bool:
    """Return whether every observed view has unambiguous ordinal order.

    Parameters
    ----------
    records
        Prepared records from one canonical thread.

    Returns
    -------
    bool
        Whether ordinals and logical/native occurrence coordinates are unique.
    """
    ordinals = [prepared.ordinal for prepared in records]
    if any(ordinal is None for ordinal in ordinals):
        return False
    if len(set(ordinals)) != len(ordinals):
        return False
    if len({prepared.ordinal_domain for prepared in records}) != 1:
        return False

    record_ids = [prepared.identity.record_id for prepared in records]
    if any(record_id is None for record_id in record_ids):
        return False
    if len(set(record_ids)) != len(record_ids):
        return False

    native_ids = [prepared.native_id for prepared in records if prepared.native_id is not None]
    return len(set(native_ids)) == len(native_ids)


def _build_conversation_unit(
    thread_id: str,
    members: list[_PreparedRecord],
) -> ConversationUnit:
    """Build one canonical unit from the observed members of a thread.

    Parameters
    ----------
    thread_id
        Full canonical thread identifier.
    members
        Prepared physical views observed for the thread.

    Returns
    -------
    ConversationUnit
        Lossless inventory with optional proven ordinal order.
    """
    inventory = tuple(prepared.record for prepared in sorted(members, key=_inventory_sort_key))
    if _can_linearize(members):
        linear_records: tuple[SearchRecord, ...] | None = inventory
    else:
        linear_records = None

    has_native_parent = any(prepared.parent_native_id is not None for prepared in members)
    if has_native_parent:
        fidelity: ConversationFidelity = "native_tree"
    elif linear_records is not None:
        fidelity = "source_order"
    else:
        fidelity = "unordered"

    return ConversationUnit(
        thread_id=thread_id,
        records=inventory,
        linear_records=linear_records,
        fidelity=fidelity,
    )


def group_conversation_units(
    records: cabc.Iterable[SearchRecord],
) -> tuple[ConversationUnit, ...]:
    """Group an observed record subset by canonical thread identity.

    Parameters
    ----------
    records
        Normalized records to consume exactly once.

    Returns
    -------
    tuple[ConversationUnit, ...]
        Canonically ordered units for records with defensible thread IDs.

    Notes
    -----
    This function preserves only observed topology. It does not assert source
    completeness, choose a revision or branch, or invent timestamp order.
    """
    groups: dict[str, list[_PreparedRecord]] = {}
    for record in records:
        identity = record_identity(record)
        if identity.thread_id is None:
            continue
        prepared = _prepare_record(record, identity)
        groups.setdefault(identity.thread_id, []).append(prepared)

    return tuple(
        _build_conversation_unit(thread_id, groups[thread_id]) for thread_id in sorted(groups)
    )
