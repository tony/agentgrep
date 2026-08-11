"""Field registry for the agentgrep query language.

The registry maps user-typed field names (`agent`, `path`,
`timestamp`, …) to typed specs that describe:

- the **kind** of value the field accepts (string substring, enum
  membership, date, path glob);
- the **layer** at which the predicate evaluates (source-level for
  fields decidable from a :class:`agentgrep.SourceHandle` alone;
  record-level for fields that need a parsed
  :class:`agentgrep.SearchRecord`);
- whether **comparison** (``>``, ``<``, ``>=``, ``<=``) and
  **range** (`[a TO b]`) operators are supported.

The compiler in :mod:`agentgrep.query.compile` uses this metadata
to split a parsed AST into the source-level and record-level
predicates the engine consumes.
"""

from __future__ import annotations

import dataclasses
import typing as t

FieldKind = t.Literal["string", "enum", "date", "path"]
"""Type of value a field accepts.

- ``string`` — substring or regex match (depending on the field)
- ``enum`` — must be one of the registered :attr:`FieldSpec.enum_values`
- ``date`` — accepts ISO + relative date literals; supports
  comparison and range operators
- ``path`` — glob-by-default (basename); ``--full-path`` style
  toggles will be added later
"""

FieldLayer = t.Literal["source", "record", "request"]
"""Which engine layer the field's predicate filters at.

``source`` predicates prune ``SourceHandle`` candidates before
any file is opened. ``record`` predicates filter parsed
``SearchRecord`` instances after the engine reads each file.
``request`` fields carry no per-source or per-record truth value at
all — they are request-wide directives (e.g. ``depth:``/``effort:``)
that select a read policy rather than admitting or rejecting a
source or record. The compiler evaluates a ``request`` predicate as
vacuously true at both layers (see
:func:`agentgrep.query.evaluate._evaluate_source` /
:func:`agentgrep.query.evaluate._evaluate_record`) and instead
extracts its value through
:func:`agentgrep.query.compile.resolve_request_modifiers`.
"""


@dataclasses.dataclass(slots=True, frozen=True)
class FieldSpec:
    """Schema entry describing one queryable field.

    Attributes
    ----------
    name : str
        Canonical field name users type before the colon.
    kind : FieldKind
        Value type the field accepts: substring string, enum membership, date literal, or
        path glob.
    layer : FieldLayer
        Where the predicate evaluates: ``"source"`` prunes handles before a file is
        opened, ``"record"`` filters parsed records, ``"request"`` carries a
        request-wide directive that filters nothing and is instead extracted by
        :func:`agentgrep.query.compile.resolve_request_modifiers`.
    enum_values : tuple[str, ...]
        Values an ``"enum"`` field accepts; anything else is a compile error. Empty for
        every other kind.
    aliases : tuple[str, ...]
        Alternate names resolving to this spec, e.g. ``date`` for ``timestamp``. Empty
        when the field has none.
    supports_comparison : bool
        Whether ``>``, ``<``, ``>=``, and ``<=`` are accepted. Comparing a field that does
        not support them is a compile error.
    supports_range : bool
        Whether ``[lo TO hi]`` ranges are accepted. Ranging a field that does not support
        them is a compile error.
    """

    name: str
    kind: FieldKind
    layer: FieldLayer
    enum_values: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    supports_comparison: bool = False
    supports_range: bool = False


@dataclasses.dataclass(slots=True, frozen=True)
class FieldRegistry:
    """Lookup table mapping field name (and alias) → :class:`FieldSpec`.

    Construct via :func:`default_registry`. Callers can override or
    extend the default by passing their own ``FieldRegistry`` to
    :func:`agentgrep.query.parser.parse_query`.

    Registries are tiny (the default ships ~10 fields) so
    :meth:`get` does a linear scan rather than caching a dict — the
    cost is negligible and the type-checker friendliness wins.

    Attributes
    ----------
    specs : tuple[FieldSpec, ...]
        Registered field specs, in the order help output and lookups walk them. A name or
        alias repeated across two specs is rejected at construction.
    """

    specs: tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        """Reject duplicate canonical names or aliases at construction time."""
        seen: set[str] = set()
        for spec in self.specs:
            for name in (spec.name, *spec.aliases):
                if name in seen:
                    message = f"duplicate field registration for {name!r}"
                    raise ValueError(message)
                seen.add(name)

    def get(self, name: str) -> FieldSpec | None:
        """Return the spec for ``name``, honoring aliases. ``None`` if unknown."""
        for spec in self.specs:
            if spec.name == name or name in spec.aliases:
                return spec
        return None

    def known_names(self) -> tuple[str, ...]:
        """Return every registered field name (canonical only, no aliases)."""
        return tuple(spec.name for spec in self.specs)


