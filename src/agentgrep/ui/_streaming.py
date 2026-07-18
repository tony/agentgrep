"""Bounded streaming and match-highlight helpers for the explorer.

Pure helpers shared by the HUD's hot paths: chunking streamed records for
the filter worker (NB-4) and applying fixed-budget literal match spans to
detail bodies (NB-9). Everything here is Textual-free and hard-bounded so
it stays safe to call from the pump or from an ``@offload`` worker.
"""

from __future__ import annotations

import collections.abc as cabc
import re
import typing as t

from agentgrep._text import DETAIL_BODY_MAX_CHARS

if t.TYPE_CHECKING:
    from rich.text import Text

    from agentgrep.records import SearchRecord

_DETAIL_HIGHLIGHT_MAX_TERMS = 32
_DETAIL_HIGHLIGHT_MAX_MATCHES = 256
_DETAIL_HIGHLIGHT_MAX_TERM_CHARS = 256
_DETAIL_HIGHLIGHT_MAX_TOTAL_TERM_CHARS = 2048
_STREAM_FILTER_MAX_TEXT_CHARS = 2 << 20


def _bounded_literal_terms(
    terms: cabc.Sequence[str],
    *,
    case_sensitive: bool,
) -> tuple[str, ...]:
    """Deduplicate decorative literal terms within a fixed presentation budget."""
    result: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for term in terms:
        if not term or len(term) > _DETAIL_HIGHLIGHT_MAX_TERM_CHARS:
            continue
        key = term if case_sensitive else term.casefold()
        if key in seen:
            continue
        if total_chars + len(term) > _DETAIL_HIGHLIGHT_MAX_TOTAL_TERM_CHARS:
            break
        seen.add(key)
        result.append(term)
        total_chars += len(term)
        if len(result) >= _DETAIL_HIGHLIGHT_MAX_TERMS:
            break
    return tuple(result)


def _stream_filter_chunks(
    records: cabc.Sequence[SearchRecord],
    *,
    max_records: int,
    max_chars: int,
) -> cabc.Iterator[tuple[SearchRecord, ...]]:
    """Yield record slices bounded by count and projected body characters."""
    chunk: list[SearchRecord] = []
    chunk_chars = 0
    for record in records:
        record_chars = min(len(record.text), max_chars)
        if chunk and (len(chunk) >= max_records or chunk_chars + record_chars > max_chars):
            yield tuple(chunk)
            chunk = []
            chunk_chars = 0
        chunk.append(record)
        chunk_chars += record_chars
    if chunk:
        yield tuple(chunk)


def _apply_bounded_literal_highlights(
    text: Text,
    source: str,
    terms: cabc.Sequence[str],
    *,
    case_sensitive: bool,
    style: str,
) -> None:
    """Apply a bounded number of literal match spans to ``text``."""
    flags = 0 if case_sensitive else re.IGNORECASE
    remaining = _DETAIL_HIGHLIGHT_MAX_MATCHES
    for term in _bounded_literal_terms(terms, case_sensitive=case_sensitive):
        compiled = re.compile(re.escape(term), flags)
        for match in compiled.finditer(source):
            text.stylize(style, match.start(), match.end())
            remaining -= 1
            if remaining == 0:
                return


def _json_pretty_print_is_bounded(text: str) -> bool:
    """Return whether two-space indentation has a conservative output budget."""
    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char in "]}":
            depth = max(0, depth - 1)
    estimated_max = len(text) * (2 * max_depth + 3)
    return estimated_max <= DETAIL_BODY_MAX_CHARS
