"""Compile a parsed query AST into predicate closures.

The compiler produces two callables:

- ``source_predicate(source)`` — conservative: returns ``False`` only
  when the AST is definitely-false given just source-level facts;
  ``True`` when it might still match (so the engine reads the
  source). Drives source pruning before any file is opened.
- ``record_predicate(record)`` — exact: returns the AST's actual
  truth value evaluated against a parsed record. Drives the
  per-record filter the engine runs after parsing.

The compiler also separates out the pure text terms so the existing
ripgrep prefilter and :func:`agentgrep.matches_text` paths still
see the same input they always did.

A bare positional query (e.g. ``"bliss"`` or ``"bliss codex"``)
short-circuits to :attr:`CompiledQuery.is_pure_text` ``= True`` and
both predicates are ``None``. The engine's existing code path runs
unchanged in that case, with no overhead from this module.

The source-side evaluation uses three-valued logic (T/F/Unknown)
so OR-mixed and NOT-mixed nodes degrade safely to "let the source
through, the record filter will decide". See the design doc at
``/home/d/.claude/plans/study-our-cli-commands-spicy-sky.md``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import os
import pathlib
import re
import typing as t

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
from agentgrep.query.dates import (
    DateParseError,
    DateRange,
    equality_range,
    parse_date_literal,
    parse_range_bound,
)
from agentgrep.query.parser import QueryParseError, parse_query
from agentgrep.query.registry import FieldRegistry, FieldSpec

_Trilean = t.Literal["T", "F", "U"]
"""Three-valued logic state used during conservative source eval.

- ``T`` — predicate is definitely satisfied by the source's known
  facts.
- ``F`` — predicate is definitely not satisfied. The source can be
  pruned without reading any records.
