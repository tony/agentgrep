"""Conversation topology grouping tests."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import importlib.util
import itertools
import json
import pathlib
import struct
import sys
import typing as t

import pytest

import agentgrep._engine.orchestration as orchestration
import agentgrep.conversations as conversations
import agentgrep.identity as identity
from agentgrep.conversations import ConversationUnit, group_conversation_units
from agentgrep.identity import RecordIdentity, record_identity, record_thread_id
from agentgrep.records import AgentName, RecordOrigin, RecordPosition, SearchRecord


class OrdinalCase(t.NamedTuple):
    """One incomplete or ambiguous ordinal sequence."""

    test_id: str
    ordinals: tuple[object, ...]


class RawTopologyCase(t.NamedTuple):
    """One raw topology field requiring an input-independent scalar order."""

    test_id: str
    field: t.Literal["ordinal", "native_id", "parent_native_id"]


ORDINAL_CASES: tuple[OrdinalCase, ...] = (
    OrdinalCase("missing", (0, None)),
    OrdinalCase("duplicate", (1, 1)),
    OrdinalCase("negative", (0, -1)),
    OrdinalCase("boolean", (0, True)),
    OrdinalCase("float", (0, 1.5)),
)

RAW_TOPOLOGY_CASES: tuple[RawTopologyCase, ...] = (
    RawTopologyCase("ordinal", "ordinal"),
    RawTopologyCase("native-id", "native_id"),
    RawTopologyCase("parent-native-id", "parent_native_id"),
)
RAW_TOPOLOGY_VALUES: tuple[object, ...] = (None, "", False, -1, 1.5)
PATHOLOGICAL_FLOAT_BITS = (
    "0000000000000000",  # Positive zero.
    "7ff0000000000000",  # Positive infinity.
    "7ff8000000000001",  # Positive quiet NaN with payload.
    "8000000000000000",  # Negative zero.
    "fff0000000000000",  # Negative infinity.
    "fff8000000000001",  # Negative quiet NaN with payload.
)


def _record(
    text: str,
    *,
    position: RecordPosition | None = None,
    agent: AgentName = "codex",
    session_id: str | None = "session-1",
    identity_namespace: str | None = "codex.session",
    store: str = "codex.sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path: str = "session.jsonl",
    timestamp: str | None = None,
) -> SearchRecord:
    """Build one normalized record for conversation tests."""
    return SearchRecord(
        kind="prompt",
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        text=text,
        role="user",
        timestamp=timestamp,
        session_id=session_id,
        conversation_id=session_id,
        identity_namespace=identity_namespace,
        position=position,
    )


def _member_projection(record: SearchRecord) -> tuple[object, ...]:
    """Return the conversation-relevant member inventory projection."""
    identity = record_identity(record)
    position = record.position
    return (
        identity.record_id,
        identity.content_id,
        position.ordinal if position is not None else None,
        position.native_id if position is not None else None,
        position.parent_native_id if position is not None else None,
        record.store,
        record.adapter_id,
        record.path.as_posix(),
    )


def _unit_projection(units: tuple[ConversationUnit, ...]) -> tuple[object, ...]:
    """Return the deterministic public conversation projection."""
    return tuple(
        (
            unit.thread_id,
            tuple(_member_projection(record) for record in unit.records),
            (
                None
                if unit.linear_records is None
                else tuple(_member_projection(record) for record in unit.linear_records)
            ),
            unit.fidelity,
        )
        for unit in units
    )


def _only_unit(records: cabc.Iterable[SearchRecord]) -> ConversationUnit:
    """Return the sole grouped unit after asserting its presence."""
    units = group_conversation_units(records)
    assert len(units) == 1
    return units[0]


def _inventory_orders(
    records: tuple[SearchRecord, ...],
    project: t.Callable[[SearchRecord], str],
) -> set[tuple[str, ...]]:
    """Return projected inventory orders under every input permutation."""
    return {
        tuple(project(record) for record in _only_unit(permutation).records)
        for permutation in itertools.permutations(records)
    }


def _raw_topology_position(
    field: t.Literal["ordinal", "native_id", "parent_native_id"],
    value: object,
) -> RecordPosition:
    """Build one deliberately malformed position for inventory-order tests."""
    if field == "ordinal":
        return RecordPosition(ordinal=t.cast("t.Any", value), quality="source_order")
    if field == "native_id":
        return RecordPosition(native_id=t.cast("t.Any", value), quality="native")
    return RecordPosition(
        native_id="message-1",
        parent_native_id=t.cast("t.Any", value),
        quality="native",
    )


def test_conversations_module_is_available() -> None:
    """The frontend-neutral conversation owner module is importable."""
    assert importlib.util.find_spec("agentgrep.conversations") is not None


def test_prepared_conversation_grouping_is_package_private() -> None:
    """The identity-reuse seam is internal rather than public API."""
    assert "group_prepared_conversation_units" not in conversations.__all__
    assert "_group_prepared_conversation_units" not in conversations.__all__
    assert not hasattr(conversations, "group_prepared_conversation_units")


def test_conversation_unit_has_exact_frozen_tuple_contract() -> None:
    """The conversation value has the reviewed shallow-frozen tuple shape."""
    record = _record("hello", position=RecordPosition(ordinal=0, quality="source_order"))
    unit = ConversationUnit(
        thread_id="agt1:example",
        records=(record,),
        linear_records=(record,),
        fidelity="source_order",
    )

    assert tuple(field.name for field in dataclasses.fields(unit)) == (
        "thread_id",
        "records",
        "linear_records",
        "fidelity",
    )
    assert not hasattr(unit, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.cast("t.Any", unit).fidelity = "unordered"


def test_group_conversation_units_omits_null_thread_records() -> None:
    """Flat records remain outside the canonical conversation projection."""
    flat = _record(
        "flat",
        session_id=None,
        identity_namespace=None,
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    threaded = _record(
        "threaded",
        position=RecordPosition(ordinal=1, quality="source_order"),
    )

    units = group_conversation_units((flat, threaded))

    assert len(units) == 1
    assert units[0].records == (threaded,)
    assert all(unit.thread_id is not None for unit in units)
    assert group_conversation_units((flat,)) == ()


def test_group_conversation_units_separates_equal_native_sessions_by_agent() -> None:
    """Equal backend session strings from different namespaces cannot merge."""
    codex = _record(
        "codex",
        agent="codex",
        identity_namespace="codex.session",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    claude = _record(
        "claude",
        agent="claude",
        identity_namespace="claude.session",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )

    units = group_conversation_units((codex, claude))

    assert len(units) == 2
    assert [unit.thread_id for unit in units] == sorted(unit.thread_id for unit in units)
    assert {unit.records[0].agent for unit in units} == {"codex", "claude"}


def test_group_conversation_units_preserves_repeated_identical_occurrences() -> None:
    """Equal content at distinct source positions remains two logical turns."""
    first = _record(
        "repeat",
        position=RecordPosition(ordinal=2, quality="source_order"),
    )
    second = _record(
        "repeat",
        position=RecordPosition(ordinal=5, quality="source_order"),
    )

    unit = _only_unit((second, first))
    identities = tuple(record_identity(record) for record in unit.records)

    assert len(unit.records) == 2
    assert identities[0].content_id == identities[1].content_id
    assert identities[0].record_id != identities[1].record_id
    assert unit.linear_records == (first, second)


@pytest.mark.parametrize("project_ordinal", [0, 1], ids=("equal", "unequal"))
def test_group_conversation_units_does_not_order_cross_domain_ordinals(
    project_ordinal: int,
) -> None:
    """Claude history and project ordinals do not share one timeline."""
    history = _record(
        "same turn",
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
        identity_namespace="claude.session",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    project = dataclasses.replace(
        history,
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        position=RecordPosition(ordinal=project_ordinal, quality="source_order"),
    )

    unit = _only_unit((project, history))
    record_ids = {record_identity(record).record_id for record in unit.records}
    dedupe_keys = {orchestration.record_dedupe_key(record) for record in unit.records}

    assert None not in record_ids
    assert len(record_ids) == 2
    assert len(dedupe_keys) == 2
    assert unit.linear_records is None
    assert unit.fidelity == "unordered"


def test_group_conversation_units_does_not_order_cross_file_ordinals() -> None:
    """File-local ordinals from separate physical sources are not one timeline."""
    first = _record(
        "first file",
        path="parts/first.jsonl",
        position=RecordPosition(ordinal=2, quality="source_order"),
    )
    second = _record(
        "second file",
        path="parts/second.jsonl",
        position=RecordPosition(ordinal=1, quality="source_order"),
    )

    unit = _only_unit((first, second))

    assert {record.path for record in unit.records} == {first.path, second.path}
    assert unit.linear_records is None
    assert unit.fidelity == "unordered"


def test_group_conversation_units_retains_duplicate_views_but_withholds_linearity() -> None:
    """Duplicate native views and revisions stay lossless but not linear."""
    first = _record(
        "original",
        position=RecordPosition(native_id="message-1", ordinal=0, quality="native"),
    )
    revised_view = dataclasses.replace(
        first,
        text="revised",
        store="codex.sessions.revised",
        adapter_id="codex.sessions_revised.v1",
        path=pathlib.Path("revised.jsonl"),
        position=RecordPosition(native_id="message-1", ordinal=1, quality="native"),
    )

    unit = _only_unit((revised_view, first))
    identities = tuple(record_identity(record) for record in unit.records)

    assert len(unit.records) == 2
    assert set(map(id, unit.records)) == {id(first), id(revised_view)}
    assert len({identity.record_id for identity in identities}) == 2
    assert [record.position.ordinal for record in unit.records if record.position] == [0, 1]
    assert {record.position.native_id for record in unit.records if record.position} == {
        "message-1"
    }
    assert unit.linear_records is None
    assert unit.fidelity == "unordered"


def test_group_conversation_units_orders_units_and_members_under_input_permutations() -> None:
    """Input enumeration cannot affect the canonical conversation projection."""
    records = (
        _record(
            "late-a",
            session_id="session-a",
            position=RecordPosition(ordinal=4, quality="source_order"),
        ),
        _record(
            "early-a",
            session_id="session-a",
            position=RecordPosition(ordinal=1, quality="source_order"),
        ),
        _record(
            "only-b",
            session_id="session-b",
            position=RecordPosition(native_id="native-b", quality="native"),
        ),
    )
    projections = {
        _unit_projection(group_conversation_units(permutation))
        for permutation in itertools.permutations(records)
    }

    assert len(projections) == 1
    units = group_conversation_units(records)
    assert len(units) == 2
    assert [unit.thread_id for unit in units] == sorted(unit.thread_id for unit in units)


def test_inventory_key_compares_physical_fields_before_exact_text() -> None:
    """Cheap physical fields break ties before potentially large exact text."""
    fields = conversations._InventoryKey._fields
    text_index = fields.index("text")

    assert fields[-1] == "text"
    assert all(fields.index(field) < text_index for field in ("store", "adapter_id", "path"))


def test_group_conversation_units_orders_timestamps_under_all_permutations() -> None:
    """Timestamp is a deterministic inventory tie-breaker, not chronology."""
    base = _record("same", position=RecordPosition(native_id="same"))
    records = tuple(
        dataclasses.replace(base, timestamp=timestamp)
        for timestamp in ("2030-01-01T00:00:00Z", "2020-01-01T00:00:00Z")
    )

    orders = _inventory_orders(records, lambda record: record.timestamp or "")

    assert len(orders) == 1
    assert set(next(iter(orders))) == {"2030-01-01T00:00:00Z", "2020-01-01T00:00:00Z"}


@pytest.mark.parametrize("field", ["title", "model", "conversation_id"])
def test_group_conversation_units_orders_original_optional_scalars(
    field: str,
) -> None:
    """Optional public scalars retain absence and exact value in the key."""
    base = _record("same", position=RecordPosition(native_id="same"))
    records: list[SearchRecord] = []
    for value in (None, ""):
        record = dataclasses.replace(base)
        setattr(record, field, value)
        records.append(record)

    orders = _inventory_orders(
        tuple(records),
        lambda record: json.dumps(getattr(record, field)),
    )

    assert len(orders) == 1
    assert set(next(iter(orders))) == {"null", '""'}


@pytest.mark.parametrize(
    "roles",
    [("USER", "user"), (None, "")],
    ids=("exact-case", "absence"),
)
def test_group_conversation_units_orders_original_roles(
    roles: tuple[str | None, str | None],
) -> None:
    """Role casefolding for identity does not erase the public role value."""
    base = _record("same", position=RecordPosition(native_id="same"))
    records = tuple(dataclasses.replace(base, role=role) for role in roles)

    orders = _inventory_orders(records, lambda record: json.dumps(record.role))

    assert len(orders) == 1
    assert set(next(iter(orders))) == {json.dumps(role) for role in roles}


def test_group_conversation_units_orders_position_presence_and_quality() -> None:
    """Position presence and quality complete otherwise equal topology keys."""
    base = _record("same", position=None)
    presence_records = (base, dataclasses.replace(base, position=RecordPosition()))
    quality_records = tuple(
        dataclasses.replace(
            base,
            position=RecordPosition(quality=quality),
        )
        for quality in ("native", "source_order")
    )

    presence_orders = _inventory_orders(
        presence_records,
        lambda record: "missing" if record.position is None else "present",
    )
    quality_orders = _inventory_orders(
        quality_records,
        lambda record: (
            (record.position.quality or "none") if record.position is not None else "missing"
        ),
    )

    assert len(presence_orders) == 1
    assert len(quality_orders) == 1


@pytest.mark.parametrize(
    "field",
    ["cwd", "repo", "worktree", "branch", "remote", "cwd_hash"],
)
def test_group_conversation_units_orders_each_origin_field_under_all_permutations(
    field: str,
) -> None:
    """Every origin scalar participates in deterministic inventory order."""
    base = _record("same", position=RecordPosition(native_id="same"))
    records: list[SearchRecord] = []
    for value in ("zeta", "alpha"):
        values = {field: value}
        records.append(
            dataclasses.replace(
                base,
                origin=RecordOrigin(**t.cast("t.Any", values)),
            ),
        )

    orders = _inventory_orders(
        tuple(records),
        lambda record: t.cast("str", getattr(record.origin, field)),
    )

    assert len(orders) == 1
    assert set(next(iter(orders))) == {"zeta", "alpha"}


def test_group_conversation_units_orders_origin_presence_under_all_permutations() -> None:
    """A missing origin differs deterministically from an empty origin value."""
    base = _record("same", position=RecordPosition(native_id="same"))
    records = (base, dataclasses.replace(base, origin=RecordOrigin()))

    orders = _inventory_orders(
        records,
        lambda record: "missing" if record.origin is None else "present",
    )

    assert len(orders) == 1


def test_group_conversation_units_orders_nested_metadata_under_all_permutations() -> None:
    """Nested JSON-compatible metadata has one typed canonical order."""
    base = _record("same", position=RecordPosition(native_id="same"))
    records = tuple(
        dataclasses.replace(
            base,
            metadata={
                "nested": {
                    "items": [None, False, 1, 1.5, {"value": value}],
                },
            },
        )
        for value in ("zeta", "alpha", "middle")
    )

    orders = _inventory_orders(
        records,
        lambda record: json.dumps(record.metadata, sort_keys=True),
    )

    assert len(orders) == 1
    assert len(next(iter(orders))) == 3


@pytest.mark.parametrize(
    "case",
    RAW_TOPOLOGY_CASES,
    ids=[case.test_id for case in RAW_TOPOLOGY_CASES],
)
def test_group_conversation_units_orders_invalid_raw_topology_under_input_permutations(
    case: RawTopologyCase,
) -> None:
    """Invalid typed scalars remain deterministic inventory tie-breakers."""
    records = tuple(
        _record(
            "same content",
            position=_raw_topology_position(case.field, value),
        )
        for value in RAW_TOPOLOGY_VALUES
    )
    projections = {
        tuple(
            getattr(record.position, case.field)
            for record in _only_unit(permutation).records
            if record.position is not None
        )
        for permutation in itertools.permutations(records)
    }

    assert projections == {RAW_TOPOLOGY_VALUES}
    assert _only_unit(records).fidelity == "unordered"


def test_group_conversation_units_orders_ordinal_above_decimal_conversion_limit() -> None:
    """Valid huge ordinals remain groupable under every input permutation."""
    previous_limit = sys.get_int_max_str_digits()
    configured_limit = sys.int_info.str_digits_check_threshold
    sys.set_int_max_str_digits(configured_limit)
    try:
        huge_ordinal = 10**configured_limit
        records = (
            _record(
                "late",
                position=RecordPosition(
                    native_id="message-late",
                    ordinal=huge_ordinal,
                    quality="native",
                ),
            ),
            _record(
                "early",
                position=RecordPosition(
                    native_id="message-early",
                    ordinal=0,
                    quality="native",
                ),
            ),
        )
        try:
            projections = {
                tuple(
                    record.position.ordinal
                    for record in _only_unit(permutation).records
                    if record.position is not None
                )
                for permutation in itertools.permutations(records)
            }
        except ValueError as error:
            pytest.fail(f"grouping converted a valid ordinal to decimal: {error}")
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert len(projections) == 1
    projection = next(iter(projections))
    assert projection[0] == 0
    assert projection[1] == huge_ordinal


def test_group_conversation_units_orders_pathological_float_topology() -> None:
    """Float topology tie-breakers preserve deterministic IEEE bit order."""
    values = tuple(struct.unpack("!d", bytes.fromhex(bits))[0] for bits in PATHOLOGICAL_FLOAT_BITS)
    records = tuple(
        _record(
            "same content",
            position=RecordPosition(
                native_id="message-1",
                ordinal=t.cast("t.Any", value),
                quality="native",
            ),
        )
        for value in values
    )
    projections = {
        tuple(
            struct.pack("!d", t.cast("float", record.position.ordinal)).hex()
            for record in _only_unit(permutation).records
            if record.position is not None
        )
        for permutation in itertools.permutations(records)
    }

    assert projections == {PATHOLOGICAL_FLOAT_BITS}


def test_group_conversation_units_linearizes_unique_gapped_ordinals() -> None:
    """Gapped, nonzero ordinals remain a proven observed order."""
    records = tuple(
        _record(
            f"ordinal-{ordinal}",
            position=RecordPosition(ordinal=ordinal, quality="source_order"),
        )
        for ordinal in (9, 2, 5)
    )

    unit = _only_unit(records)

    assert unit.fidelity == "source_order"
    assert unit.linear_records is not None
    assert [record.position.ordinal for record in unit.linear_records if record.position] == [
        2,
        5,
        9,
    ]


@pytest.mark.parametrize("case", ORDINAL_CASES, ids=[case.test_id for case in ORDINAL_CASES])
def test_group_conversation_units_withholds_linear_records(case: OrdinalCase) -> None:
    """Missing or invalid ordinal evidence cannot create transcript order."""
    records = tuple(
        _record(
            f"record-{index}",
            position=RecordPosition(
                native_id=f"message-{index}",
                ordinal=t.cast("t.Any", ordinal),
                quality="native",
            ),
        )
        for index, ordinal in enumerate(case.ordinals)
    )

    unit = _only_unit(records)
    record_ids = [record_identity(record).record_id for record in records]

    assert None not in record_ids
    assert len(set(record_ids)) == len(record_ids)
    assert len({record.position.native_id for record in records if record.position}) == len(records)
    assert unit.linear_records is None
    assert unit.fidelity == "unordered"


def test_group_conversation_units_preserves_native_parent_links_without_linearity() -> None:
    """Observed native ancestry remains a graph when sibling order is absent."""
    root = _record(
        "root",
        position=RecordPosition(native_id="root", quality="native"),
    )
    child = _record(
        "child",
        position=RecordPosition(
            native_id="child",
            parent_native_id="root",
            quality="native",
        ),
    )

    unit = _only_unit((child, root))

    assert unit.fidelity == "native_tree"
    assert unit.linear_records is None
    assert {record.position for record in unit.records} == {root.position, child.position}


def test_group_conversation_units_exposes_linear_projection_for_ordered_native_tree() -> None:
    """Native ancestry and complete ordinals remain independent projections."""
    root = _record(
        "root",
        position=RecordPosition(native_id="root", ordinal=0, quality="native"),
    )
    child = _record(
        "child",
        position=RecordPosition(
            native_id="child",
            parent_native_id="root",
            ordinal=1,
            quality="native",
        ),
    )

    unit = _only_unit((child, root))

    assert unit.fidelity == "native_tree"
    assert unit.linear_records == (root, child)


def test_group_conversation_units_treats_native_ids_without_parents_as_unordered() -> None:
    """Native occurrence identity and malformed parents are not topology."""
    empty_parent = _record(
        "empty parent",
        position=RecordPosition(native_id="message-1", parent_native_id="", quality="native"),
    )
    invalid_parent = _record(
        "invalid parent",
        position=RecordPosition(
            native_id="message-2",
            parent_native_id=t.cast("t.Any", True),
            quality="native",
        ),
    )

    unit = _only_unit((empty_parent, invalid_parent))

    assert unit.fidelity == "unordered"
    assert unit.linear_records is None


def test_group_conversation_units_uses_ordinals_across_mixed_position_quality() -> None:
    """Position quality does not hide an otherwise proven ordinal order."""
    native = _record(
        "user",
        position=RecordPosition(native_id="request-1", ordinal=0, quality="native"),
    )
    source_order = _record(
        "assistant",
        position=RecordPosition(ordinal=1, quality="source_order"),
    )

    unit = _only_unit((source_order, native))

    assert unit.fidelity == "source_order"
    assert unit.linear_records == (native, source_order)


def test_group_conversation_units_does_not_use_timestamps_as_order_evidence() -> None:
    """Timestamps neither create nor change a conversation ordering claim."""
    first = _record(
        "first",
        timestamp="2030-01-01T00:00:00Z",
        position=RecordPosition(native_id="message-b", quality="native"),
    )
    second = _record(
        "second",
        timestamp="2020-01-01T00:00:00Z",
        position=RecordPosition(native_id="message-a", quality="native"),
    )

    unit = _only_unit((first, second))

    assert unit.fidelity == "unordered"
    assert unit.linear_records is None
    assert [record.position.native_id for record in unit.records if record.position] == [
        "message-a",
        "message-b",
    ]


def test_group_conversation_units_keeps_dangling_native_parents_without_completeness_claims() -> (
    None
):
    """A missing parent stays an observed edge without graph validation."""
    child = _record(
        "child",
        position=RecordPosition(
            native_id="child",
            parent_native_id="not-loaded",
            quality="native",
        ),
    )

    unit = _only_unit((child,))

    assert unit.records == (child,)
    assert unit.fidelity == "native_tree"
    assert unit.linear_records is None


def test_group_conversation_units_never_hashes_threadless_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing topology is rejected before any cryptographic preparation."""
    flat = _record(
        "flat",
        session_id=None,
        identity_namespace=None,
        position=RecordPosition(ordinal=0, quality="source_order"),
    )

    def fail_sha256(*_args: object, **_kwargs: object) -> t.NoReturn:
        pytest.fail("threadless grouping reached SHA-256")

    monkeypatch.setattr(identity.hashlib, "sha256", fail_sha256)

    assert group_conversation_units((flat,)) == ()