def default_registry() -> FieldRegistry:
    """Construct the default registry agentgrep ships with.

    The current set is:

    ============= ====== ======= ===========================================
    Field         Kind   Layer   Notes
    ============= ====== ======= ===========================================
    ``agent``     enum   source  Values: codex, claude, cursor-cli, cursor-ide,
                                         gemini, antigravity-cli,
                                         antigravity-ide, grok, pi, opencode,
                                         vscode
    ``store``     string source  Substring against :attr:`SourceHandle.store`
    ``adapter``   string source  Alias of ``adapter_id``
    ``path``      path   source  Glob against the file basename by default
    ``mtime``     date   source  File mtime; supports comparison + range
    ``scope``     enum   record  Values: prompts, conversations, all
    ``kind``      enum   record  Values: prompt, history
    ``timestamp`` date   record  Record timestamp; comparison + range
    ``model``     string record  Substring against ``record.model``
    ``role``      string record  Substring against ``record.role``
    ``cwd``       path   record  Project working directory
    ``repo``      path   record  Project repository root
    ``worktree``  path   record  Git worktree root
    ``branch``    string record  Git branch name (whole value, casefolded)
    ``project``   string record  Project/workspace basename (whole value)
    ``cwd_hash``  string record  Opaque project hash (whole value)
    ``text``      string record  Implicit field for bare positional terms
    ``depth``     enum   request Values: prompt, targeted, deep (alias of
                                         targeted), exhaustive. Alias:
                                         ``effort``
    ============= ====== ======= ===========================================

    ``depth`` (alias ``effort``) is the only ``request``-layer field: it
    selects the engine's read-policy tier
    (:data:`~agentgrep.records.SearchEffort`) rather than admitting or
    rejecting any source or record.
    :func:`agentgrep.query.compile.resolve_request_modifiers` is the single
    place that reads it back out of a parsed query.
    """
    specs: tuple[FieldSpec, ...] = (
        FieldSpec(
            name="agent",
            kind="enum",
            layer="source",
            enum_values=(
                "codex",
                "claude",
                "cursor-cli",
                "cursor-ide",
                "gemini",
                "antigravity-cli",
                "antigravity-ide",
                "grok",
                "pi",
                "opencode",
                "vscode",
            ),
        ),
        FieldSpec(name="store", kind="string", layer="source"),
        FieldSpec(
            name="adapter_id",
            kind="string",
            layer="source",
            aliases=("adapter",),
        ),
        FieldSpec(name="path", kind="path", layer="source"),
        FieldSpec(
            name="mtime",
            kind="date",
            layer="source",
            supports_comparison=True,
            supports_range=True,
        ),
        FieldSpec(
            name="scope",
            kind="enum",
            layer="record",
            enum_values=("prompts", "conversations", "all"),
        ),
        FieldSpec(
            name="kind",
            kind="enum",
            layer="record",
            enum_values=("prompt", "history"),
        ),
        FieldSpec(
            name="timestamp",
            kind="date",
            layer="record",
            supports_comparison=True,
            supports_range=True,
            aliases=("date",),
        ),
        FieldSpec(name="model", kind="string", layer="record"),
        FieldSpec(name="role", kind="string", layer="record"),
        FieldSpec(name="cwd", kind="path", layer="record"),
        FieldSpec(name="repo", kind="path", layer="record"),
        FieldSpec(name="worktree", kind="path", layer="record"),
        FieldSpec(name="branch", kind="string", layer="record"),
        FieldSpec(name="project", kind="string", layer="record"),
        FieldSpec(name="cwd_hash", kind="string", layer="record"),
        FieldSpec(name="text", kind="string", layer="record"),
        FieldSpec(
            name="depth",
            kind="enum",
            layer="request",
            enum_values=("prompt", "targeted", "deep", "exhaustive"),
            aliases=("effort",),
        ),
        FieldSpec(
            name="human",
            kind="enum",
            layer="record",
            enum_values=("true", "false"),
        ),
    )
    return FieldRegistry(specs=specs)
