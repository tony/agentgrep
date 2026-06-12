"""Pure-Python insights report helpers."""

from __future__ import annotations

import collections
import collections.abc as cabc
import dataclasses
import re
import typing as t

import agentgrep

InsightsLevel = t.Literal[
    "builtin",
    "html",
    "ml",
    "embeddings",
    "index",
    "llm",
    "best-installed",
]

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOPWORDS = frozenset(
    {
        "about",
        "again",
        "and",
        "for",
        "from",
        "into",
        "the",
        "this",
        "that",
        "with",
        "without",
    },
)


class InsightsTermPayload(t.TypedDict):
    """JSON payload for one term-frequency row."""

    term: str
    count: int


class InsightsReportPayload(t.TypedDict):
    """JSON payload for a builtin insights report."""

    level: str
    requested_level: str
    scope: agentgrep.SearchScope
    agents: dict[str, int]
    stores: dict[str, int]
    kinds: dict[str, int]
    records_analyzed: int
    record_limit: int | None
    sampled: bool
    timestamp_range: dict[str, str | None]
    top_terms: list[InsightsTermPayload]
    skipped_enrichers: list[str]


@dataclasses.dataclass(frozen=True, slots=True)
class InsightsTerm:
    """One token-frequency row in an insights report."""

    term: str
    count: int

    def to_payload(self) -> InsightsTermPayload:
        """Return the JSON-compatible representation."""
        return {"term": self.term, "count": self.count}


@dataclasses.dataclass(frozen=True, slots=True)
class InsightsReport:
    """Aggregated local insights report."""

    level: str
    requested_level: str
    scope: agentgrep.SearchScope
    records_analyzed: int
    record_limit: int | None
    sampled: bool
    agents: dict[str, int]
    stores: dict[str, int]
    kinds: dict[str, int]
    earliest_timestamp: str | None
    latest_timestamp: str | None
    top_terms: tuple[InsightsTerm, ...]
    skipped_enrichers: tuple[str, ...]

    def to_payload(self) -> InsightsReportPayload:
        """Return the JSON-compatible representation."""
        return {
            "level": self.level,
            "requested_level": self.requested_level,
            "scope": self.scope,
            "agents": self.agents,
            "stores": self.stores,
            "kinds": self.kinds,
            "records_analyzed": self.records_analyzed,
            "record_limit": self.record_limit,
            "sampled": self.sampled,
            "timestamp_range": {
                "earliest": self.earliest_timestamp,
                "latest": self.latest_timestamp,
            },
            "top_terms": [term.to_payload() for term in self.top_terms],
            "skipped_enrichers": list(self.skipped_enrichers),
        }


def build_report(
    records: cabc.Iterable[agentgrep.SearchRecord],
    *,
    scope: agentgrep.SearchScope,
    requested_level: InsightsLevel,
    record_limit: int | None,
    sampled: bool,
) -> InsightsReport:
    """Build a deterministic builtin report from normalized records."""
    record_list = list(records)
    agent_counts: collections.Counter[str] = collections.Counter()
    store_counts: collections.Counter[str] = collections.Counter()
    kind_counts: collections.Counter[str] = collections.Counter()
    token_counts: collections.Counter[str] = collections.Counter()
    timestamps: list[str] = []

    for record in record_list:
        agent_counts[record.agent] += 1
        store_counts[record.store] += 1
        kind_counts[record.kind] += 1
        if record.timestamp:
            timestamps.append(record.timestamp)
        for token in _iter_tokens(record.text):
            token_counts[token] += 1

    top_terms = tuple(
        InsightsTerm(term=term, count=count)
        for term, count in sorted(
            token_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:10]
    )

    return InsightsReport(
        level="builtin",
        requested_level=requested_level,
        scope=scope,
        records_analyzed=len(record_list),
        record_limit=record_limit,
        sampled=sampled,
        agents=dict(sorted(agent_counts.items())),
        stores=dict(sorted(store_counts.items())),
        kinds=dict(sorted(kind_counts.items())),
        earliest_timestamp=min(timestamps) if timestamps else None,
        latest_timestamp=max(timestamps) if timestamps else None,
        top_terms=top_terms,
        skipped_enrichers=_skipped_enrichers(requested_level),
    )


def _iter_tokens(text: str) -> cabc.Iterator[str]:
    """Yield normalized report tokens from record text."""
    for match in _TOKEN_RE.finditer(text.casefold()):
        token = match.group(0)
        if token in _STOPWORDS:
            continue
        yield token


def _skipped_enrichers(requested_level: InsightsLevel) -> tuple[str, ...]:
    """Return skipped optional enrichers for the selected concept level."""
    if requested_level == "builtin":
        return (
            "html templates",
            "classical ML",
            "embeddings",
            "persistent index",
            "local LLM",
        )
    if requested_level == "best-installed":
        return ("optional enrichers require installed extras",)
    return (f"{requested_level} backend is not implemented in this slice",)
