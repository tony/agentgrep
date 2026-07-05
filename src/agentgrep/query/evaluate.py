"""Three-valued source eval and exact record eval for compiled queries."""

from __future__ import annotations

import datetime as dt
import typing as t

from agentgrep._engine.orchestration import record_matches_scope
from agentgrep.origin import (
    ORIGIN_PATH_QUERY_FIELDS,
    ORIGIN_QUERY_FIELDS,
    OriginMatcher,
    record_origin_field_values,
)
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
from agentgrep.query.dates import (
    DateParseError,
    DateRange,
    equality_range,
    parse_date_literal,
    parse_range_bound,
)
from agentgrep.query.errors import QueryCompileError
from agentgrep.query.pathmatch import (
    _CompiledPathPattern,
    _eq_value,
    _path_match,
    _path_pattern_for,
    _PathPatternKey,
)
from agentgrep.query.registry import FieldRegistry, FieldSpec
from agentgrep.query.textmatch import (
    _is_wildcard,
    _string_match,
    _text_matches,
)
from agentgrep.records import SearchRecord, SearchScope, SourceHandle

_Trilean = t.Literal["T", "F", "U"]
"""Three-valued logic state used during conservative source eval.

- ``T`` — predicate is definitely satisfied by the source's known
  facts.
- ``F`` — predicate is definitely not satisfied. The source can be
  pruned without reading any records.
- ``U`` — depends on record-level facts. The source must be read.
"""


def _evaluate_source(
    node: QueryNode,
    source: SourceHandle,
    registry: FieldRegistry,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
) -> _Trilean:
    """Evaluate ``node`` against ``source`` using three-valued logic.

    Source-level field nodes evaluate to T or F. Record-level field
    nodes evaluate to U because we can't know the record outcome
    before reading the file. AND/OR/NOT combine per Kleene's
    semantics (see the module docstring).
    """
    if isinstance(node, TermNode):
        return "U"
    if isinstance(node, FieldExistsNode):
        spec = registry.get(node.field)
        if spec is None or spec.layer == "record":
            return "U"
        # mtime existence is unknown when the stat failed (mtime_ns<=0);
        # otherwise the source carries the field by construction.
        if spec.name == "mtime":
            return "U" if source.mtime_ns <= 0 else "T"
        return "T"
    if isinstance(node, FieldEqNode | FieldCmpNode | FieldRangeNode):
        spec = registry.get(node.field)
        if spec is None or spec.layer == "record":
            return "U"
        # mtime with unknown data (stat failed, mtime_ns=0) is "U" — we
        # don't KNOW the file's mtime, so we can't definitively exclude
        # it.  Returning "F" here would violate the three-valued contract
        # (F means "definitely false given known facts").
        if spec.name == "mtime" and source.mtime_ns <= 0:
            return "U"
        result = _field_matches_source(node, source, spec, path_patterns)
        return "T" if result else "F"
    if isinstance(node, NotNode):
        inner = _evaluate_source(node.child, source, registry, path_patterns)
        if inner == "T":
            return "F"
        if inner == "F":
            return "T"
        return "U"
    if isinstance(node, AndNode):
        states = [_evaluate_source(c, source, registry, path_patterns) for c in node.children]
        if "F" in states:
            return "F"
        if "U" in states:
            return "U"
        return "T"
    if isinstance(node, OrNode):
        states = [_evaluate_source(c, source, registry, path_patterns) for c in node.children]
        if "T" in states:
            return "T"
        if "U" in states:
            return "U"
        return "F"
    return "U"


def _evaluate_record(
    node: QueryNode,
    record: SearchRecord,
    registry: FieldRegistry,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
    origin_matchers: dict[tuple[str, str], OriginMatcher] | None = None,
    *,
    case_sensitive: bool = False,
) -> bool:
    """Evaluate ``node`` exactly against ``record``.

    Source-level fields are read from the record's source metadata
    (``record.agent``, ``record.store``, ``record.adapter_id``,
    ``record.path``). Record-level fields read from the record
    itself.
    """
    if isinstance(node, TermNode):
        return _text_matches(record, node.value, case_sensitive=case_sensitive)
    if isinstance(node, FieldExistsNode):
        return _field_exists_on_record(node.field, record)
    if isinstance(node, FieldEqNode):
        spec = registry.get(node.field)
        if spec is None:
            return False
        return _field_matches_record(
            node,
            record,
            spec,
            path_patterns,
            origin_matchers,
            case_sensitive=case_sensitive,
        )
    if isinstance(node, FieldCmpNode):
        spec = registry.get(node.field)
        if spec is None:
            return False
        return _field_cmp_matches_record(node, record, spec)
    if isinstance(node, FieldRangeNode):
        spec = registry.get(node.field)
        if spec is None:
            return False
        return _field_range_matches_record(node, record, spec)
    if isinstance(node, NotNode):
        return not _evaluate_record(
            node.child,
            record,
            registry,
            path_patterns,
            origin_matchers,
            case_sensitive=case_sensitive,
        )
    if isinstance(node, AndNode):
        return all(
            _evaluate_record(
                c,
                record,
                registry,
                path_patterns,
                origin_matchers,
                case_sensitive=case_sensitive,
            )
            for c in node.children
        )
    if isinstance(node, OrNode):
        return any(
            _evaluate_record(
                c,
                record,
                registry,
                path_patterns,
                origin_matchers,
                case_sensitive=case_sensitive,
            )
            for c in node.children
        )
    return False


