"""Frontend-neutral grouping of observed conversation topology."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import struct
import typing as t

from agentgrep.identity import RecordIdentity, record_identity, record_thread_id
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


class _OptionalTextKey(t.NamedTuple):
    """Presence-aware key for one normalized optional string."""

    missing: bool
    value: str


class _CanonicalValueKey(t.NamedTuple):
    """Fixed-shape, type-ranked key for JSON-compatible metadata."""

    type_rank: int
    numeric_value: int
    text_value: str
    sequence_items: tuple[_CanonicalValueKey, ...]
    mapping_items: tuple[tuple[_CanonicalValueKey, _CanonicalValueKey], ...]


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
    position_missing: bool
    position_quality: _CanonicalValueKey
    record_id_missing: bool
    record_id: str
    content_id: str
    text: str
    agent: str
    identity_namespace: _OptionalTextKey
    session_id: _OptionalTextKey
    conversation_id: _OptionalTextKey
    store: str
    adapter_id: str
    path: str
    kind: str
    role: _OptionalTextKey
    title: _OptionalTextKey
    timestamp: _OptionalTextKey
    model: _OptionalTextKey
    origin_missing: bool
    origin_cwd: _OptionalTextKey
    origin_repo: _OptionalTextKey
    origin_worktree: _OptionalTextKey
    origin_branch: _OptionalTextKey
    origin_remote: _OptionalTextKey
    origin_cwd_hash: _OptionalTextKey
    metadata: _CanonicalValueKey


def _optional_text_sort_key(value: str | None) -> _OptionalTextKey:
    """Return a presence-aware key for one optional normalized string."""
    return _OptionalTextKey(missing=value is None, value=value or "")


def _unsupported_value_sort_key(value: object) -> _CanonicalValueKey:
    """Return a stable type-level fallback without inspecting object repr."""
    value_type = type(value)
    type_name = f"{value_type.__module__}.{value_type.__qualname__}"
    return _CanonicalValueKey(7, 0, type_name, (), ())


def _canonical_value_sort_key(
    value: object,
    *,
    active_containers: set[int] | None = None,
) -> _CanonicalValueKey:
    """Return a total type-ranked key for JSON-compatible metadata.

    Parameters
    ----------
    value
        Candidate JSON-compatible value.
    active_containers
        Container identities on the current recursion path. Cycles use the
        same type-level fallback as other unsupported values.

    Returns
    -------
    _CanonicalValueKey
        Stable key that never depends on object representation or address.
    """
    if value is None:
        return _CanonicalValueKey(0, 0, "", (), ())
    if type(value) is bool:
        return _CanonicalValueKey(1, 1 if value else 0, "", (), ())
    if type(value) is int:
        return _CanonicalValueKey(2, value, "", (), ())
    if type(value) is float:
        float_bits = int.from_bytes(struct.pack("!d", value), "big")
        return _CanonicalValueKey(3, float_bits, "", (), ())
    if type(value) is str:
        return _CanonicalValueKey(4, 0, value, (), ())
    if type(value) not in {list, dict}:
        return _unsupported_value_sort_key(value)

    active = set() if active_containers is None else active_containers
    marker = id(value)
    if marker in active:
        return _unsupported_value_sort_key(value)
    active.add(marker)
    try:
        if isinstance(value, list):
            items = tuple(
                _canonical_value_sort_key(item, active_containers=active) for item in value
            )
            return _CanonicalValueKey(5, 0, "", items, ())
        mapping_items = tuple(
            sorted(
                (
                    _canonical_value_sort_key(key, active_containers=active),
                    _canonical_value_sort_key(item, active_containers=active),
                )
                for key, item in t.cast("dict[object, object]", value).items()
            ),
        )
        return _CanonicalValueKey(6, 0, "", (), mapping_items)
    finally:
        active.remove(marker)


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
    position = record.position
    origin = record.origin
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
        position_missing=position is None,
        position_quality=_canonical_value_sort_key(
            position.quality if position is not None else None,
        ),
        record_id_missing=record_id is None,
        record_id=record_id or "",
        content_id=prepared.identity.content_id,
        text=record.text,
        agent=record.agent,
        identity_namespace=_optional_text_sort_key(record.identity_namespace),
        session_id=_optional_text_sort_key(record.session_id),
        conversation_id=_optional_text_sort_key(record.conversation_id),
        store=record.store,
        adapter_id=record.adapter_id,
        path=record.path.as_posix(),
        kind=record.kind,
        role=_optional_text_sort_key(record.role),
        title=_optional_text_sort_key(record.title),
        timestamp=_optional_text_sort_key(record.timestamp),
        model=_optional_text_sort_key(record.model),
        origin_missing=origin is None,
        origin_cwd=_optional_text_sort_key(origin.cwd if origin is not None else None),
        origin_repo=_optional_text_sort_key(origin.repo if origin is not None else None),
        origin_worktree=_optional_text_sort_key(
            origin.worktree if origin is not None else None,
        ),
        origin_branch=_optional_text_sort_key(origin.branch if origin is not None else None),
        origin_remote=_optional_text_sort_key(origin.remote if origin is not None else None),
        origin_cwd_hash=_optional_text_sort_key(
            origin.cwd_hash if origin is not None else None,
        ),
        metadata=_canonical_value_sort_key(record.metadata),
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
        thread_id = record_thread_id(record)
        if thread_id is None:
            continue
        identity = record_identity(record, prepared_thread_id=thread_id)
        prepared = _prepare_record(record, identity)
        groups.setdefault(thread_id, []).append(prepared)

    return tuple(
        _build_conversation_unit(thread_id, groups[thread_id]) for thread_id in sorted(groups)
    )
