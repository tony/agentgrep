"""Inline-completion for the Textual explorer's query inputs.

Completion is registry-driven and offers **query-language keywords only** —
field names and aliases (``age`` -> ``agent:``) and enum values
(``agent:co`` -> ``agent:codex``). It deliberately does not complete from
record text, which would surface arbitrary tokens and IDs.

:class:`QuerySuggester` backs the inline ghost text on both the search bar
and the (query-aware) filter box; :func:`keyword_completion_candidates`
backs the dropdown list. The suggester runs with ``case_sensitive=True`` so
:meth:`get_suggestion` receives the user's raw value and returns
``value + tail`` (the typed value plus only the missing characters), which
is what Textual's ``Input`` requires to render the ghost text; matching is
case-insensitive internally.
"""

from __future__ import annotations

import typing as t

from textual.suggester import Suggester

if t.TYPE_CHECKING:
    from agentgrep.query.registry import FieldRegistry


def _trailing_token(value: str) -> tuple[str, str]:
    """Split ``value`` into its leading prefix and trailing token.

    The prefix includes the whitespace separator so ``prefix + token``
    reconstructs ``value``.
    """
    head, sep, last = value.rpartition(" ")
    return head + sep, last


def enum_value_candidates(
    text: str,
    registry: FieldRegistry,
) -> tuple[str, tuple[str, ...]] | None:
    """Return enum-value candidates for a trailing ``field:partial`` token.

    Used by the search bar's dropdown: when the trailing token selects an
    enum field (``agent:``, ``scope:``), return the field's canonical name
    and the enum values matching the partial. Returns ``None`` when the
    trailing token is not an enum field predicate or nothing matches.

    Parameters
    ----------
    text : str
        Current search-bar value.
    registry : FieldRegistry
        Registry whose enum fields seed the candidates.

    Returns
    -------
    tuple[str, tuple[str, ...]] or None
        ``(field_name, matching_values)`` or ``None``.
    """
    if not text:
        return None
    _prefix, last = _trailing_token(text)
    if ":" not in last:
        return None
    field, _, partial = last.partition(":")
    spec = registry.get(field)
    if spec is None or not spec.enum_values:
        return None
    partial_cf = partial.casefold()
    matches = tuple(value for value in spec.enum_values if value.casefold().startswith(partial_cf))
    if not matches:
        return None
    # Nothing left to pick once the value is fully typed (the sole match
    # equals the partial) — don't reopen a redundant one-item dropdown.
    if len(matches) == 1 and matches[0].casefold() == partial_cf:
        return None
    return (spec.name, matches)


def apply_enum_choice(text: str, value: str) -> str:
    """Replace the trailing ``field:partial`` token's value with ``value``.

    Returns the rewritten search string, e.g. ``("ruff agent:cu", "cursor-cli")``
    -> ``"ruff agent:cursor-cli"``. If the trailing token has no colon the
    text is returned unchanged.
    """
    prefix, last = _trailing_token(text)
    field, sep, _partial = last.partition(":")
    if not sep:
        return text
    return f"{prefix}{field}:{value}"


def apply_word_choice(text: str, word: str) -> str:
    """Replace the trailing whitespace token with ``word``.

    Used by the dropdown for field-keyword completions:
    ``("ruff age", "agent:")`` -> ``"ruff agent:"``.
    """
    prefix, _last = _trailing_token(text)
    return f"{prefix}{word}"


def _field_keyword_names(registry: FieldRegistry) -> tuple[str, ...]:
    """Return sorted field names + aliases for keyword completion."""
    return tuple(sorted({name for spec in registry.specs for name in (spec.name, *spec.aliases)}))


def keyword_completion_candidates(
    text: str,
    registry: FieldRegistry,
    *,
    limit: int = 12,
) -> tuple[str, ...] | None:
    """Return query-language keyword candidates for the dropdown.

    Both the search bar and the (query-aware) filter box use this:

    - a ``field:partial`` trailing token yields the field's enum values
      (e.g. ``agent:cu`` -> ``cursor-cli``, ``cursor-ide``);
    - a bare token yields matching field keywords rendered with a trailing
      colon (``a`` -> ``adapter:``, ``adapter_id:``, ``agent:``).

    Matching is case-insensitive. No record-vocabulary terms are offered —
    completion is limited to the query language. Returns ``None`` when there
    is nothing to offer.
    """
    if not text:
        return None
    _prefix, last = _trailing_token(text)
    if not last:
        return None
    if ":" in last:
        enum = enum_value_candidates(text, registry)
        return None if enum is None else enum[1]
    last_cf = last.casefold()
    keywords = tuple(
        f"{name}:" for name in _field_keyword_names(registry) if name.casefold().startswith(last_cf)
    )
    return keywords[:limit] if keywords else None


class QuerySuggester(Suggester):
    """Complete query field names and enum values for the search bar."""

    def __init__(self, registry: FieldRegistry) -> None:
        """Build a suggester from a query field registry.

        Parameters
        ----------
        registry : FieldRegistry
            Registry whose field names, aliases, and enum values seed the
            completions.
        """
        super().__init__(use_cache=True, case_sensitive=True)
        self._registry = registry
        names: list[str] = []
        for spec in registry.specs:
            names.append(spec.name)
            names.extend(spec.aliases)
        self._field_names: tuple[str, ...] = tuple(sorted(set(names)))

    async def get_suggestion(self, value: str) -> str | None:
        """Return a completion for ``value``'s trailing token, or ``None``."""
        if not value:
            return None
        _prefix, last = _trailing_token(value)
        if not last:
            return None
        if ":" in last:
            return self._complete_enum_value(value, last)
        return self._complete_field_name(value, last)

    def _complete_enum_value(self, value: str, last: str) -> str | None:
        """Complete ``field:partial`` against the field's enum values."""
        field, _, partial = last.partition(":")
        spec = self._registry.get(field)
        if spec is None or not spec.enum_values:
            return None
        partial_cf = partial.casefold()
        for enum_value in spec.enum_values:
            value_cf = enum_value.casefold()
            if value_cf.startswith(partial_cf) and value_cf != partial_cf:
                return value + enum_value[len(partial) :]
        return None

    def _complete_field_name(self, value: str, last: str) -> str | None:
        """Complete a bare token against a field name, adding the ``:``."""
        last_cf = last.casefold()
        for name in self._field_names:
            if name.casefold().startswith(last_cf):
                return value + name[len(last) :] + ":"
        return None
