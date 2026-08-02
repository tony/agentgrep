r"""Shared "does this text carry query-language syntax" gate.

Every frontend that accepts query-language input (CLI positionals, the
TUI search box, the MCP ``search``/``find`` tools) has to answer the same
cheap yes/no question before it engages :func:`agentgrep.query.parse_query`:
does this chunk of user text look like it is using field-predicate syntax,
a boolean keyword, or a quoted phrase? Before agentgrep#153 that question was
answered by two independently hand-written implementations —
``agentgrep.cli.parser._query_syntax_present`` and
``agentgrep.query.compile._has_query_syntax`` — that were supposed to agree
but had no test forcing them to. This module holds the one shared
implementation both call sites use now.

agentgrep#153 named two defects. This module's design closes both without
reintroducing either:

1. **``kind:`` (and any future field) has to be reachable.** Registering a
   field in :func:`agentgrep.query.registry.default_registry` must be
   enough to make ``kind:prompt`` work everywhere — :func:`has_query_syntax`
   engages the parser for any *registered* field predicate.
2. **An unregistered predicate must not silently become a literal with no
   signal — but it also must not turn every colon-bearing literal a user
   might type into a hard parse error.** A blanket "any ``ident:`` shape
   engages the parser" rule (the shape this module's history briefly
   carried) makes ``agentgrep search 'Note: fix this'`` and
   ``agentgrep search 'C:\\Users\\foo'`` — plausible, real literal
   searches — fail with "unknown field 'Note'" / "unknown field 'C'".
   So :func:`has_query_syntax` does **not** engage the parser for an
   unregistered identifier; :func:`unregistered_field_predicates` finds the
   same shape separately, for a frontend to attach as a non-fatal
   diagnostic (a warning, a "did you mean") alongside the literal search
   that still runs — never as a reason to change what matched.

:func:`has_query_syntax` also still engages on a leading quote (an
intended phrase) and a standalone uppercase boolean keyword — the whole
input is then handed to :func:`agentgrep.query.parse_query`, so an
unregistered field predicate that appears *inside* a boolean expression
(``bogusfield:xyz OR ruff``) still hard-errors today, matching the
already-established behavior for a bare unregistered predicate accompanied
by an explicit boolean operator. Making that specific sub-expression case
degrade gracefully would mean teaching the recursive-descent parser to
recover mid-parse and substitute a literal term for just the offending
span — real parser-level surgery, out of scope for this fix. Tracked as a
known follow-up rather than silently left undocumented.

Constraints on this module:

- **No heavy imports.** :mod:`agentgrep.cli.parser` calls
  :func:`has_query_syntax` on the CLI's cold-start path — the gate runs on
  every ``search``/``grep``/``find`` invocation, including the common
  bare-term case, and must not import :mod:`agentgrep.query` to answer a
  yes/no question. That package's ``__init__`` eagerly imports the parser,
  compiler, date-math, and path-glob modules; measured with
  ``python -X importtime``, importing it costs roughly 17ms on top of an
  already-imported ``agentgrep`` package — a real fraction of the ~250ms
  ``agentgrep --help`` budget (AGENTS.md) for work a plain-term query never
  needs. This module only uses :mod:`re`, :mod:`dataclasses`, and
  :mod:`difflib` (stdlib), so importing it is free.
- **The known-field set here is a hand-maintained mirror, not the source
  of truth.** :data:`QUERYABLE_FIELD_NAMES` is what :func:`has_query_syntax`
  checks on the CLI's cold-start path; :mod:`agentgrep.query.compile`
  instead passes its own live, registry-derived field-name set (see
  ``agentgrep.query.compile._has_query_syntax``), which can never drift.
  ``test_cli_query_field_names_mirror_the_registry``
  (``tests/test_query_gate.py``) fails the build if
  :data:`QUERYABLE_FIELD_NAMES` drifts from
  :meth:`agentgrep.query.registry.FieldRegistry.known_names` (plus
  aliases). Unlike before, a drifted mirror here reproduces the exact
  silent-literal defect agentgrep#153 reports — for the CLI path, and only
  for the one freshly-registered field the mirror missed — so the guard
  test stays load-bearing, not cosmetic.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import difflib
import re

BOOLEAN_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})
"""Standalone uppercase keywords that engage the parser on their own.

