"""Helpers for project-origin metadata.

Project origin is optional record metadata. It is deliberately kept out of the
plain text haystack so origin filters do not change ordinary search relevance
or source prefilter behavior.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import pathlib
import typing as t

from agentgrep.records import RecordOrigin, SearchRecord, SourceOriginSummary

if t.TYPE_CHECKING:
    from agentgrep.query.ast import FieldEqNode

__all__ = [
    "LEGACY_ORIGIN_METADATA_KEYS",
    "ORIGIN_PATH_QUERY_FIELDS",
    "ORIGIN_QUERY_FIELDS",
    "ORIGIN_STRING_QUERY_FIELDS",
    "_origin_path_boundary_text",
    "_path_is_equal_or_descendant",
    "is_path_like_text",
    "normalize_origin_path_text",
    "origin_filter_nodes",
    "record_matches_origin",
    "record_origin_field_values",
]

LEGACY_ORIGIN_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "branch",
        "cwd",
        "cwd_hash",
        "directory",
        "gitBranch",
        "project",
        "project_hash",
        "projectHash",
        "repo",
        "repository",
        "workspace",
        "worktree",
    },
)

_PATH_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "cwd": ("cwd", "project", "workspace", "directory"),
    "repo": ("repo", "repository", "worktree", "cwd", "workspace", "directory"),
    "worktree": ("worktree", "workspace", "directory"),
}
_STRING_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "branch": ("branch", "gitBranch"),
    "cwd_hash": ("cwd_hash", "project_hash", "projectHash"),
}

# Query-field names backed by record origin. query.evaluate dispatches on
# these; a registered origin field missing here would silently never match.
ORIGIN_PATH_QUERY_FIELDS: frozenset[str] = frozenset(_PATH_FIELD_KEYS)
ORIGIN_STRING_QUERY_FIELDS: frozenset[str] = frozenset(_STRING_FIELD_KEYS) | {"project"}
ORIGIN_QUERY_FIELDS: frozenset[str] = ORIGIN_PATH_QUERY_FIELDS | ORIGIN_STRING_QUERY_FIELDS

_OriginMatchKind = t.Literal["path", "string"]
_OriginSummaryState = t.Literal["T", "F", "U"]

_CONTEXT_FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "branch": ("branch",),
    "cwd": ("cwd",),
    "repo": ("repo", "worktree", "cwd"),
    "worktree": ("worktree", "cwd"),
    "cwd_hash": ("cwd_hash",),
}


@dataclasses.dataclass(slots=True, frozen=True)
class OriginPredicate:
    """One compiled origin predicate against a record's origin fields."""

    fields: tuple[str, ...]
    value: str
    kind: _OriginMatchKind
    variants: tuple[str, ...]
    is_glob: bool

    @classmethod
    def from_field_value(
        cls,
        field: str,
        value: str,
        *,
        fields: tuple[str, ...] | None = None,
        variants: t.Iterable[str] | None = None,
        is_glob: bool | None = None,
    ) -> OriginPredicate | None:
        """Compile one origin field/value predicate."""
        if field in ORIGIN_PATH_QUERY_FIELDS:
            compiled_variants = tuple(variants) if variants is not None else (value,)
            return cls(
                fields=(field,) if fields is None else fields,
                value=value,
                kind="path",
                variants=compiled_variants,
                is_glob=_origin_path_is_glob(compiled_variants) if is_glob is None else is_glob,
            )
        if field in ORIGIN_STRING_QUERY_FIELDS:
            return cls(
                fields=(field,) if fields is None else fields,
                value=value,
                kind="string",
                variants=(value,),
                is_glob=_origin_string_is_glob(value) if is_glob is None else is_glob,
            )
        return None

    def matches(self, record: SearchRecord) -> bool:
        """Return whether ``record`` satisfies this origin predicate."""
        values = _dedupe(
            value for field in self.fields for value in record_origin_field_values(record, field)
        )
        if self.kind == "path":
            return any(self._path_matches(value) for value in values)
        return any(
            _origin_string_equal(value, self.value, is_glob=self.is_glob) for value in values
        )

    def evaluate_summary(self, summary: SourceOriginSummary | None) -> _OriginSummaryState:
        """Evaluate this predicate against complete source-origin facts."""
        if summary is None or any(field not in summary.complete_fields for field in self.fields):
            return "U"
        if not summary.origins:
            return "F"
        matches = [self._matches_origin(origin) for origin in summary.origins]
        if all(matches):
            return "T"
        if any(matches):
            return "U"
        return "F"

    def _matches_origin(self, origin: RecordOrigin) -> bool:
        values = _dedupe(
            value for field in self.fields for value in origin_field_values(origin, field)
        )
        if self.kind == "path":
            return any(self._path_matches(value) for value in values)
        return any(
            _origin_string_equal(value, self.value, is_glob=self.is_glob) for value in values
        )

    def _path_matches(self, value: str) -> bool:
        if self.is_glob:
            return any(fnmatch.fnmatchcase(value, variant) for variant in self.variants)
        path = _origin_path_boundary_text(value)
        return any(
            _path_is_equal_or_descendant(path, _origin_path_boundary_text(variant))
            for variant in self.variants
        )


