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

import collections.abc as cabc
import dataclasses
import re
import typing as t

from agentgrep.origin import (
    ORIGIN_PATH_QUERY_FIELDS,
    ORIGIN_QUERY_FIELDS,
    OriginMatcher,
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
from agentgrep.query.dates import DateParseError, parse_date_literal
from agentgrep.query.errors import QueryCompileError
from agentgrep.query.evaluate import _evaluate_record, _evaluate_source
from agentgrep.query.parser import QueryParseError, parse_query
from agentgrep.query.pathmatch import _compile_path_patterns, _CompiledPathPattern
from agentgrep.query.registry import FieldRegistry
from agentgrep.records import SearchQuery, SearchRecord, SearchScope, SourceHandle


@dataclasses.dataclass(slots=True, frozen=True)
class CompiledQuery:
    """Predicates plus text terms produced by :func:`compile_query`.

    ``source_predicate`` and ``record_predicate`` are ``None`` when
    the query is pure text — the engine routes through the legacy
    fast path in that case. ``text_terms`` is always populated so
    the rg prefilter and matches_text path see the right input.
    ``field_predicate`` retains the field-only portion when it can be
    separated exactly; ``field_filter_safe`` tells consumers whether
    applying it without the text terms preserves boolean semantics.
    """

    source_predicate: t.Callable[[SourceHandle], bool] | None
    record_predicate: t.Callable[[SearchRecord], bool] | None
    text_terms: tuple[str, ...]
    is_pure_text: bool
    field_predicate: t.Callable[[SearchRecord], bool] | None = None
    field_filter_safe: bool = False


def compile_query(
    ast: QueryNode,
    registry: FieldRegistry,
    *,
    case_sensitive: bool = False,
) -> CompiledQuery:
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
            field_filter_safe=True,
        )

    _validate_ast(ast, registry)
    text_terms = tuple(_collect_text_terms(ast))
    path_fields = frozenset(spec.name for spec in registry.specs if spec.kind == "path")
    path_patterns = _compile_path_patterns(ast, path_fields=path_fields)
    origin_matchers = _compile_origin_matchers(ast, registry, path_patterns)

    def source_predicate(source: SourceHandle) -> bool:
        return _evaluate_source(ast, source, registry, path_patterns) != "F"

    def record_predicate(record: SearchRecord) -> bool:
        return _evaluate_record(
            ast,
            record,
            registry,
            path_patterns,
            origin_matchers,
            case_sensitive=case_sensitive,
        )

    field_ast, field_filter_safe, _contains_text = _field_filter_ast(ast)
    field_predicate: t.Callable[[SearchRecord], bool] | None = None
    if field_filter_safe and field_ast is not None:
        field_path_patterns = _compile_path_patterns(field_ast, path_fields=path_fields)
        field_origin_matchers = _compile_origin_matchers(
            field_ast,
            registry,
            field_path_patterns,
        )

        def field_predicate(record: SearchRecord) -> bool:
            return _evaluate_record(
                field_ast,
                record,
                registry,
                field_path_patterns,
                field_origin_matchers,
                case_sensitive=case_sensitive,
            )

    return CompiledQuery(
        source_predicate=source_predicate,
        record_predicate=record_predicate,
        text_terms=text_terms,
        is_pure_text=False,
        field_predicate=field_predicate,
        field_filter_safe=field_filter_safe,
    )


