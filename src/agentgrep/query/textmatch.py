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


def _string_match(
    haystack: str,
    needle: str,
    *,
    case_sensitive: bool = False,
) -> bool:
    """Match text/string fields.

    A wildcard value (``*`` / ``?``) matches by anchored glob — ``gpt*``
    means "starts with gpt"; users wanting substring write ``*gpt*``.
    A plain value keeps the historical substring match.
    ``fnmatchcase`` on pre-casefolded inputs keeps the result identical
    across platforms (``fnmatch`` would apply OS-specific normcase).
    """
    if case_sensitive:
        haystack_cmp = haystack
        needle_cmp = needle
    else:
        haystack_cmp = haystack.casefold()
        needle_cmp = needle.casefold()
    if _is_wildcard(needle):
        return fnmatch.fnmatchcase(haystack_cmp, needle_cmp)
    return needle_cmp in haystack_cmp


def _string_equals(
    haystack: str,
    needle: str,
    *,
    case_sensitive: bool = False,
) -> bool:
    """Match identifier-like string fields by whole value.

    Branch names, project names, and hashes are identifiers, not free
    text, so a plain value must equal the whole field (casefolded by
    default). Wildcard values keep the anchored glob from
    :func:`_string_match`.
    """
    if _is_wildcard(needle):
        return _string_match(haystack, needle, case_sensitive=case_sensitive)
    if case_sensitive:
        return haystack == needle
    return haystack.casefold() == needle.casefold()


def _text_matches(
    record: SearchRecord,
    needle: str,
    *,
    case_sensitive: bool = False,
) -> bool:
    """Substring match against the record's text fields.

    Checks text, title, role, model, and path — the same fields that
    :func:`agentgrep.build_search_haystack` concatenates for the
    legacy :func:`agentgrep.matches_text` path. Keeping the surfaces
    aligned prevents a combined field+text query (``agent:codex bliss``)
    from silently dropping records where the text term appears only in
    ``model`` or ``path``.
    """
    needle_cmp = needle if case_sensitive else needle.casefold()
    text = record.text if case_sensitive else record.text.casefold()
    if needle_cmp in text:
        return True
    if record.title is not None:
        title = record.title if case_sensitive else record.title.casefold()
        if needle_cmp in title:
            return True
    if record.role is not None:
        role = record.role if case_sensitive else record.role.casefold()
        if needle_cmp in role:
            return True
    if record.model is not None:
        model = record.model if case_sensitive else record.model.casefold()
        if needle_cmp in model:
            return True
    path = str(record.path)
    if not case_sensitive:
        path = path.casefold()
    return needle_cmp in path


__all__ = (
    "_is_wildcard",
    "_string_equals",
    "_string_match",
    "_text_matches",
)