def _field_exists_on_record(field: str, record: SearchRecord) -> bool:
    """Return whether ``field`` is present and non-empty on ``record``.

    Source-derived fields (``agent``/``store``/``adapter_id``/``mtime``) and
    the always-derivable ``scope`` are always present at the record layer: a
    record only reaches here from a source the ``source_predicate`` already
    admitted, and that layer owns the real ``mtime`` decision (the record
    carries no ``mtime_ns``). Nullable record fields count as absent when
    ``None`` or empty.
    """
    if field in ORIGIN_QUERY_FIELDS:
        return bool(record_origin_field_values(record, field))
    if field in {"agent", "store", "adapter_id", "mtime", "scope"}:
        return True
    if field == "path":
        return bool(str(record.path))
    if field == "model":
        return bool(record.model)
    if field == "role":
        return bool(record.role)
    if field == "timestamp":
        return bool(record.timestamp)
    if field == "text":
        return bool(record.text)
    return False


def _field_matches_source(
    node: FieldEqNode | FieldCmpNode | FieldRangeNode,
    source: SourceHandle,
    spec: FieldSpec,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
) -> bool:
    """Decide whether ``source`` matches a source-layer field predicate."""
    if spec.name == "agent":
        return _enum_eq(source.agent, _eq_value(node), spec)
    if spec.name == "store":
        return _string_match(source.store, _eq_value(node))
    if spec.name == "adapter_id":
        return _string_match(source.adapter_id, _eq_value(node))
    if spec.name == "path":
        return _path_match(str(source.path), _path_pattern_for(node, path_patterns))
    if spec.name == "mtime":
        return _date_predicate_matches(
            node,
            _mtime_as_datetime(source.mtime_ns),
        )
    return False


def _field_matches_record(
    node: FieldEqNode,
    record: SearchRecord,
    spec: FieldSpec,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
    origin_matchers: dict[tuple[str, str], OriginMatcher] | None = None,
    *,
    case_sensitive: bool = False,
) -> bool:
    """Decide whether ``record`` matches a record-layer FieldEqNode."""
    if spec.layer == "source":
        # Source-level fields can be read off the record too.
        return _field_matches_record_via_source(node, record, spec, path_patterns)
    if spec.name == "scope":
        return record_matches_scope(
            record,
            t.cast("SearchScope", node.value),
        )
    if spec.name == "timestamp":
        return _date_predicate_matches(
            node,
            _record_timestamp_as_datetime(record.timestamp),
        )
    if spec.name == "model":
        return record.model is not None and _string_match(record.model, node.value)
    if spec.name == "role":
        return record.role is not None and _string_match(record.role, node.value)
    if spec.name in ORIGIN_QUERY_FIELDS:
        matcher = _origin_matcher_for_node(node, spec, path_patterns, origin_matchers)
        return matcher.matches(record)
    if spec.name == "text":
        # A wildcard text value matches the record text only (anchored
        # glob); a plain value keeps the multi-surface substring match.
        if _is_wildcard(node.value):
            return _string_match(
                record.text,
                node.value,
                case_sensitive=case_sensitive,
            )
        return _text_matches(record, node.value, case_sensitive=case_sensitive)
    return False


def _origin_matcher_for_node(
    node: FieldEqNode,
    spec: FieldSpec,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
    origin_matchers: dict[tuple[str, str], OriginMatcher] | None,
) -> OriginMatcher:
    """Return the precompiled matcher for an origin field predicate."""
    matcher = origin_matchers.get((node.field, node.value)) if origin_matchers is not None else None
    if matcher is not None:
        return matcher
    if spec.name in ORIGIN_PATH_QUERY_FIELDS:
        pattern = _path_pattern_for(node, path_patterns)
        return OriginMatcher.from_field_value(
            spec.name,
            node.value,
            variants=pattern.variants,
            is_glob=pattern.is_glob,
        )
    return OriginMatcher.from_field_value(spec.name, node.value)


def _field_matches_record_via_source(
    node: FieldEqNode,
    record: SearchRecord,
    spec: FieldSpec,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
) -> bool:
    """Evaluate a source-layer field against record metadata.

    The record carries its own copies of agent / store / adapter_id
    / path, so we can answer source-layer predicates at the record
    level without re-fetching the :class:`SourceHandle`.
    """
    if spec.name == "agent":
        return record.agent == node.value
    if spec.name == "store":
        return _string_match(record.store, node.value)
    if spec.name == "adapter_id":
        return _string_match(record.adapter_id, node.value)
    if spec.name == "path":
        return _path_match(str(record.path), _path_pattern_for(node, path_patterns))
    return False