def _field_filter_ast(node: QueryNode) -> tuple[QueryNode | None, bool, bool]:
    """Separate field predicates from line-level text matching.

    Returns the field-only AST, whether the split preserves boolean
    semantics, and whether the original subtree contained text matching.
    Conjunctions separate cleanly. A mixed field/text ``OR`` or a negated
    text expression does not, so callers can reject it instead of silently
    widening the record set.
    """
    if isinstance(node, TermNode) or (isinstance(node, FieldEqNode) and node.field == "text"):
        return None, True, True
    if isinstance(node, FieldEqNode | FieldCmpNode | FieldRangeNode | FieldExistsNode):
        return node, True, False
    if isinstance(node, NotNode):
        child, safe, contains_text = _field_filter_ast(node.child)
        if not safe or contains_text or child is None:
            return None, False, contains_text
        return NotNode(child=child), True, False
    children: list[QueryNode] = []
    contains_text = False
    for child in node.children:
        field_child, safe, child_contains_text = _field_filter_ast(child)
        if not safe:
            return None, False, contains_text or child_contains_text
        contains_text = contains_text or child_contains_text
        if field_child is not None:
            children.append(field_child)
    if isinstance(node, OrNode) and contains_text and children:
        return None, False, True
    if not children:
        return None, True, contains_text
    if len(children) == 1:
        return children[0], True, contains_text
    if isinstance(node, AndNode):
        return AndNode(children=tuple(children)), True, contains_text
    return OrNode(children=tuple(children)), True, contains_text


def _compile_origin_matchers(
    node: QueryNode,
    registry: FieldRegistry,
    path_patterns: dict[tuple[str, str], _CompiledPathPattern],
) -> dict[tuple[str, str], OriginMatcher]:
    """Return origin matchers keyed by the parsed field/value pair."""
    if isinstance(node, FieldEqNode):
        spec = registry.get(node.field)
        if spec is None or spec.name not in ORIGIN_QUERY_FIELDS:
            return {}
        pattern = path_patterns.get((node.field, node.value))
        if spec.name in ORIGIN_PATH_QUERY_FIELDS and pattern is not None:
            matcher = OriginMatcher.from_field_value(
                spec.name,
                node.value,
                variants=pattern.variants,
                is_glob=pattern.is_glob,
            )
        else:
            matcher = OriginMatcher.from_field_value(spec.name, node.value)
        return {(node.field, node.value): matcher}
    if isinstance(node, NotNode):
        return _compile_origin_matchers(node.child, registry, path_patterns)
    if isinstance(node, AndNode | OrNode):
        matchers: dict[tuple[str, str], OriginMatcher] = {}
        for child in node.children:
            matchers.update(_compile_origin_matchers(child, registry, path_patterns))
        return matchers
    return {}


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

    query: SearchQuery | None
    error: str | None