def test_group_conversation_units_reuses_retained_thread_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One retained input prepares its thread and full identity exactly once."""
    record = _record(
        "threaded",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    expected_thread_id = record_thread_id(record)
    thread_calls: list[SearchRecord] = []
    identity_calls: list[tuple[SearchRecord, str | None]] = []

    def counting_record_thread_id(value: SearchRecord) -> str | None:
        thread_calls.append(value)
        return record_thread_id(value)

    def counting_record_identity(
        value: SearchRecord,
        *,
        prepared_thread_id: str | None = None,
    ) -> RecordIdentity:
        identity_calls.append((value, prepared_thread_id))
        if prepared_thread_id is None:
            return record_identity(value)
        return record_identity(value, prepared_thread_id=prepared_thread_id)

    monkeypatch.setattr(
        conversations,
        "record_thread_id",
        counting_record_thread_id,
        raising=False,
    )
    monkeypatch.setattr(conversations, "record_identity", counting_record_identity)

    unit = _only_unit((record,))

    assert unit.records == (record,)
    assert thread_calls == [record]
    assert identity_calls == [(record, expected_thread_id)]


def test_group_conversation_units_threadless_megabyte_probe_hashes_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1 MiB threadless body stays outside the identity hashing budget."""
    flat = _record(
        "x" * (1024 * 1024),
        session_id=None,
        identity_namespace=None,
    )
    real_sha256 = identity.hashlib.sha256
    hashed_lengths: list[int] = []

    def tracking_sha256(data: bytes = b"") -> t.Any:
        hashed_lengths.append(len(data))
        return real_sha256(data)

    monkeypatch.setattr(identity.hashlib, "sha256", tracking_sha256)

    assert group_conversation_units((flat,)) == ()

    assert sum(hashed_lengths) == 0


