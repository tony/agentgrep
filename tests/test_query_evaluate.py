"""Unit tests for the query evaluator's trilean source eval and record eval.

The public engine tests (:mod:`tests.test_query_engine`) drive the evaluator
through compiled queries. These tests pin the layer underneath: the Kleene
three-valued algebra used to prune sources before reading them, and the
per-field record-eval branches. The algebra is finite, so the AND/OR/NOT
tables are exhaustive rather than sampled.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.query.ast import (
    AndNode,
    FieldCmpNode,
    FieldEqNode,
    FieldExistsNode,
    FieldRangeNode,
    NotNode,
    OrNode,
    QueryNode,
    TermNode,
)
from agentgrep.query.errors import QueryCompileError
from agentgrep.query.evaluate import (
    _compare,
    _enum_eq,
    _evaluate_record,
    _evaluate_source,
    _field_exists_on_record,
    _mtime_as_datetime,
    _origin_field_exists_on_source,
    _record_timestamp_as_datetime,
    _Trilean,
)
from agentgrep.query.registry import default_registry
from agentgrep.records import RecordOrigin, SourceOriginSummary

_REGISTRY = default_registry()


def _source(
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path: str = "/tmp/codex/sessions/abc.jsonl",
    mtime_ns: int = 0,
    origin_summary: SourceOriginSummary | None = None,
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for evaluator tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=mtime_ns,
        origin_summary=origin_summary,
    )


def _record(
    *,
    agent: agentgrep.AgentName = "codex",
    text: str = "bliss",
    path: str = "/tmp/codex/sessions/abc.jsonl",
    title: str | None = None,
    role: str | None = "user",
    timestamp: str | None = None,
    model: str | None = None,
    origin: RecordOrigin | None = None,
) -> agentgrep.SearchRecord:
    """Build a synthetic record for evaluator tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=agent,
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path(path),
        text=text,
        title=title,
        role=role,
        timestamp=timestamp,
        model=model,
        session_id=None,
        conversation_id=None,
        origin=origin,
        metadata={},
    )


def _eval_source(node: QueryNode, source: agentgrep.SourceHandle) -> _Trilean:
    """Evaluate ``node`` against ``source`` with the default registry."""
    return _evaluate_source(node, source, _REGISTRY, {})


# --- Kleene three-valued leaves -------------------------------------------
#
# The evaluator prunes a source only when a predicate is definitely false (F)
# given source facts; record-level predicates stay unknown (U) until the file
# is read. These three leaves produce each trilean state deterministically so
# the AND/OR/NOT tables below can be built exhaustively.
_LEAF_T = FieldEqNode(field="agent", value="codex")  # matches the codex source
_LEAF_F = FieldEqNode(field="agent", value="claude")  # cannot match it
_LEAF_U = TermNode(value="anything")  # bare term is always record-level


def test_kleene_leaves_have_expected_states() -> None:
    """The three fixture leaves really evaluate to T, F, and U."""
    source = _source(agent="codex")
    assert _eval_source(_LEAF_T, source) == "T"
    assert _eval_source(_LEAF_F, source) == "F"
    assert _eval_source(_LEAF_U, source) == "U"


class KleeneBinaryCase(t.NamedTuple):
    """One row of a binary Kleene truth table."""

    test_id: str
    left: _Trilean
    right: _Trilean
    expected: _Trilean


_LEAVES: dict[_Trilean, QueryNode] = {"T": _LEAF_T, "F": _LEAF_F, "U": _LEAF_U}


def _kleene_and(left: _Trilean, right: _Trilean) -> _Trilean:
    if "F" in (left, right):
        return "F"
    if "U" in (left, right):
        return "U"
    return "T"


def _kleene_or(left: _Trilean, right: _Trilean) -> _Trilean:
    if "T" in (left, right):
        return "T"
    if "U" in (left, right):
        return "U"
    return "F"


_STATES: tuple[_Trilean, ...] = ("T", "F", "U")

AND_CASES: tuple[KleeneBinaryCase, ...] = tuple(
    KleeneBinaryCase(
        test_id=f"and-{left}-{right}-is-{_kleene_and(left, right)}",
        left=left,
        right=right,
        expected=_kleene_and(left, right),
    )
    for left in _STATES
    for right in _STATES
)

OR_CASES: tuple[KleeneBinaryCase, ...] = tuple(
    KleeneBinaryCase(
        test_id=f"or-{left}-{right}-is-{_kleene_or(left, right)}",
        left=left,
        right=right,
        expected=_kleene_or(left, right),
    )
    for left in _STATES
    for right in _STATES
)