def build_query_from_input(
    text: str,
    base_query: SearchQuery,
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
        compiled = compile_query(ast, registry, case_sensitive=base_query.case_sensitive)
    except QueryCompileError as exc:
        return QueryBuildResult(query=None, error=str(exc))
    # A pure-text result (phrase, or parenthesized AND of terms) needs no
    # predicate; route the extracted terms through the fast path so the
    # search box stays as cacheable as a bare-term query.
    result_compiled = None if compiled.is_pure_text else compiled
    # A ``scope:`` predicate filters records, but the coarse discovery scope
    # decides which stores are opened at all. Widen discovery to "all" when
    scope = scope_widened_for_ast(ast, base_query.scope)
    return QueryBuildResult(
        query=_rebuild(
            base_query,
            terms=compiled.text_terms,
            compiled=result_compiled,
            scope=scope,
        ),
        error=None,
    )


def compose_query_ast(
    terms: cabc.Sequence[str],
    nodes: cabc.Sequence[QueryNode],
    registry: FieldRegistry,
) -> tuple[QueryNode, QueryNode | None]:
    """AND synthetic ``nodes`` with user terms compiled as the bare path would.

    Frontends inject generated predicates (origin filters) through this
    helper so user terms keep their no-filter semantics: terms carrying
    query syntax parse exactly as the bare path parses them, and plain
    terms become literal :class:`~agentgrep.query.ast.TermNode` children —
    a single token with spaces stays one substring term instead of being
    re-parsed as two.

    Parameters
    ----------
    terms : Sequence[str]
        User search terms, one argv/request element each.
    nodes : Sequence[QueryNode]
        Synthetic predicate nodes to AND with the user terms.
    registry : FieldRegistry
        Registry used for syntax detection and parsing.

    Returns
    -------
    tuple[QueryNode, QueryNode | None]
        The composed root, plus the parsed user AST when the terms
        carried query syntax (``None`` when every term stayed literal).

    Raises
    ------
    QueryParseError
        When the user terms carry syntax that fails to parse.
    """
    cleaned = tuple(term for term in terms if term.strip())
    children: list[QueryNode] = list(nodes)
    user_ast: QueryNode | None = None
    if any(_has_query_syntax(term.strip(), registry) for term in cleaned):
        user_ast = parse_query(" ".join(cleaned), registry)
        children.append(user_ast)
    else:
        children.extend(TermNode(value=term) for term in cleaned)
    if len(children) == 1:
        return children[0], user_ast
    return AndNode(children=tuple(children)), user_ast


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
    base: SearchQuery,
    *,
    terms: tuple[str, ...],
    compiled: CompiledQuery | None,
    scope: SearchScope | None = None,
) -> SearchQuery:
    """Clone ``base`` with new ``terms`` / ``compiled``; carry the rest forward.

    ``scope`` overrides the discovery scope when a ``scope:`` predicate
    widened it; ``None`` keeps ``base.scope``.
    """
    return SearchQuery(
        terms=terms,
        scope=base.scope if scope is None else scope,
        any_term=base.any_term,
        regex=base.regex,
        case_sensitive=base.case_sensitive,
        agents=base.agents,
        limit=base.limit,
        dedupe=base.dedupe,
        compiled=compiled,
        match_surface=base.match_surface,
        origin_filter=base.origin_filter,
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


def scope_widened_for_ast(ast: QueryNode | None, scope: SearchScope) -> SearchScope:
    """Return ``all`` when ``ast`` carries a ``scope:`` predicate.

    A ``scope:`` predicate filters records, so discovery must open both
    prompt and conversation stores for it to act — otherwise
    ``scope:conversations`` against a prompts-scoped query would open no
    conversation stores and match nothing.
    """
    if ast is not None and "scope" in fields_in_ast(ast):
        return "all"
    return scope


_FIND_BOOLEAN_TEXT_REASON = (
    "find cannot evaluate OR / NOT over text terms; use search or grep, "
    "or narrow with field predicates (agent:, path:, store:, mtime:)"
)


def find_unsupported_reason(
    node: QueryNode,
    registry: FieldRegistry,
    *,
    under_boolean: bool = False,
) -> str | None:
    """Return why ``find`` cannot faithfully evaluate ``node``, or ``None``.

    ``find`` enumerates sources: it honors the source-level predicate plus a
    flat text pattern against paths, but never reads records. So it cannot
    evaluate record-level field predicates (``scope``/``timestamp``/``model``/
    ``role``) or boolean (OR / NOT) composition over text terms — those would
    be silently dropped or flattened into a literal pattern. Such a query gets
    a reason string so the CLI can reject it instead of mis-searching.
    Everything ``find`` can honor — source-level predicates in any shape, plus
    bare conjoined text terms — returns ``None``.
    """
    if isinstance(node, TermNode):
        return _FIND_BOOLEAN_TEXT_REASON if under_boolean else None
    if isinstance(node, FieldEqNode | FieldCmpNode | FieldRangeNode | FieldExistsNode):
        spec = registry.get(node.field)
        if spec is None or spec.layer == "source":
            return None
        if spec.name == "text":
            return _FIND_BOOLEAN_TEXT_REASON if under_boolean else None
        return (
            f"the {spec.name}: field filters records, which find does not read; use search or grep"
        )
    if isinstance(node, NotNode):
        return find_unsupported_reason(node.child, registry, under_boolean=True)
    if isinstance(node, AndNode):
        for child in node.children:
            reason = find_unsupported_reason(child, registry, under_boolean=under_boolean)
            if reason is not None:
                return reason
        return None
    if isinstance(node, OrNode):
        for child in node.children:
            reason = find_unsupported_reason(child, registry, under_boolean=True)
            if reason is not None:
                return reason
        return None
    return None


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
