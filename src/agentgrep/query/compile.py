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
import typing as t

from agentgrep._query_gate import has_query_syntax, unregistered_field_predicates
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
from agentgrep.query.dates import DateParseError, parse_date_literal
from agentgrep.query.errors import QueryCompileError
from agentgrep.query.evaluate import _evaluate_record, _evaluate_source
from agentgrep.query.parser import QueryParseError, parse_query
from agentgrep.query.pathmatch import _compile_path_patterns, _CompiledPathPattern
from agentgrep.query.registry import FieldRegistry
from agentgrep.records import (
    SearchEffort,
    SearchQuery,
    SearchRecord,
    SearchScope,
    SourceHandle,
)


@dataclasses.dataclass(slots=True, frozen=True)
class CompiledQuery:
    """Predicates plus text terms produced by :func:`compile_query`.

    ``source_predicate`` and ``record_predicate`` are ``None`` when
    the query is pure text — the engine routes through the legacy
    fast path in that case. ``text_terms`` is always populated so
    the rg prefilter and matches_text path see the right input.

    Attributes
    ----------
    source_predicate : t.Callable[[SourceHandle], bool] | None
        Conservative source-level filter, returning ``False`` only when the AST cannot
        match given source facts alone. ``None`` for a pure-text query, which prunes no
        sources at this layer.
    record_predicate : t.Callable[[SearchRecord], bool] | None
        Exact per-record filter evaluating the AST against a parsed record, including its
        AND/OR/NOT structure. ``None`` for a pure-text query.
    text_terms : tuple[str, ...]
        Bare terms collected from the AST in source order, plus the values of ``text:``
        predicates. Populated for field queries too, so the grep prefilter still has
        needles.
    routing_terms : tuple[str, ...]
        Positive bare/text-field terms safe to use as prompt-routing clues. Terms below
        a negation are omitted because absence is not positive conversation evidence.
    routing_predicate : t.Callable[[SearchRecord], bool] | None
        Conservative conversation-invariant metadata filter for prompt routing. It
        returns ``False`` only when known prompt metadata proves that the containing
        conversation cannot match.
    has_positive_routing_metadata : bool
        Whether a positive conversation-invariant metadata clause can initiate routing
        without a text clue.
    is_pure_text : bool
        Whether the query was only bare terms under AND. ``True`` implies both predicates
        are ``None``.
    """

    source_predicate: t.Callable[[SourceHandle], bool] | None
    record_predicate: t.Callable[[SearchRecord], bool] | None
    text_terms: tuple[str, ...]
    routing_terms: tuple[str, ...]
    routing_predicate: t.Callable[[SearchRecord], bool] | None
    has_positive_routing_metadata: bool
    is_pure_text: bool


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

    Also validates a conflicting inline ``depth:``/``effort:``
    directive (``depth:targeted effort:exhaustive foo``) at this same
    compile step, even though nothing here reads the extracted value —
    :func:`resolve_request_modifiers` is what actually resolves effort,
    called separately by each frontend after compiling. Without this,
    ``compile_query`` alone (the MCP ``validate_query`` tool's dry run)
    would report a conflicting query as valid, only for the same query
    to fail once a real search calls ``resolve_request_modifiers``.
    """
    if _is_pure_text(ast):
        terms = _collect_text_terms(ast)
        return CompiledQuery(
            source_predicate=None,
            record_predicate=None,
            text_terms=tuple(terms),
            routing_terms=tuple(terms),
            routing_predicate=None,
            has_positive_routing_metadata=False,
            is_pure_text=True,
        )

    _validate_ast(ast, registry)
    _ = _effort_directive(ast, registry)
    text_terms = tuple(_collect_text_terms(ast))
    routing_terms = tuple(_collect_positive_text_terms(ast))
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

    routing_predicate = None
    if _has_routing_metadata(ast, registry):

        def routing_predicate(record: SearchRecord) -> bool:
            return (
                _evaluate_routing_metadata(
                    ast,
                    record,
                    registry,
                    path_patterns,
                    origin_matchers,
                    case_sensitive=case_sensitive,
                )
                != "F"
            )

    return CompiledQuery(
        source_predicate=source_predicate,
        record_predicate=record_predicate,
        text_terms=text_terms,
        routing_terms=routing_terms,
        routing_predicate=routing_predicate,
        has_positive_routing_metadata=_has_positive_routing_metadata(ast, registry),
        is_pure_text=False,
    )


type _RoutingMetadataState = t.Literal["T", "F", "U"]
_ROUTING_INVARIANT_FIELDS = frozenset({"agent"}) | ORIGIN_QUERY_FIELDS


def _evaluate_routing_metadata(
    node: QueryNode,
    record: SearchRecord,
    registry: FieldRegistry,
    path_patterns: dict[tuple[str, str], _CompiledPathPattern],
    origin_matchers: dict[tuple[str, str], OriginMatcher],
    *,
    case_sensitive: bool,
) -> _RoutingMetadataState:
    """Conservatively evaluate conversation-invariant prompt metadata."""
    if isinstance(node, FieldExistsNode | FieldEqNode | FieldCmpNode | FieldRangeNode):
        spec = registry.get(node.field)
        if spec is None or spec.name not in _ROUTING_INVARIANT_FIELDS:
            return "U"
        if spec.name in ORIGIN_QUERY_FIELDS and not record_origin_field_values(
            record,
            spec.name,
        ):
            return "U"
        matched = _evaluate_record(
            node,
            record,
            registry,
            path_patterns,
            origin_matchers,
            case_sensitive=case_sensitive,
        )
        return "T" if matched else "F"
    if isinstance(node, NotNode):
        state = _evaluate_routing_metadata(
            node.child,
            record,
            registry,
            path_patterns,
            origin_matchers,
            case_sensitive=case_sensitive,
        )
        return "F" if state == "T" else "T" if state == "F" else "U"
    if isinstance(node, AndNode):
        states = tuple(
            _evaluate_routing_metadata(
                child,
                record,
                registry,
                path_patterns,
                origin_matchers,
                case_sensitive=case_sensitive,
            )
            for child in node.children
        )
        return "F" if "F" in states else "U" if "U" in states else "T"
    if isinstance(node, OrNode):
        states = tuple(
            _evaluate_routing_metadata(
                child,
                record,
                registry,
                path_patterns,
                origin_matchers,
                case_sensitive=case_sensitive,
            )
            for child in node.children
        )
        return "T" if "T" in states else "U" if "U" in states else "F"
    return "U"


def _has_routing_metadata(node: QueryNode, registry: FieldRegistry) -> bool:
    """Return whether ``node`` contains conversation-invariant metadata."""
    if isinstance(node, FieldExistsNode | FieldEqNode | FieldCmpNode | FieldRangeNode):
        spec = registry.get(node.field)
        return spec is not None and spec.name in _ROUTING_INVARIANT_FIELDS
    if isinstance(node, NotNode):
        return _has_routing_metadata(node.child, registry)
    if isinstance(node, AndNode | OrNode):
        return any(_has_routing_metadata(child, registry) for child in node.children)
    return False


def _has_positive_routing_metadata(
    node: QueryNode,
    registry: FieldRegistry,
    *,
    negated: bool = False,
) -> bool:
    """Return whether positive invariant metadata can initiate routing."""
    if isinstance(node, FieldExistsNode | FieldEqNode | FieldCmpNode | FieldRangeNode):
        spec = registry.get(node.field)
        return not negated and spec is not None and spec.name in _ROUTING_INVARIANT_FIELDS
    if isinstance(node, NotNode):
        return _has_positive_routing_metadata(
            node.child,
            registry,
            negated=not negated,
        )
    if isinstance(node, AndNode | OrNode):
        return any(
            _has_positive_routing_metadata(
                child,
                registry,
                negated=negated,
            )
            for child in node.children
        )
    return False


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


def _validate_ast(
    node: QueryNode,
    registry: FieldRegistry,
    *,
    under_boolean: bool = False,
) -> None:
    """Walk the AST and raise :class:`QueryCompileError` on any field-level error.

    Catches five classes of semantic error:

    - **unknown enum value**: ``agent:gpt4`` when ``gpt4`` isn't
      in the agent enum's ``enum_values``.
    - **unparseable date literal**: ``timestamp:>bogus`` or
      ``timestamp:[bogus TO 2026]`` against a date-kind field.
    - **comparison against non-comparable field**: e.g.
      ``agent:>codex`` (the agent enum doesn't support comparison).
    - **range against non-range field**: e.g.
      ``scope:[prompts TO conversations]``.
    - **a request-layer directive (``depth:``/``effort:``) negated or
      OR'd**: e.g. ``NOT depth:targeted`` or
      ``(depth:targeted OR foo)``. A request-layer predicate evaluates
      as vacuously true at both the source and record layer (see
      :mod:`agentgrep.query.evaluate`), so negating it would silently
      flip an entire AND chain to always-false, and OR-ing it would
      silently flip the whole OR to always-true — both directly
      contradict what the query looks like it does. ``under_boolean``
      tracks whether the current node is reachable through a ``NOT``
      or an ``OR`` branch; plain AND composition (the common
      ``depth:targeted foo`` case) never sets it.

    The walk is O(nodes) and runs once before the closures are built. It
    is the only place all five classes are guaranteed to raise — the
    closures themselves are not a reliable fallback. Re-checking is
    real but partial and field-specific, not systematic: a source-layer
    enum field's evaluation does independently re-verify membership
    (:func:`agentgrep.query.evaluate._enum_eq`, reached from ``agent:``'s
    own evaluation path), but :func:`~agentgrep.query.evaluate._date_predicate_matches`
    catches a malformed date literal and silently returns ``False`` rather
    than raising for *any* date-kind field, source-layer comparison/range
    dispatch has no unsupported-operator guard at all, and a request-layer
    field's vacuous-true short-circuit bypasses every one of these checks
    unconditionally — it never reaches ``_enum_eq``, the date dispatch, or
    the comparison/range dispatch in the first place. A direct caller
    (tests, library consumers) who reaches a closure without calling this
    function first should not assume they'll see the same errors at call
    time.
    """
    if isinstance(node, FieldExistsNode):
        # Field-exists is valid for any registered field; the parser
        # already rejected unknown field names.
        _reject_request_field_under_boolean(node.field, registry, under_boolean=under_boolean)
        return
    if isinstance(node, FieldEqNode):
        _validate_field_value(node.field, node.value, registry)
        _reject_request_field_under_boolean(node.field, registry, under_boolean=under_boolean)
        return
    if isinstance(node, FieldCmpNode):
        spec = registry.get(node.field)
        if spec is None:
            return
        if not spec.supports_comparison:
            message = f"field {spec.name!r} does not support comparison operators"
            raise QueryCompileError(message)
        _validate_field_value(node.field, node.value, registry)
        _reject_request_field_under_boolean(node.field, registry, under_boolean=under_boolean)
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
        _reject_request_field_under_boolean(node.field, registry, under_boolean=under_boolean)
        return
    if isinstance(node, NotNode):
        _validate_ast(node.child, registry, under_boolean=True)
        return
    if isinstance(node, AndNode):
        for child in node.children:
            _validate_ast(child, registry, under_boolean=under_boolean)
        return
    if isinstance(node, OrNode):
        for child in node.children:
            _validate_ast(child, registry, under_boolean=True)


def _reject_request_field_under_boolean(
    field: str,
    registry: FieldRegistry,
    *,
    under_boolean: bool,
) -> None:
    """Raise when a ``request``-layer field predicate sits under NOT/OR."""
    if not under_boolean:
        return
    spec = registry.get(field)
    if spec is not None and spec.layer == "request":
        message = (
            f"field {spec.name!r} is a request-wide directive and cannot be "
            "negated or combined with OR"
        )
        raise QueryCompileError(message)


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

    Attributes
    ----------
    query : SearchQuery | None
        Rebuilt query carrying the new terms and any compiled predicates. ``None`` when
        the input failed to parse or compile.
    error : str | None
        Message to show the user, taken from the parse or compile error. ``None`` on
        success.
    warning : str | None
        Non-fatal diagnostic on an otherwise successful build — a field-predicate-shaped
        token (e.g. ``kind:prompt`` before ``kind`` was registered) whose field isn't
        registered, which still runs as a literal substring search. ``None`` when nothing
        to warn about. Only ever set alongside a non-``None`` ``query``.
    """

    query: SearchQuery | None
    error: str | None
    warning: str | None = None


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

    Inherits ``scope``, ``effort``, ``any_term``, ``regex``,
    ``case_sensitive``, ``agents``, ``limit``, and ``dedupe`` from
    ``base_query`` so the search bar lives on top of the existing
    filter scope and read policy rather than resetting them.

    Returns a :class:`QueryBuildResult`. On parse/compile failure,
    the caller can surface ``result.error`` in a status line and
    keep the search box editable.
    """
    stripped = text.strip()
    if not stripped:
        return QueryBuildResult(
            query=_rebuild(
                base_query,
                terms=(),
                compiled=None,
                conversation_limit=base_query.conversation_limit,
            ),
            error=None,
        )
    if not _has_query_syntax(stripped, registry):
        terms = tuple(stripped.split())
        found = unregistered_field_predicates(
            stripped,
            known_field_names=_registry_field_names(registry),
        )
        return QueryBuildResult(
            query=_rebuild(
                base_query,
                terms=terms,
                compiled=None,
                conversation_limit=base_query.conversation_limit,
            ),
            error=None,
            warning=found[0].message if found else None,
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
    try:
        scope, effort = resolve_request_modifiers(
            ast,
            registry,
            base_scope=base_query.scope,
            base_effort=base_query.effort,
            base_scope_explicit=base_query.scope_provenance == "explicit",
        )
    except QueryCompileError as exc:
        return QueryBuildResult(query=None, error=str(exc))
    # resolve_request_modifiers only reconciles an *implicit* scope; a scope
    # stated on purpose (an explicit base scope or an inline scope: predicate)
    # can still contradict the directive. Returning an error here instead of
    # a query would fall into build_query_from_input's own parse/compile
    # error contract, which frontends other than the TUI's search box (whose
    # error-recovery path falls back to a bare-term split, not a surfaced
    # error) rely on to mean "this text didn't parse" — a semantically valid
    # but contradictory request is a different kind of failure. The TUI's
    # workflow layer (SearchWorkflow.on_query) already checks the resolved
    # SearchQuery for the targeted/prompts contradiction and rejects it
    # before dispatch without ever reaching this function's error path; it
    # gains the same symmetric check for prompt/non-prompts there.
    # A conversation_limit set for a prior targeted run has nothing to bound
    # once effort resolves away from targeted (e.g. an inline depth:exhaustive
    # directive on a /deep-launched query) — carrying it forward would build
    # a SearchQuery the engine also rejects (conversation_limit requires
    # targeted effort), just later and less legibly.
    conversation_limit = base_query.conversation_limit if effort == "targeted" else None
    return QueryBuildResult(
        query=_rebuild(
            base_query,
            terms=compiled.text_terms,
            compiled=result_compiled,
            scope=scope,
            effort=effort,
            conversation_limit=conversation_limit,
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


def _has_query_syntax(text: str, registry: FieldRegistry) -> bool:
    """Return whether ``text`` carries query-language syntax.

    Delegates to the shared gate (:func:`agentgrep._query_gate.has_query_syntax`,
    also used by the CLI's cold-start scan,
    :func:`agentgrep.cli.parser._query_syntax_present`) but passes the live
    ``registry``'s field names and aliases rather than that gate's
    hand-maintained mirror — this path has already paid to import
    :mod:`agentgrep.query`, so there is no reason to risk drift here. Engages
    on a *registered* field predicate, a standalone uppercase boolean
    keyword, or a leading quote. An unregistered field-shaped predicate does
    not engage the parser — see
    :func:`agentgrep._query_gate.unregistered_field_predicates`.

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
    return has_query_syntax(text, known_field_names=_registry_field_names(registry))


def _registry_field_names(registry: FieldRegistry) -> frozenset[str]:
    """Return every field name and alias ``registry`` knows, live (never a mirror)."""
    return frozenset(name for spec in registry.specs for name in (spec.name, *spec.aliases))


def _rebuild(
    base: SearchQuery,
    *,
    terms: tuple[str, ...],
    compiled: CompiledQuery | None,
    conversation_limit: int | None,
    scope: SearchScope | None = None,
    effort: SearchEffort | None = None,
) -> SearchQuery:
    """Clone ``base`` with new ``terms`` / ``compiled``; carry the rest forward.

    ``scope`` overrides the discovery scope when a ``scope:`` predicate
    changed it; ``None`` keeps ``base.scope``. ``effort`` similarly
    overrides the read policy when that scope needs transcript stores.
    ``conversation_limit`` has no sentinel default — unlike ``scope``/
    ``effort``, ``None`` is itself a valid override (a stale targeted-only
    bound has nothing to bound once effort resolves elsewhere), so every
    caller must state its own value explicitly.
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
        effort=base.effort if effort is None else effort,
        order=base.order,
        scope_provenance=base.scope_provenance,
        conversation_limit=conversation_limit,
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
    """Return the narrowest discovery scope that can satisfy ``ast``.

    A query without a ``scope:`` predicate keeps ``scope``. Otherwise,
    boolean scope predicates are reduced to the prompt/conversation
    record kinds they can admit. This keeps ``scope:prompts`` on prompt
    stores while still widening mixed expressions that may match either
    kind.
    """
    if ast is None or "scope" not in fields_in_ast(ast):
        return scope
    possible = _possible_record_scopes(ast)
    if possible == {"prompts"} or not possible:
        return "prompts"
    if possible == {"conversations"}:
        return "conversations"
    return "all"


_ALL_RECORD_SCOPES = frozenset(("prompts", "conversations"))


def _possible_record_scopes(node: QueryNode) -> frozenset[str]:
    """Return record kinds that may satisfy the scope clauses in ``node``."""
    return frozenset(
        record_scope
        for record_scope in _ALL_RECORD_SCOPES
        if _scope_truth(node, record_scope) != "F"
    )


def _scope_truth(
    node: QueryNode,
    record_scope: str,
) -> t.Literal["T", "F", "U"]:
    """Project ``node`` onto one record kind with conservative unknowns."""
    if isinstance(node, FieldEqNode) and node.field == "scope":
        return "T" if node.value in {record_scope, "all"} else "F"
    if isinstance(node, FieldExistsNode) and node.field == "scope":
        return "T"
    if isinstance(node, NotNode):
        child = _scope_truth(node.child, record_scope)
        if child == "T":
            return "F"
        if child == "F":
            return "T"
        return "U"
    if isinstance(node, AndNode):
        states = tuple(_scope_truth(child, record_scope) for child in node.children)
        if "F" in states:
            return "F"
        return "U" if "U" in states else "T"
    if isinstance(node, OrNode):
        states = tuple(_scope_truth(child, record_scope) for child in node.children)
        if "T" in states:
            return "T"
        return "U" if "U" in states else "F"
    return "U"


_DEPTH_VALUE_ALIASES: dict[str, SearchEffort] = {"deep": "targeted"}
"""Map a friendly ``depth:``/``effort:`` value onto its canonical ladder rung.

``deep`` mirrors the ``--deep`` flag and the ``/deep`` slash command's own
vocabulary; ``prompt``, ``targeted``, and ``exhaustive`` already match
:data:`~agentgrep.records.SearchEffort` and need no translation.
"""

_DEPTH_FIELD_NAME = "depth"
"""Canonical name of the one built-in ``layer="request"`` field.

``layer="request"`` is the general engine-owned category "no per-record/
per-source truth value, extract instead of evaluate"; ``depth`` is one
specific field in that category whose value happens to be a
:data:`~agentgrep.records.SearchEffort`. A custom :class:`FieldRegistry`
can register other request-layer fields for its own purposes (see
:mod:`agentgrep.query.registry`), so :func:`_effort_directive` must key off
this canonical name, not the broader layer, or it would misread an
unrelated request-layer field's value as an effort.
"""


def _effort_directive(
    node: QueryNode,
    registry: FieldRegistry,
    *,
    under_boolean: bool = False,
) -> SearchEffort | None:
    """Return the single inline ``depth:``/``effort:`` value in ``node``, or ``None``.

    Matches only the canonical ``depth`` field (see ``_DEPTH_FIELD_NAME``),
    not every ``layer="request"`` field — a custom :class:`FieldRegistry`
    can register other request-layer fields for unrelated purposes, and
    their values are not :data:`~agentgrep.records.SearchEffort` strings.

    Callers normally only reach a real occurrence of the field through plain
    AND composition — ``_validate_ast`` already rejects it under ``NOT``/
    ``OR`` during :func:`compile_query`. This function re-checks that same
    rule itself (via ``under_boolean``, threaded through ``NotNode``/
    ``OrNode``) rather than trusting that every caller of the public
    :func:`resolve_request_modifiers` already ran ``compile_query`` first, so
    it raises the same :class:`QueryCompileError` regardless of call order.

    Also raises when two ANDed clauses resolve to different effort values
    (``depth:targeted AND depth:exhaustive``); the ``deep`` synonym and its
    canonical ``targeted`` spelling count as the same value for this check.
    """
    if isinstance(node, FieldEqNode | FieldExistsNode):
        spec = registry.get(node.field)
        if spec is None or spec.name != _DEPTH_FIELD_NAME:
            return None
        _reject_request_field_under_boolean(node.field, registry, under_boolean=under_boolean)
        if isinstance(node, FieldExistsNode):
            return None
        return _DEPTH_VALUE_ALIASES.get(node.value, t.cast("SearchEffort", node.value))
    if isinstance(node, NotNode):
        return _effort_directive(node.child, registry, under_boolean=True)
    if isinstance(node, AndNode):
        found: set[SearchEffort] = set()
        for child in node.children:
            value = _effort_directive(child, registry, under_boolean=under_boolean)
            if value is not None:
                found.add(value)
        if len(found) > 1:
            message = "conflicting depth:/effort: directives in one query"
            raise QueryCompileError(message)
        return next(iter(found), None)
    if isinstance(node, OrNode):
        for child in node.children:
            _ = _effort_directive(child, registry, under_boolean=True)
        return None
    return None


def resolve_request_modifiers(
    ast: QueryNode | None,
    registry: FieldRegistry,
    *,
    base_scope: SearchScope,
    base_effort: SearchEffort | None,
    base_scope_explicit: bool = False,
) -> tuple[SearchScope, SearchEffort]:
    """Return the effective ``(scope, effort)`` after request-wide directives resolve.

    The single shared resolver behind every frontend's depth ladder: the
    TUI's :func:`build_query_from_input` and the CLI's ``search``/``grep``
    argument builders (:mod:`agentgrep.cli.parser`) all route through this
    instead of each keeping its own copy of the scope-widening-implies-deeper-
    reads ladder. Walks ``ast`` for an inline ``scope:`` predicate (see
    :func:`scope_widened_for_ast`) and an inline ``depth:``/``effort:``
    predicate (see :func:`_effort_directive`).

    An inline depth/effort directive always wins over ``base_effort`` — it is
    the one part of this ladder a user can now type explicitly instead of
    only reaching through ``--deep``/``--exhaustive`` or a structured MCP
    parameter. Without one, a ``base_effort`` already at ``"targeted"`` or
    ``"exhaustive"`` stays sticky (an earlier deep/exhaustive authorization
    survives a follow-up edit); otherwise effort escalates to
    ``"exhaustive"`` only when the resolved scope leaves ``"prompts"``.

    A ``targeted`` directive additionally widens an implicit ``"prompts"``
    scope to ``"all"``, and a ``prompt`` directive narrows an implicit
    broader scope back to ``"prompts"`` — mirroring how ``--deep`` alone
    (with no ``--scope``) already widens scope rather than erroring. Neither
    reconciliation applies when scope was stated on purpose: by the caller
    (``base_scope_explicit``) or by an inline ``scope:`` predicate in
    ``ast`` itself. A user who explicitly asked for a scope that contradicts
    the directive still gets that contradiction back in the returned pair,
    for the caller to reject — see the ``targeted``/``prompts`` and
    ``prompt``/non-``prompts`` combinations :func:`build_query_from_input`
    rejects outright, and the equivalent CLI/MCP checks
    (``_targeted_conversation_limit``, ``_normalize_request_depth``).

    Parameters
    ----------
    ast : QueryNode | None
        Parsed user query, or ``None`` for a bare-term/flag-only request.
    registry : FieldRegistry
        Registry used to resolve field aliases to canonical names.
    base_scope : SearchScope
        Scope before any inline ``scope:`` predicate widens it — usually
        derived from an explicit ``--scope``/``scope=`` selection or a
        legacy ``--deep``-style compatibility default.
    base_effort : SearchEffort | None
        Effort before any inline ``depth:``/``effort:`` directive overrides
        it. ``None`` means "no sticky effort yet" (the TUI's launch state);
        every CLI/MCP caller passes a concrete value instead.
    base_scope_explicit : bool
        Whether ``base_scope`` was explicitly chosen (an explicit
        ``--scope``/``scope=`` selection) rather than the implicit default.
        Blocks the ``targeted``-directive auto-widen described above so an
        explicit ``scope=prompts`` still contradicts ``depth:targeted``
        cleanly. Defaults to ``False`` (treat ``base_scope`` as the implicit
        default) for callers that have no such distinction to offer.

    Returns
    -------
    tuple[SearchScope, SearchEffort]
        The scope and effort the engine should read at.

    Raises
    ------
    QueryCompileError
        When ``ast`` carries conflicting depth/effort directives, or negates
        or OR-combines the field (see :func:`_effort_directive`). A prior
        :func:`compile_query` call already rejects the same NOT/OR shape via
        ``_validate_ast``, but this function enforces it independently too,
        so a caller reaching it without compiling first still fails closed.
    """
    scope = scope_widened_for_ast(ast, base_scope)
    directive = _effort_directive(ast, registry) if ast is not None else None
    if directive is not None:
        scope_stated_explicitly = base_scope_explicit or (
            ast is not None and "scope" in fields_in_ast(ast)
        )
        if not scope_stated_explicitly:
            if directive == "targeted" and scope == "prompts":
                scope = "all"
            elif directive == "prompt" and scope != "prompts":
                scope = "prompts"
        return scope, directive
    if base_effort in {"targeted", "exhaustive"}:
        return scope, base_effort
    return scope, ("exhaustive" if scope != "prompts" else "prompt")


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
        if spec.layer == "request":
            return (
                f"the {spec.name}: field selects a read policy, which find does not "
                "apply; use search or grep"
            )
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


def _collect_positive_text_terms(node: QueryNode) -> list[str]:
    """Collect text terms that are not beneath a negation.

    Targeted routing may relax boolean composition to an OR over positive
    clues, but a negative term can never establish which conversation should
    be opened.
    """
    if isinstance(node, TermNode):
        return [node.value]
    if isinstance(node, FieldEqNode) and node.field == "text":
        return [node.value]
    if isinstance(node, AndNode | OrNode):
        out: list[str] = []
        for child in node.children:
            out.extend(_collect_positive_text_terms(child))
        return out
    return []