@pytest.mark.parametrize("case", AND_CASES, ids=[c.test_id for c in AND_CASES])
def test_source_eval_and_is_kleene(case: KleeneBinaryCase) -> None:
    """AND over {T,F,U}^2 prunes only when a conjunct is definitely false."""
    node = AndNode(children=(_LEAVES[case.left], _LEAVES[case.right]))
    assert _eval_source(node, _source(agent="codex")) == case.expected


@pytest.mark.parametrize("case", OR_CASES, ids=[c.test_id for c in OR_CASES])
def test_source_eval_or_is_kleene(case: KleeneBinaryCase) -> None:
    """OR over {T,F,U}^2 keeps a source unless every disjunct is false."""
    node = OrNode(children=(_LEAVES[case.left], _LEAVES[case.right]))
    assert _eval_source(node, _source(agent="codex")) == case.expected


class KleeneUnaryCase(t.NamedTuple):
    """One row of the NOT truth table."""

    test_id: str
    inner: _Trilean
    expected: _Trilean


NOT_CASES: tuple[KleeneUnaryCase, ...] = (
    KleeneUnaryCase(test_id="not-T-is-F", inner="T", expected="F"),
    KleeneUnaryCase(test_id="not-F-is-T", inner="F", expected="T"),
    KleeneUnaryCase(test_id="not-U-is-U", inner="U", expected="U"),
)


@pytest.mark.parametrize("case", NOT_CASES, ids=[c.test_id for c in NOT_CASES])
def test_source_eval_not_is_kleene(case: KleeneUnaryCase) -> None:
    """NOT flips a definite state and leaves the unknown state unknown."""
    node = NotNode(child=_LEAVES[case.inner])
    assert _eval_source(node, _source(agent="codex")) == case.expected


# --- Source-eval node-type x edge-state matrix ----------------------------


class SourceEvalCase(t.NamedTuple):
    """A source-eval case naming the invariant it pins."""

    test_id: str
    node: QueryNode
    mtime_ns: int
    expected: _Trilean


SOURCE_EVAL_CASES: tuple[SourceEvalCase, ...] = (
    SourceEvalCase(
        test_id="exists-unknown-field-is-U",
        node=FieldExistsNode(field="nonesuch"),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="exists-record-field-is-U",
        node=FieldExistsNode(field="model"),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="exists-source-field-is-T",
        node=FieldExistsNode(field="store"),
        mtime_ns=0,
        expected="T",
    ),
    SourceEvalCase(
        test_id="exists-mtime-unknown-when-stat-failed-is-U",
        node=FieldExistsNode(field="mtime"),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="exists-mtime-present-is-T",
        node=FieldExistsNode(field="mtime"),
        mtime_ns=1_700_000_000_000_000_000,
        expected="T",
    ),
    SourceEvalCase(
        test_id="eq-unknown-field-is-U",
        node=FieldEqNode(field="nonesuch", value="x"),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="eq-record-field-is-U",
        node=FieldEqNode(field="model", value="sonnet"),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="eq-mtime-unknown-when-stat-failed-is-U-not-F",
        node=FieldEqNode(field="mtime", value="2026-01-01"),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="range-record-field-is-U",
        node=FieldRangeNode(
            field="timestamp",
            lo="2026-01-01",
            hi="2026-12-31",
            inclusive_lo=True,
            inclusive_hi=True,
        ),
        mtime_ns=0,
        expected="U",
    ),
    SourceEvalCase(
        test_id="cmp-record-field-is-U",
        node=FieldCmpNode(field="timestamp", op="gt", value="2026-01-01"),
        mtime_ns=0,
        expected="U",
    ),
)


@pytest.mark.parametrize(
    "case",
    SOURCE_EVAL_CASES,
    ids=[c.test_id for c in SOURCE_EVAL_CASES],
)
def test_source_eval_node_edge_states(case: SourceEvalCase) -> None:
    """Node type x edge state maps to the documented trilean."""
    source = _source(agent="codex", mtime_ns=case.mtime_ns)
    assert _eval_source(case.node, source) == case.expected


def test_source_eval_matches_source_field_is_T_or_F() -> None:
    """A satisfied source-layer predicate is T; an unsatisfiable one is F."""
    source = _source(agent="codex", store="sessions")
    assert _eval_source(FieldEqNode(field="store", value="sessions"), source) == "T"
    assert _eval_source(FieldEqNode(field="store", value="other"), source) == "F"


# --- _origin_field_exists_on_source ---------------------------------------


class OriginExistsCase(t.NamedTuple):
    """Trilean cases for origin-field existence on a source summary."""

    test_id: str
    summary: SourceOriginSummary | None
    expected: _Trilean


