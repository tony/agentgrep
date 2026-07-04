"""Read-only export of matched records and conversations to portable artifacts.

This is the one frontend-neutral core the CLI ``export`` verb and the
``export_records`` MCP tool drive (the TUI action is deferred), so the formats
never drift. It owns conversation
assembly, an export-owned total order (``timestamp`` then content id) that makes
reruns byte-identical and diffable, optional body redaction, and each tier's
writer.

It is deliberately never imported by the package ``__init__`` — users who never
export pay zero cold-start — and it imports no pydantic: the NDJSON/JSON writers
reuse :func:`agentgrep.cli.serializers.serialize_search_record` through a
function-local import, inheriting the pydantic-free fallback.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import typing as t

from agentgrep.identity import (
    conversation_anchor,
    conversation_content_hash,
    record_content_id,
    session_identity,
    short_id,
)

if t.TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from agentgrep.records import SearchRecord

__all__ = [
    "Conversation",
    "ExportFormat",
    "Turn",
    "assemble_conversations",
    "export_total_order_key",
    "iter_ndjson_lines",
    "redact_record",
    "render_csv",
    "render_export",
    "render_json",
    "render_markdown",
]

ExportFormat = t.Literal["ndjson", "json", "markdown", "csv"]


@dataclasses.dataclass(frozen=True, slots=True)
class Turn:
    """One ordered turn within an assembled conversation."""

    role: str
    text: str
    timestamp: str | None
    content_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class Conversation:
    """An assembled conversation: a durable anchor plus ordered turns."""

    id: str
    content_hash: str
    agent: str
    store: str
    session_id: str | None
    model: str | None
    turns: tuple[Turn, ...]


def _redact_text(text: str) -> str:
    """Return a stable, body-free stand-in for redacted text."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def export_total_order_key(record: SearchRecord) -> tuple[str, str]:
    """Return the export-owned total order for ``record``.

    ``(timestamp, content-id)`` — independent of the engine's
    scheduler-dependent stream order, so a re-export is byte-identical.
    """
    return (record.timestamp or "", record_content_id(record))


def redact_record(payload: dict[str, object]) -> dict[str, object]:
    """Return a copy of a serialized record with all bodies redacted.

    Hashes ``text`` and ``title`` (both prompt-derived content) to
    ``sha256:<digest>`` and drops ``metadata`` (which some backends populate
    with raw absolute workspace paths), while keeping the id, provenance, and
    shape. A redacted export therefore stays diffable and citeable without
    carrying prompt content or local paths off-box.
    """
    redacted = dict(payload)
    text = redacted.get("text")
    if isinstance(text, str):
        redacted["text"] = _redact_text(text)
    title = redacted.get("title")
    if isinstance(title, str) and title:
        redacted["title"] = _redact_text(title)
    if "metadata" in redacted:
        redacted["metadata"] = {}
    return redacted


def assemble_conversations(records: Iterable[SearchRecord]) -> list[Conversation]:
    """Group records into conversations, deterministically.

    Grouping uses the kind-free session-identity ladder
    (``session_id`` or ``conversation_id`` or the home-collapsed display path),
    turns are ordered by :func:`export_total_order_key`, and conversations are
    ordered by their earliest turn, so the assembly is a pure function of the
    record set.

    Parameters
    ----------
    records : iterable of SearchRecord
        The records to assemble.

    Returns
    -------
    list of Conversation
        Assembled conversations in deterministic order.
    """
    buckets: dict[str, list[SearchRecord]] = {}
    for record in records:
        buckets.setdefault(session_identity(record), []).append(record)
    conversations: list[Conversation] = []
    for members in buckets.values():
        ordered = sorted(members, key=export_total_order_key)
        turns = tuple(
            Turn(
                role=member.role or "user",
                text=member.text,
                timestamp=member.timestamp,
                content_id=record_content_id(member),
            )
            for member in ordered
        )
        first = ordered[0]
        conversations.append(
            Conversation(
                id=conversation_anchor(first),
                content_hash=conversation_content_hash(turn.content_id for turn in turns),
                agent=first.agent,
                store=first.store,
                session_id=first.session_id,
                model=first.model,
                turns=turns,
            ),
        )
    conversations.sort(key=lambda conv: (conv.turns[0].timestamp or "", conv.turns[0].content_id))
    return conversations