@dataclasses.dataclass(slots=True, frozen=True)
class OriginMatcher:
    """Compiled matcher for origin fields and origin-context boosts."""

    predicates: tuple[OriginPredicate, ...]

    @classmethod
    def from_field_value(
        cls,
        field: str,
        value: str,
        *,
        variants: t.Iterable[str] | None = None,
        is_glob: bool | None = None,
    ) -> OriginMatcher:
        """Compile one origin query field/value predicate."""
        predicate = OriginPredicate.from_field_value(
            field,
            value,
            variants=variants,
            is_glob=is_glob,
        )
        return cls(()) if predicate is None else cls((predicate,))

    @classmethod
    def from_origin(cls, origin: RecordOrigin | None) -> OriginMatcher:
        """Compile a :class:`RecordOrigin` into same-context predicates."""
        if origin is None or origin.is_empty():
            return cls(())
        predicates: list[OriginPredicate] = []
        for field in ("branch", "cwd", "repo", "worktree", "cwd_hash"):
            value = t.cast("str | None", getattr(origin, field))
            if not value:
                continue
            predicate = OriginPredicate.from_field_value(
                field,
                value,
                fields=_CONTEXT_FIELD_VALUES[field],
            )
            if predicate is not None:
                predicates.append(predicate)
        return cls(tuple(predicates))

    def matches(self, record: SearchRecord) -> bool:
        """Return whether ``record`` satisfies every compiled predicate."""
        return bool(self.predicates) and all(
            predicate.matches(record) for predicate in self.predicates
        )

    def may_match_summary(self, summary: SourceOriginSummary | None) -> bool:
        """Return whether a source summary cannot rule this matcher out."""
        return self.evaluate_summary(summary) != "F"

    def evaluate_summary(self, summary: SourceOriginSummary | None) -> _OriginSummaryState:
        """Evaluate every predicate against source-origin facts."""
        if not self.predicates:
            return "T"
        states = [predicate.evaluate_summary(summary) for predicate in self.predicates]
        if "F" in states:
            return "F"
        if "U" in states:
            return "U"
        return "T"


def is_path_like_text(text: str) -> bool:
    """Return whether a store value looks like a filesystem path.

    Shared by ingest gating (adapters) and display rewriting
    (serializers) so origin fields and legacy metadata agree on what
    counts as a path.

    >>> is_path_like_text("/work/proj"), is_path_like_text("~")
    (True, True)
    >>> is_path_like_text("a1b2-uuid")
    False
    """
    return "/" in text or "\\" in text or text == "~"


def normalize_origin_path_text(value: str | None) -> str | None:
    """Expand and absolutize a user-supplied origin path filter value.

    CLI and MCP frontends share this so ``--cwd .`` and an MCP
    ``cwd="."`` resolve the same way before matching against the
    absolute paths records carry. The value keeps the user's logical
    (unresolved) form; symlink resolution happens as an extra pattern
    variant at compile time so records stored in either form match.
    """
    if value is None or not value.strip():
        # A blank value must not resolve to the invoking process's cwd.
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = _logical_cwd() / path
    return os.path.normpath(str(path))


def _logical_cwd() -> pathlib.Path:
    physical = pathlib.Path.cwd()
    pwd = os.environ.get("PWD")
    if pwd:
        candidate = pathlib.Path(pwd).expanduser()
        if candidate.is_absolute() and _same_physical_path(candidate, physical):
            return pathlib.Path(os.path.normpath(str(candidate)))
    return physical