ORIGIN_EXISTS_CASES: tuple[OriginExistsCase, ...] = (
    OriginExistsCase(test_id="no-summary-is-U", summary=None, expected="U"),
    OriginExistsCase(
        test_id="field-incomplete-is-U",
        summary=SourceOriginSummary(
            origins=(RecordOrigin(branch="main"),),
            complete_fields=frozenset(),
        ),
        expected="U",
    ),
    OriginExistsCase(
        test_id="no-origins-is-F",
        summary=SourceOriginSummary(origins=(), complete_fields=frozenset({"branch"})),
        expected="F",
    ),
    OriginExistsCase(
        test_id="all-origins-have-field-is-T",
        summary=SourceOriginSummary(
            origins=(RecordOrigin(branch="main"), RecordOrigin(branch="dev")),
            complete_fields=frozenset({"branch"}),
        ),
        expected="T",
    ),
    OriginExistsCase(
        test_id="some-origins-have-field-is-U",
        summary=SourceOriginSummary(
            origins=(RecordOrigin(branch="main"), RecordOrigin()),
            complete_fields=frozenset({"branch"}),
        ),
        expected="U",
    ),
    OriginExistsCase(
        test_id="no-origin-has-field-is-F",
        summary=SourceOriginSummary(
            origins=(RecordOrigin(), RecordOrigin()),
            complete_fields=frozenset({"branch"}),
        ),
        expected="F",
    ),
)


@pytest.mark.parametrize(
    "case",
    ORIGIN_EXISTS_CASES,
    ids=[c.test_id for c in ORIGIN_EXISTS_CASES],
)
def test_origin_field_exists_on_source(case: OriginExistsCase) -> None:
    """Origin-field existence follows Kleene rules over the summary."""
    source = _source(origin_summary=case.summary)
    assert _origin_field_exists_on_source("branch", source) == case.expected


# --- _field_exists_on_record ----------------------------------------------


class RecordExistsCase(t.NamedTuple):
    """One presence check against a record field."""

    test_id: str
    field: str
    record: agentgrep.SearchRecord
    expected: bool


