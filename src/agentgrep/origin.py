"""Helpers for project-origin metadata.

Project origin is optional record metadata. It is deliberately kept out of the
plain text haystack so origin filters do not change ordinary search relevance
or source prefilter behavior.
"""

from __future__ import annotations

import pathlib
import typing as t

from agentgrep.records import RecordOrigin, SearchRecord

if t.TYPE_CHECKING:
    from agentgrep.query.ast import FieldEqNode

__all__ = [
    "LEGACY_ORIGIN_METADATA_KEYS",
    "_origin_path_boundary_text",
    "_path_is_equal_or_descendant",
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
        if value
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


def record_matches_origin(record: SearchRecord, origin: RecordOrigin | None) -> bool:
    """Return whether ``record`` belongs to the supplied origin context.

    Ranking uses this for opt-in same-project boosts. Path comparisons accept
    descendants, so a target repo of ``/repo`` matches a record cwd of
    ``/repo/src``.
    """
    if origin is None or origin.is_empty():
        return False
    checks: list[bool] = []
    if origin.branch:
        checks.append(
            _any_string_equal(record_origin_field_values(record, "branch"), origin.branch),
        )
    if origin.cwd:
        checks.append(_any_path_related(record_origin_field_values(record, "cwd"), origin.cwd))
    if origin.repo:
        checks.append(
            _any_path_related(
                (
                    *record_origin_field_values(record, "repo"),
                    *record_origin_field_values(record, "worktree"),
                    *record_origin_field_values(record, "cwd"),
                ),
                origin.repo,
            ),
        )
    if origin.worktree:
        checks.append(
            _any_path_related(
                (
                    *record_origin_field_values(record, "worktree"),
                    *record_origin_field_values(record, "cwd"),
                ),
                origin.worktree,
            ),
        )
    if origin.cwd_hash:
        checks.append(
            _any_string_equal(record_origin_field_values(record, "cwd_hash"), origin.cwd_hash),
        )
    return bool(checks) and all(checks)


def _any_string_equal(values: t.Iterable[str], target: str) -> bool:
    """Casefolded whole-value match, mirroring the branch:/cwd_hash: filters."""
    target_cmp = target.casefold()
    return any(value.casefold() == target_cmp for value in values)


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


def _any_path_related(values: t.Iterable[str], target: str) -> bool:
    target_norm = _origin_path_boundary_text(target)
    if not target_norm:
        return False
    return any(
        _path_is_equal_or_descendant(_origin_path_boundary_text(value), target_norm)
        for value in values
    )


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