def test_group_conversation_units_consumes_input_once() -> None:
    """One-shot iterables are neither replayed nor scanned a second time."""
    records = (
        _record("first", position=RecordPosition(ordinal=0, quality="source_order")),
        _record("second", position=RecordPosition(ordinal=1, quality="source_order")),
    )

    class OneShotRecords:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> t.Iterator[SearchRecord]:
            self.iterations += 1
            if self.iterations > 1:
                message = "records iterable consumed more than once"
                raise AssertionError(message)
            return iter(records)

    one_shot = OneShotRecords()

    _ = group_conversation_units(one_shot)

    assert one_shot.iterations == 1


def test_private_group_prepared_conversation_units_matches_public_wrapper() -> None:
    """Caller-prepared identities produce the established public units."""
    records = (
        _record("late", position=RecordPosition(ordinal=4, quality="source_order")),
        _record("early", position=RecordPosition(ordinal=1, quality="source_order")),
    )
    prepared = tuple((record, record_identity(record)) for record in records)

    units = conversations._group_prepared_conversation_units(prepared)

    assert _unit_projection(units) == _unit_projection(group_conversation_units(records))


def test_private_group_prepared_conversation_units_consumes_input_once() -> None:
    """Prepared one-shot iterables are neither replayed nor rescanned."""
    record = _record(
        "threaded",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    prepared = ((record, record_identity(record)),)

    class OneShotPreparedRecords:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> t.Iterator[tuple[SearchRecord, RecordIdentity]]:
            self.iterations += 1
            if self.iterations > 1:
                message = "prepared records consumed more than once"
                raise AssertionError(message)
            return iter(prepared)

    one_shot = OneShotPreparedRecords()

    _ = conversations._group_prepared_conversation_units(one_shot)

    assert one_shot.iterations == 1


def test_private_group_prepared_conversation_units_never_rehashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supplied identity bundles remain the only identity work performed."""
    record = _record(
        "threaded",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    prepared = ((record, record_identity(record)),)

    def fail_identity(*_args: object, **_kwargs: object) -> t.NoReturn:
        pytest.fail("prepared conversation grouping recomputed identity")

    monkeypatch.setattr(conversations, "record_thread_id", fail_identity)
    monkeypatch.setattr(conversations, "record_identity", fail_identity)

    units = conversations._group_prepared_conversation_units(prepared)

    assert units[0].records == (record,)
