"""Deterministic builtin (level 0) activity analysis.

Everything here is pure-Python aggregation over the normalized record
stream: counters, a daily timeline, frequent terms, repeated
instructions, an open-thread heuristic, and coverage. No ML, no
network, no model. The output is a :class:`~agentgrep.insights.model.InsightsActivity`.

Records are duck-typed: any object exposing the
:class:`agentgrep.SearchRecord` attributes (``text``, ``agent``,
``store``, ``kind``, ``path``, ``timestamp``, ``session_id``,
``conversation_id``, ``model``, ``role``, ``title``, ``metadata``)
works, so this module never imports the engine at load time.
"""

from __future__ import annotations

import collections
import re
import typing as t

from agentgrep.insights.model import (
    InsightsActivity,
    InsightsCoverage,
    InsightsOpenThread,
    InsightsTerm,
    InsightsTimelineBucket,
    InsightsWorkArea,
    RecordRef,
)

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep import SearchRecord

_TOP_TERMS = 10
_TOP_WORK_AREAS = 8
_TOP_TIMELINE = 30
_MAX_OPEN_THREADS = 10
_MAX_REPEATED = 8
_MIN_TOKEN_LEN = 3
_SNIPPET_CHARS = 160

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_WS_RE = re.compile(r"\s+")

# A small, intentionally conservative English stopword set plus a handful of
# agent-prompt fillers. Kept inline (not a dependency) so the builtin path
# stays self-contained. Split a named string (not a literal) so the SIM905
# fixer does not collapse it into one over-long list literal.
_STOPWORD_TEXT = (
    "the and for that this with you your are can will from have has had not but "
    "was were they them their then than out our get got use used using into about "
    "would could should what when where which while who whom why how all any some "
    "more most other such only own same too very just over also able please make "
    "made want need like help write code file files line lines function functions "
    "add added adding update updated change changed run running test tests work "
    "working"
)
_STOPWORDS: frozenset[str] = frozenset(_STOPWORD_TEXT.split())


def _tokenize(text: str) -> list[str]:
    """Return casefolded content tokens from ``text`` (stopwords removed)."""
    return [
        token
        for raw in _TOKEN_RE.findall(text)
        if len(token := raw.casefold()) >= _MIN_TOKEN_LEN and token not in _STOPWORDS
    ]


def _top_terms(counter: collections.Counter[str], limit: int) -> tuple[InsightsTerm, ...]:
    """Return the ``limit`` most common terms as :class:`InsightsTerm`."""
    return tuple(InsightsTerm(term=term, count=count) for term, count in counter.most_common(limit))


def _snippet(text: str) -> str:
    """Return a single-line, length-bounded excerpt of ``text``."""
    flattened = _WS_RE.sub(" ", text).strip()
    if len(flattened) <= _SNIPPET_CHARS:
        return flattened
    return flattened[: _SNIPPET_CHARS - 1].rstrip() + "…"


def _record_ref(record: SearchRecord) -> RecordRef:
    """Build a :class:`RecordRef` drilldown handle for ``record``."""
    return RecordRef(
        agent=str(record.agent),
        store=record.store,
        path=str(record.path),
        timestamp=record.timestamp,
        session_id=record.session_id,
        conversation_id=record.conversation_id,
        snippet=_snippet(record.text) if record.text else None,
    )


def _work_area_key(record: SearchRecord) -> tuple[str, str]:
    """Return ``(key, label)`` identifying the record's coarse work area.

    Prefers an explicit session/conversation identity; otherwise falls
    back to the immediate parent directory of the source path so records
    still cluster by project layout.
    """
    if record.session_id:
        return f"session:{record.session_id}", f"session {record.session_id[:12]}"
    if record.conversation_id:
        return (
            f"conversation:{record.conversation_id}",
            f"conversation {record.conversation_id[:12]}",
        )
    parent = record.path.parent.name or str(record.path.parent)
    return f"path:{parent}", parent


def _date_of(timestamp: str | None) -> str | None:
    """Return the ``YYYY-MM-DD`` prefix of an ISO timestamp, if present."""
    if not timestamp or len(timestamp) < 10:
        return None
    head = timestamp[:10]
    if head[4] == "-" and head[7] == "-":
        return head
    return None