RECORD_EXISTS_CASES: tuple[RecordExistsCase, ...] = (
    RecordExistsCase(
        test_id="agent-always-present", field="agent", record=_record(), expected=True
    ),
    RecordExistsCase(
        test_id="mtime-always-present", field="mtime", record=_record(), expected=True
    ),
    RecordExistsCase(
        test_id="scope-always-present", field="scope", record=_record(), expected=True
    ),
    RecordExistsCase(test_id="path-present", field="path", record=_record(), expected=True),
    RecordExistsCase(
        test_id="model-present",
        field="model",
        record=_record(model="sonnet"),
        expected=True,
    ),
    RecordExistsCase(
        test_id="model-absent",
        field="model",
        record=_record(model=None),
        expected=False,
    ),
    RecordExistsCase(
        test_id="role-present",
        field="role",
        record=_record(role="user"),
        expected=True,
    ),
    RecordExistsCase(
        test_id="role-absent",
        field="role",
        record=_record(role=None),
        expected=False,
    ),
    RecordExistsCase(
        test_id="timestamp-present",
        field="timestamp",
        record=_record(timestamp="2026-07-18T00:00:00Z"),
        expected=True,
    ),
    RecordExistsCase(
        test_id="timestamp-absent",
        field="timestamp",
        record=_record(timestamp=None),
        expected=False,
    ),
    RecordExistsCase(test_id="text-present", field="text", record=_record(text="x"), expected=True),
    RecordExistsCase(test_id="text-empty", field="text", record=_record(text=""), expected=False),
    RecordExistsCase(
        test_id="origin-branch-present",
        field="branch",
        record=_record(origin=RecordOrigin(branch="main")),
        expected=True,
    ),
    RecordExistsCase(
        test_id="origin-branch-absent",
        field="branch",
        record=_record(origin=None),
        expected=False,
    ),
    RecordExistsCase(
        test_id="unknown-field-absent",
        field="nonesuch",
        record=_record(),
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    RECORD_EXISTS_CASES,
    ids=[c.test_id for c in RECORD_EXISTS_CASES],
)
def test_field_exists_on_record(case: RecordExistsCase) -> None:
    """Field presence at the record layer matches the documented rules."""
    assert _field_exists_on_record(case.field, case.record) is case.expected


# --- _compare (all four operators) ----------------------------------------

_EARLIER = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


class CompareCase(t.NamedTuple):
    """One datetime comparison over an operator."""

    test_id: str
    moment: dt.datetime
    op: t.Literal["gt", "lt", "gte", "lte"]
    bound: dt.datetime
    expected: bool


COMPARE_CASES: tuple[CompareCase, ...] = (
    CompareCase(test_id="gt-true", moment=_LATER, op="gt", bound=_EARLIER, expected=True),
    CompareCase(test_id="gt-false", moment=_EARLIER, op="gt", bound=_LATER, expected=False),
    CompareCase(test_id="lt-true", moment=_EARLIER, op="lt", bound=_LATER, expected=True),
    CompareCase(test_id="lt-false", moment=_LATER, op="lt", bound=_EARLIER, expected=False),
    CompareCase(test_id="gte-equal", moment=_EARLIER, op="gte", bound=_EARLIER, expected=True),
    CompareCase(test_id="gte-below", moment=_EARLIER, op="gte", bound=_LATER, expected=False),
    CompareCase(test_id="lte-equal", moment=_LATER, op="lte", bound=_LATER, expected=True),
    CompareCase(test_id="lte-above", moment=_LATER, op="lte", bound=_EARLIER, expected=False),
)


@pytest.mark.parametrize("case", COMPARE_CASES, ids=[c.test_id for c in COMPARE_CASES])
def test_compare_operators(case: CompareCase) -> None:
    """Each comparison operator returns the expected boolean."""
    assert _compare(case.moment, case.op, case.bound) is case.expected


# --- _enum_eq -------------------------------------------------------------


def test_enum_eq_matches_valid_value() -> None:
    """A declared enum value compares by exact equality."""
    spec = _REGISTRY.get("agent")
    assert spec is not None
    assert _enum_eq("codex", "codex", spec) is True
    assert _enum_eq("codex", "claude", spec) is False


def test_enum_eq_rejects_unknown_value() -> None:
    """An undeclared enum value raises so typos surface at compile time."""
    spec = _REGISTRY.get("agent")
    assert spec is not None
    with pytest.raises(QueryCompileError, match="invalid agent value"):
        _enum_eq("codex", "nonesuch", spec)


# --- _mtime_as_datetime / _record_timestamp_as_datetime -------------------


def test_mtime_as_datetime_none_when_stat_failed() -> None:
    """A non-positive mtime means the stat failed; there is no datetime."""
    assert _mtime_as_datetime(0) is None
    assert _mtime_as_datetime(-1) is None


def test_mtime_as_datetime_converts_positive_ns() -> None:
    """A positive mtime converts to an aware UTC datetime."""
    moment = _mtime_as_datetime(1_700_000_000_000_000_000)
    assert moment is not None
    assert moment.tzinfo is dt.UTC


class TimestampParseCase(t.NamedTuple):
    """One record-timestamp parse case."""

    test_id: str
    raw: str | None
    expected: dt.datetime | None


TIMESTAMP_CASES: tuple[TimestampParseCase, ...] = (
    TimestampParseCase(test_id="none-is-none", raw=None, expected=None),
    TimestampParseCase(test_id="unparseable-is-none", raw="not-a-date", expected=None),
    TimestampParseCase(
        test_id="z-suffix-parses-utc",
        raw="2026-07-18T12:00:00Z",
        expected=dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.UTC),
    ),
    TimestampParseCase(
        test_id="naive-coerced-to-utc",
        raw="2026-07-18T12:00:00",
        expected=dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.UTC),
    ),
    TimestampParseCase(
        test_id="offset-converted-to-utc",
        raw="2026-07-18T07:00:00-05:00",
        expected=dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.UTC),
    ),
)


@pytest.mark.parametrize(
    "case",
    TIMESTAMP_CASES,
    ids=[c.test_id for c in TIMESTAMP_CASES],
)
def test_record_timestamp_as_datetime(case: TimestampParseCase) -> None:
    """Record timestamps parse to aware UTC datetimes or None."""
    assert _record_timestamp_as_datetime(case.raw) == case.expected


# --- record eval branches -------------------------------------------------


def test_record_eval_model_and_role_fields() -> None:
    """Record-layer string fields match by substring on the record value."""
    record = _record(model="claude-sonnet", role="assistant")
    assert _evaluate_record(FieldEqNode(field="model", value="sonnet"), record, _REGISTRY, {})
    assert not _evaluate_record(FieldEqNode(field="model", value="opus"), record, _REGISTRY, {})
    assert _evaluate_record(FieldEqNode(field="role", value="assistant"), record, _REGISTRY, {})