Lowercase ``and``/``or``/``not`` stay literal search terms — the
tokenizer treats them as plain terms, so this gate must agree.
"""

QUERYABLE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "store",
        "adapter_id",
        "adapter",
        "path",
        "mtime",
        "scope",
        "kind",
        "timestamp",
        "date",
        "model",
        "role",
        "cwd",
        "repo",
        "worktree",
        "branch",
        "project",
        "cwd_hash",
        "text",
    },
)
"""Canonical field names + aliases, hand-mirrored from the query registry.

Kept in sync with :func:`agentgrep.query.registry.default_registry` by
``test_cli_query_field_names_mirror_the_registry``.
"""

_IDENT_COLON_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*):")
"""A bare identifier immediately followed by ``:``, not glued to a prior
identifier character. Matches the same shape
:func:`agentgrep.query.parser.tokenize` uses to decide ``ident:`` splits, so
this gate and the tokenizer never disagree about what counts as
field-predicate *shape*."""

_TOKEN_VALUE_RE = re.compile(r"\S*")
"""Everything up to the next whitespace, used to recover the full
``ident:value`` token's text for a diagnostic message."""


def has_query_syntax(
    text: str,
    *,
    known_field_names: frozenset[str] = QUERYABLE_FIELD_NAMES,
) -> bool:
    """Return whether ``text`` should engage :func:`agentgrep.query.parse_query`.

    Used identically by the CLI's cold-start positional scan
    (:mod:`agentgrep.cli.parser`) and the compiler's live registry-driven
    scan (:mod:`agentgrep.query.compile`).

    Parameters
    ----------
    text : str
        A chunk of user input: one CLI positional, or the whole search-box
        string. May contain multiple words.
    known_field_names : frozenset[str]
        Field names (plus aliases) that count as a real field predicate.
        Callers that already have a live
        :class:`~agentgrep.query.registry.FieldRegistry` should pass its
        real name set; the default is the hand-maintained
        :data:`QUERYABLE_FIELD_NAMES` mirror for callers that cannot afford
        to import the registry.

    Returns
    -------
    bool
        ``True`` when the parser should be engaged: a leading quote (an
        intended phrase), a standalone uppercase boolean keyword, or a
        *registered* field predicate. An unregistered ``ident:`` shape does
        not engage the parser on its own — see
        :func:`unregistered_field_predicates`.
    """
    if not text:
        return False
    if text[:1] in {'"', "'"}:
        return True
    if any(word in BOOLEAN_KEYWORDS for word in text.split()):
        return True
    return any(match.group(1) in known_field_names for match in _IDENT_COLON_RE.finditer(text))


@dataclasses.dataclass(slots=True, frozen=True)
class UnregisteredFieldToken:
    """One field-predicate-shaped token whose identifier is not registered.

    Attributes
    ----------
    token : str
        The full ``ident:value`` text as it appeared in the input, up to
        the next whitespace.
    field : str
        Just the identifier before the colon (``token``'s prefix).
    suggestion : str | None
        The closest registered field name, when one is a plausible typo
        target (:func:`difflib.get_close_matches`); ``None`` otherwise.
    """

    token: str
    field: str
    suggestion: str | None

    @property
    def message(self) -> str:
        """Human-readable diagnostic text, ready to print or surface as-is."""
        base = (
            f"{self.token!r} looks like a field predicate, but {self.field!r} is not "
            "a registered query field; searching for the literal text instead"
        )
        if self.suggestion is None:
            return base
        return f"{base} (did you mean {self.suggestion!r}?)"


UNREGISTERED_FIELD_PREDICATE_CODE = "unregistered_field_predicate"
"""Stable machine-readable diagnostic code for every :class:`UnregisteredFieldToken`.

One constant, not a per-instance field, since every instance of this
diagnostic shares the same code — CLI JSON/NDJSON output and the MCP
``search`` tool's response both attach it alongside a
:class:`UnregisteredFieldToken`'s other fields.
"""