def _field_cmp_matches_record(
    node: FieldCmpNode,
    record: SearchRecord,
    spec: FieldSpec,
) -> bool:
    """Decide whether ``record`` satisfies a comparison predicate."""
    if not spec.supports_comparison:
        message = f"field {spec.name!r} does not support comparison operators"
        raise QueryCompileError(message)
    if spec.name == "timestamp":
        moment = _record_timestamp_as_datetime(record.timestamp)
        if moment is None:
            return False
        try:
            bound = parse_date_literal(node.value).value
        except DateParseError as exc:
            message = f"invalid date in {spec.name}:{node.op} predicate: {exc}"
            raise QueryCompileError(message) from exc
        return _compare(moment, node.op, bound)
    return False


def _field_range_matches_record(
    node: FieldRangeNode,
    record: SearchRecord,
    spec: FieldSpec,
) -> bool:
    """Decide whether ``record`` satisfies a range predicate."""
    if not spec.supports_range:
        message = f"field {spec.name!r} does not support range operators"
        raise QueryCompileError(message)
    if spec.name == "timestamp":
        moment = _record_timestamp_as_datetime(record.timestamp)
        if moment is None:
            return False
        return _range_match(moment, node)
    return False


def _date_predicate_matches(
    node: FieldEqNode | FieldCmpNode | FieldRangeNode,
    moment: dt.datetime | None,
) -> bool:
    """Dispatch a date predicate against a single known moment."""
    if moment is None:
        return False
    if isinstance(node, FieldEqNode):
        try:
            window = equality_range(node.value)
        except DateParseError:
            return False
        return _within_range(moment, window)
    if isinstance(node, FieldCmpNode):
        try:
            bound = parse_date_literal(node.value).value
        except DateParseError:
            return False
        return _compare(moment, node.op, bound)
    return _range_match(moment, node)


def _range_match(moment: dt.datetime, node: FieldRangeNode) -> bool:
    """Decide whether ``moment`` falls inside the range ``node`` describes."""
    try:
        lo = parse_range_bound(node.lo)
        hi = parse_range_bound(node.hi)
    except DateParseError:
        return False
    window = DateRange(
        lo=lo,
        hi=hi,
        inclusive_lo=node.inclusive_lo,
        inclusive_hi=node.inclusive_hi,
    )
    return _within_range(moment, window)


def _within_range(moment: dt.datetime, window: DateRange) -> bool:
    """Return whether ``moment`` falls inside ``window``."""
    if window.lo is not None:
        if window.inclusive_lo:
            if moment < window.lo:
                return False
        elif moment <= window.lo:
            return False
    if window.hi is not None:
        if window.inclusive_hi:
            if moment > window.hi:
                return False
        elif moment >= window.hi:
            return False
    return True


def _compare(
    moment: dt.datetime,
    op: t.Literal["gt", "lt", "gte", "lte"],
    bound: dt.datetime,
) -> bool:
    """Pure comparison between two datetimes."""
    if op == "gt":
        return moment > bound
    if op == "lt":
        return moment < bound
    if op == "gte":
        return moment >= bound
    return moment <= bound


def _enum_eq(actual: str, expected: str, spec: FieldSpec) -> bool:
    """Enum-membership check for fields with ``enum_values``.

    If the spec declares enum values, an unknown value at compile
    time raises QueryCompileError so users see typos. The actual
    comparison is case-sensitive ASCII (all current enum domains
    are lowercase ASCII).
    """
    if spec.enum_values and expected not in spec.enum_values:
        choices = ", ".join(spec.enum_values)
        message = f"invalid {spec.name} value {expected!r}; valid choices: {choices}"
        raise QueryCompileError(message)
    return actual == expected


def _mtime_as_datetime(mtime_ns: int) -> dt.datetime | None:
    """Convert a ``SourceHandle.mtime_ns`` into a UTC datetime."""
    if mtime_ns <= 0:
        return None
    return dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=dt.UTC)


def _record_timestamp_as_datetime(timestamp: str | None) -> dt.datetime | None:
    """Parse the record's stored timestamp string into a UTC datetime.

    Records store timestamps as strings (varies by adapter); we try
    a few common ISO shapes and return ``None`` on failure. Records
    without a parseable timestamp silently fail any date predicate.
    """
    if timestamp is None:
        return None
    try:
        moment = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


__all__ = (
    "_Trilean",
    "_compare",
    "_date_predicate_matches",
    "_enum_eq",
    "_evaluate_record",
    "_evaluate_source",
    "_field_cmp_matches_record",
    "_field_exists_on_record",
    "_field_matches_record",
    "_field_matches_record_via_source",
    "_field_matches_source",
    "_field_range_matches_record",
    "_mtime_as_datetime",
    "_range_match",
    "_record_timestamp_as_datetime",
    "_within_range",
)
