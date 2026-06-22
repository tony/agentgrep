"""Path-pattern compilation and matching for compiled queries.

See ADR 0010 (module boundaries and the facade re-export contract).
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import pathlib
import typing as t

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
    "_compile_path_pattern",
    "_compile_path_patterns",
    "_dedupe_preserving_order",
    "_eq_value",
    "_expand_current_user_home_patterns",
    "_path_match",
    "_path_pattern_for",
    "_path_separators",
)