def test_record_eval_source_field_via_record_metadata() -> None:
    """Source-layer fields answer from the record's own copies."""
    record = _record(agent="codex", path="/tmp/codex/sessions/abc.jsonl")
    assert _evaluate_record(FieldEqNode(field="agent", value="codex"), record, _REGISTRY, {})
    assert not _evaluate_record(FieldEqNode(field="agent", value="claude"), record, _REGISTRY, {})


def test_record_eval_unknown_field_is_false() -> None:
    """An unregistered field never matches at the record layer."""
    assert not _evaluate_record(
        FieldEqNode(field="nonesuch", value="x"),
        record=_record(),
        registry=_REGISTRY,
        path_patterns={},
    )


_TS_RECORD = _record(timestamp="2026-06-01T12:00:00Z")
_ORIGIN_RECORD = _record(origin=RecordOrigin(branch="main", cwd="/workspace/proj"))


class RecordEvalCase(t.NamedTuple):
    """One record-eval case over the timestamp/scope/origin/text branches."""

    test_id: str
    node: QueryNode
    record: agentgrep.SearchRecord
    expected: bool


RECORD_EVAL_CASES: tuple[RecordEvalCase, ...] = (
    RecordEvalCase(
        test_id="timestamp-eq-same-day",
        node=FieldEqNode(field="timestamp", value="2026-06-01"),
        record=_TS_RECORD,
        expected=True,
    ),
    RecordEvalCase(
        test_id="timestamp-eq-other-day",
        node=FieldEqNode(field="timestamp", value="2026-01-01"),
        record=_TS_RECORD,
        expected=False,
    ),
    RecordEvalCase(
        test_id="timestamp-cmp-gt",
        node=FieldCmpNode(field="timestamp", op="gt", value="2026-01-01"),
        record=_TS_RECORD,
        expected=True,
    ),
    RecordEvalCase(
        test_id="timestamp-cmp-lt-false",
        node=FieldCmpNode(field="timestamp", op="lt", value="2026-01-01"),
        record=_TS_RECORD,
        expected=False,
    ),
    RecordEvalCase(
        test_id="timestamp-range-inside",
        node=FieldRangeNode(
            field="timestamp",
            lo="2026-01-01",
            hi="2026-12-31",
            inclusive_lo=True,
            inclusive_hi=True,
        ),
        record=_TS_RECORD,
        expected=True,
    ),
    RecordEvalCase(
        test_id="timestamp-range-outside",
        node=FieldRangeNode(
            field="timestamp",
            lo="2025-01-01",
            hi="2025-12-31",
            inclusive_lo=True,
            inclusive_hi=True,
        ),
        record=_TS_RECORD,
        expected=False,
    ),
    RecordEvalCase(
        test_id="timestamp-cmp-missing-timestamp-false",
        node=FieldCmpNode(field="timestamp", op="gt", value="2026-01-01"),
        record=_record(timestamp=None),
        expected=False,
    ),
    RecordEvalCase(
        test_id="scope-prompts-matches-prompt",
        node=FieldEqNode(field="scope", value="prompts"),
        record=_record(),
        expected=True,
    ),
    RecordEvalCase(
        test_id="scope-conversations-excludes-prompt",
        node=FieldEqNode(field="scope", value="conversations"),
        record=_record(),
        expected=False,
    ),
    RecordEvalCase(
        test_id="text-wildcard-anchored-hit",
        node=FieldEqNode(field="text", value="bli*"),
        record=_record(text="bliss"),
        expected=True,
    ),
    RecordEvalCase(
        test_id="text-wildcard-anchored-miss",
        node=FieldEqNode(field="text", value="xyz*"),
        record=_record(text="bliss"),
        expected=False,
    ),
    RecordEvalCase(
        test_id="text-plain-substring",
        node=FieldEqNode(field="text", value="lis"),
        record=_record(text="bliss"),
        expected=True,
    ),
    RecordEvalCase(
        test_id="origin-branch-hit",
        node=FieldEqNode(field="branch", value="main"),
        record=_ORIGIN_RECORD,
        expected=True,
    ),
    RecordEvalCase(
        test_id="origin-branch-miss",
        node=FieldEqNode(field="branch", value="dev"),
        record=_ORIGIN_RECORD,
        expected=False,
    ),
    RecordEvalCase(
        test_id="origin-cwd-path-hit",
        node=FieldEqNode(field="cwd", value="/workspace/proj"),
        record=_ORIGIN_RECORD,
        expected=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    RECORD_EVAL_CASES,
    ids=[c.test_id for c in RECORD_EVAL_CASES],
)
def test_record_eval_field_branches(case: RecordEvalCase) -> None:
    """Record-layer field predicates match the empirically verified outcome."""
    assert _evaluate_record(case.node, case.record, _REGISTRY, {}) is case.expected