def _same_physical_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def origin_filter_nodes(
    *,
    cwd: str | None = None,
    repo: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    cwd_hash: str | None = None,
) -> tuple[FieldEqNode, ...]:
    """Return synthetic field predicates for explicit origin filters.

    Values land verbatim in AST nodes for
    :func:`agentgrep.query.compose_query_ast`, so no query-text quoting
    or escaping is involved.
    """
    # Deferred: keeps pydantic-backed AST models off the CLI cold-start path.
    from agentgrep.query.ast import FieldEqNode

    return tuple(
        FieldEqNode(field=field, value=value)
        for field, value in (
            ("cwd", cwd),
            ("repo", repo),
            ("worktree", worktree),
            ("branch", branch),
            ("cwd_hash", cwd_hash),
        )
        if value and value.strip()
    )


def record_origin_field_values(record: SearchRecord, field: str) -> tuple[str, ...]:
    """Return every known value for an origin query field on ``record``."""
    origin = record.origin
    if field in _PATH_FIELD_KEYS:
        values: list[str] = []
        if origin is not None:
            value = t.cast("str | None", getattr(origin, field))
            if value:
                values.append(value)
            if field == "repo":
                values.extend(value for value in (origin.worktree, origin.cwd) if value)
        values.extend(_metadata_strings(record, _PATH_FIELD_KEYS[field]))
        return _dedupe(values)
    if field in _STRING_FIELD_KEYS:
        values = []
        if origin is not None:
            value = t.cast("str | None", getattr(origin, field))
            if value:
                values.append(value)
        values.extend(_metadata_strings(record, _STRING_FIELD_KEYS[field]))
        return _dedupe(values)
    if field == "project":
        return _project_values(record)
    return ()


def origin_field_values(origin: RecordOrigin, field: str) -> tuple[str, ...]:
    """Return values for an origin query field on one origin object."""
    if field in _PATH_FIELD_KEYS:
        values: list[str] = []
        value = t.cast("str | None", getattr(origin, field))
        if value:
            values.append(value)
        if field == "repo":
            values.extend(value for value in (origin.worktree, origin.cwd) if value)
        return _dedupe(values)
    if field in _STRING_FIELD_KEYS:
        value = t.cast("str | None", getattr(origin, field))
        return () if not value else (value,)
    if field == "project":
        values: list[str] = []
        for value in (origin.repo, origin.worktree, origin.cwd):
            _append_project_value(values, value)
        return _dedupe(values)
    return ()


def record_matches_origin(record: SearchRecord, origin: RecordOrigin | None) -> bool:
    """Return whether ``record`` belongs to the supplied origin context.

    Ranking uses this for opt-in same-project boosts. Path comparisons accept
    descendants, so a target repo of ``/repo`` matches a record cwd of
    ``/repo/src``.
    """
    return OriginMatcher.from_origin(origin).matches(record)


def _metadata_strings(record: SearchRecord, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = record.metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _project_values(record: SearchRecord) -> tuple[str, ...]:
    values: list[str] = []
    if record.origin is not None:
        for value in (record.origin.repo, record.origin.worktree, record.origin.cwd):
            _append_project_value(values, value)
    for key in ("project", "workspace", "directory", "cwd", "repo", "worktree"):
        value = record.metadata.get(key)
        if isinstance(value, str) and value.strip():
            _append_project_value(values, value.strip())
    return _dedupe(values)


def _append_project_value(values: list[str], value: str | None) -> None:
    if not value:
        return
    values.append(value)
    normalized = value.rstrip("/\\")
    if not normalized:
        return
    if "/" in normalized or "\\" in normalized:
        name = pathlib.PurePosixPath(normalized.replace("\\", "/")).name
        if name:
            values.append(name)


def _origin_string_is_glob(value: str) -> bool:
    return "*" in value or "?" in value


def _origin_path_is_glob(values: t.Iterable[str]) -> bool:
    return any(ch in value for value in values for ch in "*?[")


def _origin_string_equal(value: str, target: str, *, is_glob: bool) -> bool:
    value_cmp = value.casefold()
    target_cmp = target.casefold()
    if is_glob:
        return fnmatch.fnmatchcase(value_cmp, target_cmp)
    return value_cmp == target_cmp


def _origin_path_boundary_text(value: str) -> str:
    """Normalize a path for boundary comparison (shared by filter and boost)."""
    if value == "~" or value.startswith("~/"):
        value = str(pathlib.Path(value).expanduser())
    normalized = value.replace("\\", "/")
    stripped = normalized.rstrip("/")
    if stripped:
        return stripped
    if normalized.startswith("/"):
        return "/"
    return normalized


def _path_is_equal_or_descendant(path: str, target: str) -> bool:
    if not path or not target:
        return False
    if target == "/":
        return path.startswith("/")
    return path == target or path.startswith(f"{target}/")


def _dedupe(values: t.Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
