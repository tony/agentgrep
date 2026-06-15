"""Inline-completion suggesters for the Textual explorer.

Two :class:`textual.suggester.Suggester` implementations back the inline
ghost-text completion in :mod:`agentgrep.ui.app`:

- :class:`QuerySuggester` completes the search bar's trailing token —
  field names and aliases (``age`` -> ``agent:``) and enum values
  (``agent:co`` -> ``agent:codex``) — from the query field registry.
- :class:`FilterSuggester` completes the filter box's trailing token
  from a vocabulary of words drawn from the loaded result records.

Both run with ``case_sensitive=True`` so :meth:`get_suggestion` receives
the user's raw value, and they return ``value + tail`` (the typed value
plus only the missing characters). That guarantees the suggestion starts
with the value, which is what Textual's ``Input`` requires to render the
ghost text, while case-insensitive matching happens internally.
"""

from __future__ import annotations

import collections.abc as cabc
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


class FilterSuggester(Suggester):
    """Complete the filter box's trailing token from a word vocabulary."""

    def __init__(self, vocabulary: cabc.Iterable[str]) -> None:
        """Build a suggester from an initial vocabulary.

        Parameters
        ----------
        vocabulary : collections.abc.Iterable[str]
            Words available as completions; refreshed via
            :meth:`set_vocabulary` as records load.
        """
        # The vocabulary mutates as records stream in, so caching would
        # serve stale suggestions; disable it.
        super().__init__(use_cache=False, case_sensitive=True)
        self._vocabulary: tuple[str, ...] = tuple(sorted(set(vocabulary)))

    def set_vocabulary(self, vocabulary: cabc.Iterable[str]) -> None:
        """Replace the completion vocabulary (e.g. after records load)."""
        self._vocabulary = tuple(sorted(set(vocabulary)))

    async def get_suggestion(self, value: str) -> str | None:
        """Return a completion for ``value``'s trailing token, or ``None``."""
        if not value:
            return None
        _prefix, last = _trailing_token(value)
        if not last:
            return None
        last_cf = last.casefold()
        for word in self._vocabulary:
            word_cf = word.casefold()
            if word_cf.startswith(last_cf) and word_cf != last_cf:
                return value + word[len(last) :]
        return None