- ``U`` — depends on record-level facts. The source must be read.
"""


@dataclasses.dataclass(slots=True, frozen=True)
class CompiledQuery:
    """Predicates plus text terms produced by :func:`compile_query`.

    ``source_predicate`` and ``record_predicate`` are ``None`` when
    the query is pure text — the engine routes through the legacy
    fast path in that case. ``text_terms`` is always populated so
    the rg prefilter and matches_text path see the right input.
    """

    source_predicate: t.Callable[[agentgrep.SourceHandle], bool] | None
    record_predicate: t.Callable[[agentgrep.SearchRecord], bool] | None
    text_terms: tuple[str, ...]
    is_pure_text: bool


@dataclasses.dataclass(slots=True, frozen=True)
class _CompiledPathPattern:
    """Pre-expanded path predicate used by compiled query closures."""

    raw: str
    variants: tuple[str, ...]
    is_glob: bool


class QueryCompileError(ValueError):
    """Raised when a query AST can't be compiled.

    Distinct from :class:`agentgrep.query.parser.QueryParseError` —
    parse errors are syntactic, compile errors are semantic
    (e.g. comparing against a string field, range against an enum).
    """


def compile_query(ast: QueryNode, registry: FieldRegistry) -> CompiledQuery:
    """Compile an AST into a :class:`CompiledQuery`.

    Pure-text queries short-circuit to the fast path; everything
    else gets a source-level conservative predicate plus an exact
    record-level predicate.

    Field-level predicates are validated up-front so semantic
    errors (unknown enum value, unparseable date, comparison
    against a string field, range against an enum) raise
    :class:`QueryCompileError` before the closures are
    constructed. Without this walk the same errors would surface
    only when the closures were evaluated — and the eager search
    path's record-side closure dodges them entirely, so users see
    silent zero-match runs instead of clean errors.
    """
    if _is_pure_text(ast):
        terms = _collect_text_terms(ast)
        return CompiledQuery(
            source_predicate=None,
            record_predicate=None,
            text_terms=tuple(terms),
            is_pure_text=True,
        )

    _validate_ast(ast, registry)
    text_terms = tuple(_collect_text_terms(ast))
    path_patterns = _compile_path_patterns(ast)

    def source_predicate(source: agentgrep.SourceHandle) -> bool:
        return _evaluate_source(ast, source, registry, path_patterns) != "F"

    def record_predicate(record: agentgrep.SearchRecord) -> bool:
        return _evaluate_record(ast, record, registry, path_patterns)

    return CompiledQuery(
        source_predicate=source_predicate,
        record_predicate=record_predicate,
        text_terms=text_terms,
        is_pure_text=False,
    )


def _validate_ast(node: QueryNode, registry: FieldRegistry) -> None:
    """Walk the AST and raise :class:`QueryCompileError` on any field-level error.

    Catches the four classes of semantic error the closures would
    otherwise raise lazily during evaluation:

    - **unknown enum value**: ``agent:gpt4`` when ``gpt4`` isn't
      in the agent enum's ``enum_values``.
    - **unparseable date literal**: ``timestamp:>bogus`` or
      ``timestamp:[bogus TO 2026]`` against a date-kind field.
    - **comparison against non-comparable field**: e.g.
      ``agent:>codex`` (the agent enum doesn't support comparison).
    - **range against non-range field**: e.g.
      ``scope:[prompts TO conversations]``.

    The walk is O(nodes) and runs once before the closures are
    built; the closures themselves keep their defensive raises so
    direct callers (tests, library consumers) still see the same
    errors at call time.
    """
    if isinstance(node, FieldExistsNode):
        # Field-exists is valid for any registered field; the parser
        # already rejected unknown field names.
        return
    if isinstance(node, FieldEqNode):
        _validate_field_value(node.field, node.value, registry)
        return
    if isinstance(node, FieldCmpNode):
        spec = registry.get(node.field)
        if spec is None:
            return
        if not spec.supports_comparison:
            message = f"field {spec.name!r} does not support comparison operators"
            raise QueryCompileError(message)
        _validate_field_value(node.field, node.value, registry)
        return
    if isinstance(node, FieldRangeNode):
        spec = registry.get(node.field)
        if spec is None:
            return
        if not spec.supports_range:
            message = f"field {spec.name!r} does not support range operators"
            raise QueryCompileError(message)
        _validate_range_bound(node.field, node.lo, registry)
        _validate_range_bound(node.field, node.hi, registry)
        return
    if isinstance(node, NotNode):
        _validate_ast(node.child, registry)
        return
    if isinstance(node, AndNode | OrNode):
        for child in node.children:
            _validate_ast(child, registry)


def _validate_field_value(
    field: str,
    value: str,
    registry: FieldRegistry,
) -> None:
    """Validate one ``field:value`` predicate against its :class:`FieldSpec`.

    Enums: value must be in ``enum_values``. Dates: value must
    parse via :func:`parse_date_literal`. Strings, paths, and
    unknown fields pass through (unknown fields are caught at
    parse time so this branch is mostly defensive).
    """
    spec = registry.get(field)
    if spec is None:
        return
    if spec.kind == "enum" and spec.enum_values and value not in spec.enum_values:
        choices = ", ".join(spec.enum_values)
        message = f"invalid {spec.name} value {value!r}; valid choices: {choices}"
        raise QueryCompileError(message)
    if spec.kind == "date":
        try:
            _ = parse_date_literal(value)
        except DateParseError as exc:
            message = f"invalid date in {spec.name} predicate: {exc}"
            raise QueryCompileError(message) from exc


def _validate_range_bound(
    field: str,
    literal: str,
    registry: FieldRegistry,
) -> None:
    """Validate one bound of a ``field:[lo TO hi]`` predicate.

    Treats ``*`` as the legal unbounded marker (no parse needed).
    Everything else must parse via :func:`parse_date_literal` when
    the field is date-kind.
    """
    spec = registry.get(field)
    if spec is None or spec.kind != "date":
        return
    if literal.strip() == "*":
        return
    try:
        _ = parse_date_literal(literal)
    except DateParseError as exc:
        message = f"invalid date in {spec.name} range: {exc}"
        raise QueryCompileError(message) from exc


@dataclasses.dataclass(slots=True, frozen=True)
class QueryBuildResult:
    """Outcome of :func:`build_query_from_input`.

    Either ``query`` is a fresh :class:`agentgrep.SearchQuery` and
    ``error`` is ``None`` (success), or ``query`` is ``None`` and
    ``error`` carries a user-facing message (parse / compile failure).
    Frozen so consumers can pass the result across thread boundaries.
    """

    query: agentgrep.SearchQuery | None
    error: str | None


def build_query_from_input(
    text: str,
    base_query: agentgrep.SearchQuery,
    registry: FieldRegistry,
) -> QueryBuildResult:
    """Translate a search-input string into a fresh :class:`SearchQuery`.

    The TUI's search box uses this on every debounced change. The
    helper bridges three input shapes:

    - **Empty / whitespace-only**: returns an empty-terms query.
    - **Bare terms** (no ``:``): split on whitespace; legacy path.
    - **Field syntax** (`:` present): parse + compile, route the
      compiled query through ``SearchQuery.compiled`` so source and
      record predicates apply on the next search.

    Inherits ``scope``, ``any_term``, ``regex``,
    ``case_sensitive``, ``agents``, ``limit``, and ``dedupe`` from
    ``base_query`` so the search bar lives on top of the existing
    filter scope rather than resetting it.

    Returns a :class:`QueryBuildResult`. On parse/compile failure,
    the caller can surface ``result.error`` in a status line and
    keep the search box editable.
    """
    stripped = text.strip()
    if not stripped:
        return QueryBuildResult(
            query=_rebuild(base_query, terms=(), compiled=None),
            error=None,
        )
    if not _has_query_syntax(stripped, registry):
        terms = tuple(stripped.split())
        return QueryBuildResult(
            query=_rebuild(base_query, terms=terms, compiled=None),
            error=None,
        )
    try:
        ast = parse_query(stripped, registry)
    except QueryParseError as exc:
        return QueryBuildResult(query=None, error=str(exc))
    try:
        compiled = compile_query(ast, registry)
    except QueryCompileError as exc:
        return QueryBuildResult(query=None, error=str(exc))
    # A pure-text result (phrase, or parenthesized AND of terms) needs no
    # predicate; route the extracted terms through the fast path so the
    # search box stays as cacheable as a bare-term query.
    result_compiled = None if compiled.is_pure_text else compiled
    return QueryBuildResult(
        query=_rebuild(
            base_query,
            terms=compiled.text_terms,
            compiled=result_compiled,
        ),
        error=None,
    )


_BOOLEAN_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})
_IDENT_COLON_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*):")


def _has_query_syntax(text: str, registry: FieldRegistry) -> bool:
    """Return whether ``text`` carries query-language syntax.

    Mirrors the CLI gate (:func:`agentgrep.cli.parser._query_syntax_present`)
    but derives the queryable field names from ``registry`` rather than a
    hardcoded mirror — the query module is already imported on this path,
    so there is no cold-start cost. Engages on a known field predicate, a
    standalone uppercase boolean keyword, or a leading quote.

    Parameters
    ----------
    text : str
        The (already stripped) search-box input.
    registry : FieldRegistry
        Registry whose field names and aliases count as predicates.

    Returns
    -------
    bool
        ``True`` when the parser should be engaged.
    """
    if not text:
        return False
    if text[:1] in {'"', "'"}:
        return True
    if any(word in _BOOLEAN_KEYWORDS for word in text.split()):
        return True
    field_names = {name for spec in registry.specs for name in (spec.name, *spec.aliases)}
    return any(match.group(1) in field_names for match in _IDENT_COLON_RE.finditer(text))


def _rebuild(
    base: agentgrep.SearchQuery,
    *,
    terms: tuple[str, ...],
    compiled: CompiledQuery | None,
) -> agentgrep.SearchQuery:
    """Clone ``base`` with new ``terms`` / ``compiled``; carry the rest forward."""
    return agentgrep.SearchQuery(
        terms=terms,
        scope=base.scope,
        any_term=base.any_term,
        regex=base.regex,
        case_sensitive=base.case_sensitive,
        agents=base.agents,
        limit=base.limit,
        dedupe=base.dedupe,
        compiled=compiled,
        match_surface=base.match_surface,
    )


def fields_in_ast(node: QueryNode) -> set[str]:
    """Return the set of field names referenced anywhere in ``node``.

    Used by the CLI layer to detect collisions between
    ``--agent``-style flags and ``agent:`` query syntax: if the
    user sets both, parse-time error rather than silently
    intersect or override. Bare positional terms don't appear in
    the result (they have no field name).
    """
    if isinstance(node, FieldEqNode | FieldCmpNode | FieldRangeNode | FieldExistsNode):
        return {node.field}
    if isinstance(node, NotNode):
        return fields_in_ast(node.child)
    if isinstance(node, AndNode | OrNode):
        result: set[str] = set()
        for child in node.children:
            result |= fields_in_ast(child)
        return result
    return set()


def _is_pure_text(node: QueryNode) -> bool:
    """Return whether ``node`` contains only bare TermNodes under AND.

    A pure-text query has no field predicates, no OR, no NOT — just
    one term or an implicit-AND chain of terms.
    """
    if isinstance(node, TermNode):
        return True
    if isinstance(node, AndNode):
        return all(_is_pure_text(child) for child in node.children)
    return False


def _collect_text_terms(node: QueryNode) -> list[str]:
    """Walk the AST collecting every bare ``TermNode`` value in order.

    Includes terms nested under AND/OR/NOT (the rg prefilter benefits
    from knowing all terms even when boolean composition won't push
    cleanly). Field-equality nodes against the ``text`` field also
    contribute their value.
    """
    if isinstance(node, TermNode):
        return [node.value]
    if isinstance(node, FieldEqNode) and node.field == "text":
        return [node.value]
    if isinstance(node, AndNode | OrNode):
        out: list[str] = []
        for child in node.children:
            out.extend(_collect_text_terms(child))
        return out
    if isinstance(node, NotNode):
        return _collect_text_terms(node.child)
    return []


def _compile_path_patterns(node: QueryNode) -> dict[str, _CompiledPathPattern]:
    """Return pre-expanded path patterns keyed by their raw query value."""
    if isinstance(node, FieldEqNode) and node.field == "path":
        return {node.value: _compile_path_pattern(node.value)}
    if isinstance(node, NotNode):
        return _compile_path_patterns(node.child)
    if isinstance(node, AndNode | OrNode):
        patterns: dict[str, _CompiledPathPattern] = {}
        for child in node.children:
            patterns.update(_compile_path_patterns(child))
        return patterns
    return {}


def _compile_path_pattern(raw: str) -> _CompiledPathPattern:
    """Compile one ``path:`` value into raw and home-expanded variants."""
    variants = [raw]
    variants.extend(_expand_current_user_home_patterns(raw))
    unique_variants = _dedupe_preserving_order(variants)
    return _CompiledPathPattern(
        raw=raw,
        variants=unique_variants,
        is_glob=any(ch in variant for variant in unique_variants for ch in "*?["),
    )


def _expand_current_user_home_patterns(raw: str) -> tuple[str, ...]:
    """Expand current-user ``~`` and home-rooted (``~/`` or platform sep) path prefixes."""
    home = str(pathlib.Path.home())
    if raw == "~":
        child_patterns = [
            (home if home.endswith(separator) else home + separator) + "*"
            for separator in _path_separators()
        ]
        return _dedupe_preserving_order([home, *child_patterns])
    if raw.startswith("~/"):
        return (home + raw[1:],)
    if os.sep != "/" and raw.startswith(f"~{os.sep}"):
        return (home + raw[1:],)
    if os.altsep is not None and raw.startswith(f"~{os.altsep}"):
        return (home + raw[1:],)
    return ()


def _path_separators() -> tuple[str, ...]:
    """Return filesystem separators that may appear in local path strings."""
    separators = [os.sep]
    if os.altsep is not None:
        separators.append(os.altsep)
    return _dedupe_preserving_order(separators)


def _dedupe_preserving_order(values: t.Iterable[str]) -> tuple[str, ...]:
    """Return unique values while preserving first-seen order."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return tuple(unique)


def _evaluate_source(
    node: QueryNode,
    source: agentgrep.SourceHandle,
    registry: FieldRegistry,
    path_patterns: dict[str, _CompiledPathPattern],
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
    record: agentgrep.SearchRecord,
    registry: FieldRegistry,
    path_patterns: dict[str, _CompiledPathPattern],
) -> bool:
    """Evaluate ``node`` exactly against ``record``.

    Source-level fields are read from the record's source metadata
    (``record.agent``, ``record.store``, ``record.adapter_id``,
    ``record.path``). Record-level fields read from the record
    itself.
    """
    if isinstance(node, TermNode):
        return _text_matches(record, node.value)
    if isinstance(node, FieldExistsNode):
        return _field_exists_on_record(node.field, record)
    if isinstance(node, FieldEqNode):
        spec = registry.get(node.field)
        if spec is None:
            return False
        return _field_matches_record(node, record, spec, path_patterns)
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
        return not _evaluate_record(node.child, record, registry, path_patterns)
    if isinstance(node, AndNode):
        return all(_evaluate_record(c, record, registry, path_patterns) for c in node.children)
    if isinstance(node, OrNode):
        return any(_evaluate_record(c, record, registry, path_patterns) for c in node.children)
    return False


def _field_exists_on_record(field: str, record: agentgrep.SearchRecord) -> bool:
    """Return whether ``field`` is present and non-empty on ``record``.

    Source-derived identity fields (``agent``/``store``/``adapter_id``)
    and the always-derivable ``scope`` are always present. Nullable
    record fields count as absent when ``None`` or empty.
    """
    if field in {"agent", "store", "adapter_id", "scope"}:
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
    source: agentgrep.SourceHandle,
    spec: FieldSpec,
    path_patterns: dict[str, _CompiledPathPattern],
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
    record: agentgrep.SearchRecord,
    spec: FieldSpec,
    path_patterns: dict[str, _CompiledPathPattern],
) -> bool:
    """Decide whether ``record`` matches a record-layer FieldEqNode."""
    if spec.layer == "source":
        # Source-level fields can be read off the record too.
        return _field_matches_record_via_source(node, record, spec, path_patterns)
    if spec.name == "scope":
        return agentgrep.record_matches_scope(
            record,
            t.cast("agentgrep.SearchScope", node.value),
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
    if spec.name == "text":
        # A wildcard text value matches the record text only (anchored
        # glob); a plain value keeps the multi-surface substring match.
        if _is_wildcard(node.value):
            return _string_match(record.text, node.value)
        return _text_matches(record, node.value)
    return False


def _field_matches_record_via_source(
    node: FieldEqNode,
    record: agentgrep.SearchRecord,
    spec: FieldSpec,
    path_patterns: dict[str, _CompiledPathPattern],
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
    record: agentgrep.SearchRecord,
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
    record: agentgrep.SearchRecord,
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


def _is_wildcard(value: str) -> bool:
    """Return whether a string-field value carries a glob wildcard.

    Only ``*`` and ``?`` count; ``[...]`` classes stay path-only so a
    literal ``model:gpt[4]`` is not surprisingly reinterpreted.
    """
    return "*" in value or "?" in value


def _string_match(haystack: str, needle: str) -> bool:
    """Case-insensitive match for text/string fields.

    A wildcard value (``*`` / ``?``) matches by anchored glob — ``gpt*``
    means "starts with gpt"; users wanting substring write ``*gpt*``.
    A plain value keeps the historical casefolded substring match.
    ``fnmatchcase`` on pre-casefolded inputs keeps the result identical
    across platforms (``fnmatch`` would apply OS-specific normcase).
    """
    if _is_wildcard(needle):
        return fnmatch.fnmatchcase(haystack.casefold(), needle.casefold())
    return needle.casefold() in haystack.casefold()


def _path_pattern_for(
    node: FieldEqNode | FieldCmpNode | FieldRangeNode,
    path_patterns: dict[str, _CompiledPathPattern],
) -> _CompiledPathPattern:
    """Return the precompiled path pattern for a path predicate node."""
    raw = _eq_value(node)
    compiled = path_patterns.get(raw)
    if compiled is not None:
        return compiled
    return _compile_path_pattern(raw)


def _path_match(path: str, pattern: _CompiledPathPattern) -> bool:
    """Match a path against a pattern; substring fallback for non-glob input.

    Globs (`*`, `?`, `[...]`) in any compiled variant — including the
    home-expanded forms of `path:~` and `path:~/...` — trigger fnmatch;
    patterns whose variants stay glob-free fall through to substring
    containment so users can write `path:codex` without typing a
    leading `*`.
    """
    if pattern.is_glob:
        return any(fnmatch.fnmatchcase(path, variant) for variant in pattern.variants)
    return any(variant in path for variant in pattern.variants)


def _text_matches(record: agentgrep.SearchRecord, needle: str) -> bool:
    """Case-insensitive substring match against the record's text fields.

    Checks text, title, role, model, and path — the same fields that
    :func:`agentgrep.build_search_haystack` concatenates for the
    legacy :func:`agentgrep.matches_text` path. Keeping the surfaces
    aligned prevents a combined field+text query (``agent:codex bliss``)
    from silently dropping records where the text term appears only in
    ``model`` or ``path``.
    """
    needle_cf = needle.casefold()
    if needle_cf in record.text.casefold():
        return True
    if record.title is not None and needle_cf in record.title.casefold():
        return True
    if record.role is not None and needle_cf in record.role.casefold():
        return True
    if record.model is not None and needle_cf in record.model.casefold():
        return True
    return needle_cf in str(record.path).casefold()


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


def _eq_value(
    node: FieldEqNode | FieldCmpNode | FieldRangeNode,
) -> str:
    """Extract the raw value text for an equality-style predicate.

    The source-side matcher only needs the value (comparison and
    range nodes shouldn't reach here unless the field supports
    them; the date path handles those directly).
    """
    if isinstance(node, FieldEqNode):
        return node.value
    if isinstance(node, FieldCmpNode):
        return node.value
    return node.lo