def unregistered_field_predicates(
    text: str,
    *,
    known_field_names: frozenset[str] = QUERYABLE_FIELD_NAMES,
) -> tuple[UnregisteredFieldToken, ...]:
    r"""Find field-predicate-shaped tokens whose identifier is not registered.

    This never changes whether :func:`has_query_syntax` engages the
    parser — an unregistered predicate still runs as a literal substring
    search. It exists so a frontend can attach a non-fatal warning (and a
    "did you mean" suggestion) alongside that search, instead of leaving a
    typo'd field name silent.

    Two shapes are deliberately excluded, to keep the false-positive rate
    low on plausible non-predicate literals:

    - **``scheme://`` URIs** (``https://example.com``, ``git://host/repo``)
      — the identifier before the colon is a URL scheme, not a field-name
      typo, *unless* the identifier happens to be a registered field name
      itself (``agent://codex`` is then a deliberate, if unusual,
      predicate — and is already excluded here since it's registered).
    - **An identifier that is not all-lowercase** (``Note:``, ``TODO:``,
      ``C:\\Users``) — every registered field name is lowercase
      ``snake_case``, so a capitalized or mixed-case token in front of a
      colon is far more likely to be prose or a path than a typo'd field
      name.

    Parameters
    ----------
    text : str
        A chunk of user input, exactly as passed to :func:`has_query_syntax`.
    known_field_names : frozenset[str]
        Field names (plus aliases) to treat as registered.

    Returns
    -------
    tuple[UnregisteredFieldToken, ...]
        One entry per unregistered field-predicate-shaped token found, in
        the order they appear in ``text``. Empty when none are found.
    """
    found: list[UnregisteredFieldToken] = []
    for match in _IDENT_COLON_RE.finditer(text):
        ident = match.group(1)
        if ident in known_field_names:
            continue
        if not ident.islower():
            continue
        if text[match.end() : match.end() + 2] == "//":
            continue
        value_match = _TOKEN_VALUE_RE.match(text, match.end())
        value_end = value_match.end() if value_match else match.end()
        token = text[match.start() : value_end]
        close_matches = difflib.get_close_matches(ident, known_field_names, n=1)
        found.append(
            UnregisteredFieldToken(
                token=token,
                field=ident,
                suggestion=close_matches[0] if close_matches else None,
            ),
        )
    return tuple(found)


def unregistered_field_predicates_in(
    tokens: cabc.Sequence[str],
    *,
    known_field_names: frozenset[str] = QUERYABLE_FIELD_NAMES,
) -> tuple[UnregisteredFieldToken, ...]:
    """Scan every token in ``tokens`` for an unregistered field predicate.

    A thin wrapper over :func:`unregistered_field_predicates` for callers
    with more than one chunk of input — CLI positionals, or MCP request
    terms — that deduplicates by field name across (and within) tokens, in
    first-seen order, so a typo repeated across several terms is reported
    once. Both the CLI's cold-start scan and the MCP ``search`` tool call
    this instead of each keeping their own copy of the same loop.

    Parameters
    ----------
    tokens : collections.abc.Sequence[str]
        Raw text to scan — CLI argv positionals, or MCP request terms.
    known_field_names : frozenset[str]
        Field names (plus aliases) to treat as registered.

    Returns
    -------
    tuple[UnregisteredFieldToken, ...]
        One entry per distinct unregistered field name found, in
        first-seen order.
    """
    found: list[UnregisteredFieldToken] = []
    seen_fields: set[str] = set()
    for token in tokens:
        for entry in unregistered_field_predicates(token, known_field_names=known_field_names):
            if entry.field in seen_fields:
                continue
            seen_fields.add(entry.field)
            found.append(entry)
    return tuple(found)


__all__ = [
    "BOOLEAN_KEYWORDS",
    "QUERYABLE_FIELD_NAMES",
    "UNREGISTERED_FIELD_PREDICATE_CODE",
    "UnregisteredFieldToken",
    "has_query_syntax",
    "unregistered_field_predicates",
    "unregistered_field_predicates_in",
]