def _normalize_instruction(text: str) -> str:
    """Return a normalized first line for repeated-instruction detection."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return _WS_RE.sub(" ", first_line).strip().casefold()


def build_activity(
    records: cabc.Sequence[SearchRecord],
    *,
    sampled: bool,
) -> InsightsActivity:
    """Compute the deterministic activity report for ``records``."""
    token_counter: collections.Counter[str] = collections.Counter()
    instruction_counter: collections.Counter[str] = collections.Counter()
    metadata_fields: collections.Counter[str] = collections.Counter()

    # Per-work-area accumulators.
    area_labels: dict[str, str] = {}
    area_records: dict[str, int] = collections.defaultdict(int)
    area_agents: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    area_stores: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    area_tokens: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)

    # Per-day accumulators.
    day_records: dict[str, int] = collections.defaultdict(int)
    day_agents: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    day_tokens: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)

    open_threads: list[InsightsOpenThread] = []
    records_with_timestamp = 0
    records_with_session_identity = 0

    for record in records:
        tokens = _tokenize(record.text)
        token_counter.update(tokens)

        key, label = _work_area_key(record)
        area_labels.setdefault(key, label)
        area_records[key] += 1
        area_agents[key][str(record.agent)] += 1
        area_stores[key][record.store] += 1
        area_tokens[key].update(tokens)

        date = _date_of(record.timestamp)
        if date is not None:
            records_with_timestamp += 1
            day_records[date] += 1
            day_agents[date][str(record.agent)] += 1
            day_tokens[date].update(tokens)

        if record.session_id or record.conversation_id:
            records_with_session_identity += 1

        for meta_field in ("title", "role", "model"):
            if getattr(record, meta_field, None):
                metadata_fields[meta_field] += 1
        for meta_key in record.metadata:
            metadata_fields[f"metadata.{meta_key}"] += 1

        instruction = _normalize_instruction(record.text)
        if instruction:
            instruction_counter[instruction] += 1

        stripped = record.text.strip()
        if stripped.endswith("?") and len(open_threads) < _MAX_OPEN_THREADS * 4:
            open_threads.append(
                InsightsOpenThread(
                    title=_snippet(record.text),
                    agent=str(record.agent),
                    store=record.store,
                    reason="prompt ends with a question and may be unresolved",
                    ref=_record_ref(record),
                    timestamp=record.timestamp,
                    session_id=record.session_id,
                )
            )

    work_areas = tuple(
        InsightsWorkArea(
            label=area_labels[key],
            record_count=area_records[key],
            agents=dict(area_agents[key]),
            stores=dict(area_stores[key]),
            top_terms=_top_terms(area_tokens[key], 5),
        )
        for key in sorted(area_records, key=lambda k: (-area_records[k], k))[:_TOP_WORK_AREAS]
    )

    timeline = tuple(
        InsightsTimelineBucket(
            date=date,
            record_count=day_records[date],
            agents=dict(day_agents[date]),
            top_terms=_top_terms(day_tokens[date], 5),
        )
        for date in sorted(day_records)[:_TOP_TIMELINE]
    )

    repeated_instructions = tuple(
        instruction for instruction, count in instruction_counter.most_common() if count >= 2
    )[:_MAX_REPEATED]

    coverage = InsightsCoverage(
        records_with_timestamp=records_with_timestamp,
        records_with_session_identity=records_with_session_identity,
        metadata_fields=dict(metadata_fields),
    )

    record_count = len(records)
    activity_units = len(area_records)
    summary = _summary(record_count, activity_units, len(timeline))

    return InsightsActivity(
        summary=summary,
        records_analyzed=record_count,
        activity_units=activity_units,
        sampled=sampled,
        work_areas=work_areas,
        timeline=timeline,
        recurring_patterns=_top_terms(token_counter, _TOP_TERMS),
        repeated_instructions=repeated_instructions,
        open_threads=tuple(open_threads[:_MAX_OPEN_THREADS]),
        coverage=coverage,
    )


def _summary(record_count: int, activity_units: int, day_count: int) -> str:
    """Return a one-line human summary of the activity scope."""
    if record_count == 0:
        return "No records matched the requested scope."
    units = "work area" if activity_units == 1 else "work areas"
    days = "day" if day_count == 1 else "days"
    if day_count:
        return (
            f"Analyzed {record_count} records across {activity_units} {units} "
            f"and {day_count} {days}."
        )
    return f"Analyzed {record_count} records across {activity_units} {units}."
