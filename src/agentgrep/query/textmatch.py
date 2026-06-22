"""Case-insensitive text / wildcard matching for compiled queries."""

from __future__ import annotations

import fnmatch

from agentgrep.records import SearchRecord


def _is_wildcard(value: str) -> bool:
    """Return whether a string-field value carries a glob wildcard.

    Only ``*`` and ``?`` count; ``[...]`` classes stay path-only so a
    literal ``model:gpt[4]`` is not surprisingly reinterpreted.
    """
    return "*" in value or "?" in value


def _string_match(haystack: str, needle: str) -> bool:
    """Case-insensitive match for text/string fields.

    A wildcard value (``*`` / ``?``) matches by anchored glob — ``gpt*``
    means "starts with gpt"; users wanting substring write ``*gpt*``.
    A plain value keeps the historical casefolded substring match.
    ``fnmatchcase`` on pre-casefolded inputs keeps the result identical
    across platforms (``fnmatch`` would apply OS-specific normcase).
    """
    if _is_wildcard(needle):
        return fnmatch.fnmatchcase(haystack.casefold(), needle.casefold())
    return needle.casefold() in haystack.casefold()


def _text_matches(record: SearchRecord, needle: str) -> bool:
    """Case-insensitive substring match against the record's text fields.

    Checks text, title, role, model, and path — the same fields that
    :func:`agentgrep.build_search_haystack` concatenates for the
    legacy :func:`agentgrep.matches_text` path. Keeping the surfaces
    aligned prevents a combined field+text query (``agent:codex bliss``)
    from silently dropping records where the text term appears only in
    ``model`` or ``path``.
    """
    needle_cf = needle.casefold()
    if needle_cf in record.text.casefold():
        return True
    if record.title is not None and needle_cf in record.title.casefold():
        return True
    if record.role is not None and needle_cf in record.role.casefold():
        return True
    if record.model is not None and needle_cf in record.model.casefold():
        return True
    return needle_cf in str(record.path).casefold()


__all__ = (
    "_is_wildcard",
    "_string_match",
    "_text_matches",
)
