"""Path-pattern compilation and matching for compiled queries."""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import pathlib

from agentgrep import wsl
from agentgrep._text import restore_encoded_paths
from agentgrep.origin import _dedupe as _dedupe_preserving_order
from agentgrep.query.ast import (
    AndNode,
    FieldCmpNode,
    FieldEqNode,
    FieldRangeNode,
    NotNode,
    OrNode,
    QueryNode,
)


@dataclasses.dataclass(slots=True, frozen=True)
class _CompiledPathPattern:
    """Pre-expanded path predicate used by compiled query closures."""

    raw: str
    variants: tuple[str, ...]
    is_glob: bool


_PathPatternKey = tuple[str, str]


def _compile_path_patterns(
    node: QueryNode,
    *,
    path_fields: frozenset[str] | None = None,
) -> dict[_PathPatternKey, _CompiledPathPattern]:
    """Return pre-expanded path patterns keyed by field and raw query value."""
    fields = frozenset({"path"}) if path_fields is None else path_fields
    if isinstance(node, FieldEqNode) and node.field in fields:
        # Origin paths (cwd/repo/worktree) also match their resolved
        # form so a symlinked filter finds physically recorded paths;
        # `path:` keeps pure substring semantics.
        return {
            (node.field, node.value): _compile_path_pattern(
                node.value,
                add_resolved=node.field != "path",
            ),
        }
    if isinstance(node, NotNode):
        return _compile_path_patterns(node.child, path_fields=fields)
    if isinstance(node, AndNode | OrNode):
        patterns: dict[_PathPatternKey, _CompiledPathPattern] = {}
        for child in node.children:
            patterns.update(_compile_path_patterns(child, path_fields=fields))
        return patterns
    return {}


def _compile_path_pattern(raw: str, *, add_resolved: bool = False) -> _CompiledPathPattern:
    """Compile one ``path:`` value into raw and home-expanded variants."""
    variants = [raw]
    variants.extend(_expand_current_user_home_patterns(raw))
    is_glob = any(ch in variant for variant in variants for ch in "*?[")
    if add_resolved and not is_glob:
        variants.extend(
            str(pathlib.Path(variant).resolve(strict=False))
            for variant in tuple(variants)
            if pathlib.Path(variant).is_absolute()
        )
    unique_variants = _dedupe_preserving_order(variants)
    return _CompiledPathPattern(
        raw=raw,
        variants=unique_variants,
        is_glob=is_glob,
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
    # Display paths also hide account names inside encoded project folders
    # (``-home-u-work`` as ``-~-work``); restore them so a copied path matches.
    for prefix in ("~/", f"~{os.sep}", f"~{os.altsep}" if os.altsep else None):
        if prefix is not None and raw.startswith(prefix):
            return restore_encoded_paths(home + raw[1:], home)
    # A Windows path is displayed as %APPDATA%/..., %LOCALAPPDATA%/..., or
    # %USERPROFILE%/...; expand it to every Windows profile so a copied path matches.
    candidates = wsl.host_display_candidates(raw)
    if candidates:
        return _dedupe_preserving_order(
            [variant for path in candidates for variant in restore_encoded_paths(str(path), home)],
        )
    restored = restore_encoded_paths(raw, home)
    return () if restored == (raw,) else restored


def _path_separators() -> tuple[str, ...]:
    """Return filesystem separators that may appear in local path strings."""
    separators = [os.sep]
    if os.altsep is not None:
        separators.append(os.altsep)
    return _dedupe_preserving_order(separators)


def _path_pattern_for(
    node: FieldEqNode | FieldCmpNode | FieldRangeNode,
    path_patterns: dict[_PathPatternKey, _CompiledPathPattern],
) -> _CompiledPathPattern:
    """Return the precompiled path pattern for a path predicate node."""
    raw = _eq_value(node)
    compiled = path_patterns.get((node.field, raw))
    if compiled is not None:
        return compiled
    return _compile_path_pattern(raw, add_resolved=node.field != "path")


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


__all__ = (
    "_CompiledPathPattern",
    "_PathPatternKey",
    "_compile_path_pattern",
    "_compile_path_patterns",
    "_dedupe_preserving_order",
    "_eq_value",
    "_expand_current_user_home_patterns",
    "_path_match",
    "_path_pattern_for",
    "_path_separators",
)