def _ordered(records: Iterable[SearchRecord], limit: int | None) -> list[SearchRecord]:
    ordered = sorted(records, key=export_total_order_key)
    return ordered if limit is None else ordered[:limit]


def render_export(
    records: Iterable[SearchRecord],
    fmt: ExportFormat,
    *,
    redact: bool = False,
    limit: int | None = None,
) -> str:
    """Render records to the requested format (canonical, no trailing newline)."""
    if fmt == "ndjson":
        lines = iter_ndjson_lines(records, redact=redact, limit=limit)
        return "".join(f"{line}\n" for line in lines)
    if fmt == "json":
        return render_json(records, redact=redact, limit=limit)
    if fmt == "csv":
        return render_csv(records, redact=redact, limit=limit)
    selected = records if limit is None else sorted(records, key=export_total_order_key)[:limit]
    return render_markdown(assemble_conversations(selected), redact=redact)


def iter_ndjson_lines(
    records: Iterable[SearchRecord],
    *,
    redact: bool = False,
    limit: int | None = None,
) -> Iterator[str]:
    """Yield one deterministic NDJSON line per record, sorted by total order."""
    from agentgrep.cli.serializers import serialize_search_record

    for record in _ordered(records, limit):
        payload: dict[str, object] = dict(serialize_search_record(record))
        if redact:
            payload = redact_record(payload)
        yield json.dumps(payload, ensure_ascii=False, sort_keys=True)


def render_json(
    records: Iterable[SearchRecord],
    *,
    redact: bool = False,
    limit: int | None = None,
) -> str:
    """Render a single, capped JSON envelope of records (not streamable)."""
    from agentgrep.cli.serializers import build_envelope, serialize_search_record

    rows: list[dict[str, object]] = []
    for record in _ordered(records, limit):
        payload: dict[str, object] = dict(serialize_search_record(record))
        if redact:
            payload = redact_record(payload)
        rows.append(payload)
    envelope = build_envelope("export", {"format": "json"}, rows)
    return json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)


_CSV_COLUMNS = ("id", "agent", "store", "timestamp", "role", "session_id", "text")


def render_csv(
    records: Iterable[SearchRecord],
    *,
    redact: bool = False,
    limit: int | None = None,
) -> str:
    """Render records as CSV (prompt scope), quoting via :mod:`csv`.

    Embedded newlines, commas, and quotes in prompt text go through
    ``csv.writer`` rather than a hand-rolled join, so the output stays valid.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for record in _ordered(records, limit):
        text = _redact_text(record.text) if redact else record.text
        writer.writerow(
            [
                short_id(record_content_id(record)),
                record.agent,
                record.store,
                record.timestamp or "",
                record.role or "",
                record.session_id or "",
                text,
            ],
        )
    return buffer.getvalue()


def _fence_for(body: str) -> str:
    """Return a backtick fence one longer than the longest backtick run in ``body``.

    Examples
    --------
    >>> _fence_for("plain text")
    '```'
    >>> _fence_for("has ``` a fence")
    '````'
    """
    longest = 0
    run = 0
    for char in body:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def render_markdown(conversations: Sequence[Conversation], *, redact: bool = False) -> str:
    """Render assembled conversations as Markdown transcripts.

    Each conversation opens with a YAML front-matter block carrying the content
    id and provenance, then a ``## role`` section per turn with a fenced body;
    the fence grows past any backtick run in the body so nested code survives.
    """
    blocks: list[str] = []
    for conversation in conversations:
        front = [
            "---",
            f"id: {short_id(conversation.content_hash)}",
            f"content_hash: {conversation.content_hash}",
            f"conversation_id: {conversation.id}",
            f"agent: {conversation.agent}",
            f"store: {conversation.store}",
            f"session_id: {conversation.session_id or ''}",
            f"model: {conversation.model or ''}",
            "---",
        ]
        lines: list[str] = ["\n".join(front), ""]
        for turn in conversation.turns:
            text = _redact_text(turn.text) if redact else turn.text
            fence = _fence_for(text)
            lines.append(f"## {turn.role}")
            lines.append("")
            lines.append(f"{fence}\n{text}\n{fence}")
            lines.append("")
        blocks.append("\n".join(lines).rstrip() + "\n")
    return "\n".join(blocks)
