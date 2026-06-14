"""Typed report model for the insights pipeline.

This module owns the dependency-light dataclasses and JSON payload
shapes for insights reports. It imports nothing heavier than the
standard library so ``import agentgrep`` and the builtin report path
stay cheap (see ADR 0005 § *Dependency Levels*).

Report-local result types (:class:`ReportStatus`, :class:`ReportDiagnostic`,
:class:`RecordRef`) map onto the ADR 0004 result vocabulary
(``RunStatus``, ``Diagnostic``, ``RecordRef``). They are defined here as
small standalone types while those canonical result types are still
settling; the intent is to converge, not to grow a second vocabulary.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
"""Schema version embedded in the machine-readable report payload."""

InsightsLevel = t.Literal["builtin", "html", "ml", "embeddings", "index", "llm"]
"""A concrete enrichment rung. ``builtin`` is always available."""

RequestedLevel = t.Literal["builtin", "html", "ml", "embeddings", "index", "llm", "best-installed"]
"""A level the user may request, including the ``best-installed`` selector."""

ReportStatusName = t.Literal["ok", "partial", "empty", "error"]
"""Coarse run outcome, mapping to the ADR 0004 ``RunStatus`` concept."""

DiagnosticSeverity = t.Literal["info", "warning", "error"]
"""Severity for a :class:`ReportDiagnostic`."""

LEVEL_ORDER: tuple[InsightsLevel, ...] = (
    "builtin",
    "html",
    "ml",
    "embeddings",
    "index",
    "llm",
)
"""Levels from cheapest/always-available to heaviest. Order is load-bearing
for ``best-installed`` selection."""


def level_rank(level: InsightsLevel) -> int:
    """Return the ladder position of ``level`` (``builtin`` is ``0``)."""
    return LEVEL_ORDER.index(level)


# ---------------------------------------------------------------------------
# JSON payload shapes (TypedDicts)
# ---------------------------------------------------------------------------


class RecordRefPayload(t.TypedDict):
    """JSON shape for a :class:`RecordRef`."""

    agent: str
    store: str
    path: str
    timestamp: str | None
    session_id: str | None
    conversation_id: str | None
    snippet: str | None


class ReportDiagnosticPayload(t.TypedDict):
    """JSON shape for a :class:`ReportDiagnostic`."""

    severity: DiagnosticSeverity
    code: str
    message: str
    setup_command: str | None


class InsightsTermPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsTerm`."""

    term: str
    count: int


class InsightsTimelineBucketPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsTimelineBucket`."""

    date: str
    record_count: int
    agents: dict[str, int]
    top_terms: list[InsightsTermPayload]


class InsightsWorkAreaPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsWorkArea`."""

    label: str
    record_count: int
    agents: dict[str, int]
    stores: dict[str, int]
    top_terms: list[InsightsTermPayload]


class InsightsOpenThreadPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsOpenThread`."""

    title: str
    agent: str
    store: str
    timestamp: str | None
    session_id: str | None
    reason: str
    ref: RecordRefPayload


class InsightsCoveragePayload(t.TypedDict):
    """JSON shape for a :class:`InsightsCoverage`."""

    records_with_timestamp: int
    records_with_session_identity: int
    metadata_fields: dict[str, int]


class InsightsActivityPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsActivity`."""

    summary: str
    records_analyzed: int
    activity_units: int
    sampled: bool
    work_areas: list[InsightsWorkAreaPayload]
    timeline: list[InsightsTimelineBucketPayload]
    recurring_patterns: list[InsightsTermPayload]
    repeated_instructions: list[str]
    open_threads: list[InsightsOpenThreadPayload]
    coverage: InsightsCoveragePayload


class InsightsEnrichmentPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsEnrichment`."""

    level: InsightsLevel
    backend: str
    status: t.Literal["ok", "skipped", "error"]
    message: str
    data: dict[str, t.Any]
    provenance: dict[str, t.Any] | None


class InsightsLevelStatusPayload(t.TypedDict):
    """JSON shape for a :class:`InsightsLevelStatus`."""

    level: InsightsLevel
    available: bool
    backend: str | None
    reason: str
    setup_command: str | None


class InsightsReportPayload(t.TypedDict):
    """JSON shape for a full :class:`InsightsReport`."""

    schema_version: int
    status: ReportStatusName
    scope: str
    requested_level: RequestedLevel
    level: InsightsLevel
    records_analyzed: int
    record_limit: int | None
    sampled: bool
    agents: dict[str, int]
    stores: dict[str, int]
    kinds: dict[str, int]
    earliest_timestamp: str | None
    latest_timestamp: str | None
    top_terms: list[InsightsTermPayload]
    activity: InsightsActivityPayload
    enrichments: list[InsightsEnrichmentPayload]
    levels: list[InsightsLevelStatusPayload]
    diagnostics: list[ReportDiagnosticPayload]
    next_actions: list[str]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordRef:
    """A stable handle back to a source record or session.

    Maps to the ADR 0004 ``RecordRef`` drilldown concept: enough to
    re-open the record with ``agentgrep search`` / ``find`` without
    embedding the full record text.
    """

    agent: str
    store: str
    path: str
    timestamp: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    snippet: str | None = None

    def to_payload(self) -> RecordRefPayload:
        """Return the JSON-serializable form."""
        return RecordRefPayload(
            agent=self.agent,
            store=self.store,
            path=self.path,
            timestamp=self.timestamp,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            snippet=self.snippet,
        )


@dataclass(frozen=True, slots=True)
class ReportDiagnostic:
    """A non-fatal note about the report run.

    Maps to the ADR 0004 ``Diagnostic`` concept. ``setup_command`` carries
    the precise next command for a missing optional dependency.
    """

    severity: DiagnosticSeverity
    code: str
    message: str
    setup_command: str | None = None

    def to_payload(self) -> ReportDiagnosticPayload:
        """Return the JSON-serializable form."""
        return ReportDiagnosticPayload(
            severity=self.severity,
            code=self.code,
            message=self.message,
            setup_command=self.setup_command,
        )


@dataclass(frozen=True, slots=True)
class InsightsTerm:
    """One frequent term and its raw occurrence count."""

    term: str
    count: int

    def to_payload(self) -> InsightsTermPayload:
        """Return the JSON-serializable form."""
        return InsightsTermPayload(term=self.term, count=self.count)


@dataclass(frozen=True, slots=True)
class InsightsTimelineBucket:
    """Per-day activity bucket."""

    date: str
    record_count: int
    agents: dict[str, int]
    top_terms: tuple[InsightsTerm, ...]

    def to_payload(self) -> InsightsTimelineBucketPayload:
        """Return the JSON-serializable form."""
        return InsightsTimelineBucketPayload(
            date=self.date,
            record_count=self.record_count,
            agents=dict(self.agents),
            top_terms=[term.to_payload() for term in self.top_terms],
        )


@dataclass(frozen=True, slots=True)
class InsightsWorkArea:
    """A coarse work area inferred from session/conversation/path grouping."""

    label: str
    record_count: int
    agents: dict[str, int]
    stores: dict[str, int]
    top_terms: tuple[InsightsTerm, ...]

    def to_payload(self) -> InsightsWorkAreaPayload:
        """Return the JSON-serializable form."""
        return InsightsWorkAreaPayload(
            label=self.label,
            record_count=self.record_count,
            agents=dict(self.agents),
            stores=dict(self.stores),
            top_terms=[term.to_payload() for term in self.top_terms],
        )


@dataclass(frozen=True, slots=True)
class InsightsOpenThread:
    """A candidate unanswered thread (a trailing question, by heuristic)."""

    title: str
    agent: str
    store: str
    reason: str
    ref: RecordRef
    timestamp: str | None = None
    session_id: str | None = None

    def to_payload(self) -> InsightsOpenThreadPayload:
        """Return the JSON-serializable form."""
        return InsightsOpenThreadPayload(
            title=self.title,
            agent=self.agent,
            store=self.store,
            timestamp=self.timestamp,
            session_id=self.session_id,
            reason=self.reason,
            ref=self.ref.to_payload(),
        )


@dataclass(frozen=True, slots=True)
class InsightsCoverage:
    """Which metadata surfaces were present across the analyzed records."""

    records_with_timestamp: int
    records_with_session_identity: int
    metadata_fields: dict[str, int]

    def to_payload(self) -> InsightsCoveragePayload:
        """Return the JSON-serializable form."""
        return InsightsCoveragePayload(
            records_with_timestamp=self.records_with_timestamp,
            records_with_session_identity=self.records_with_session_identity,
            metadata_fields=dict(self.metadata_fields),
        )


@dataclass(frozen=True, slots=True)
class InsightsActivity:
    """The deterministic builtin (L0) activity report."""

    summary: str
    records_analyzed: int
    activity_units: int
    sampled: bool
    work_areas: tuple[InsightsWorkArea, ...]
    timeline: tuple[InsightsTimelineBucket, ...]
    recurring_patterns: tuple[InsightsTerm, ...]
    repeated_instructions: tuple[str, ...]
    open_threads: tuple[InsightsOpenThread, ...]
    coverage: InsightsCoverage

    def to_payload(self) -> InsightsActivityPayload:
        """Return the JSON-serializable form."""
        return InsightsActivityPayload(
            summary=self.summary,
            records_analyzed=self.records_analyzed,
            activity_units=self.activity_units,
            sampled=self.sampled,
            work_areas=[area.to_payload() for area in self.work_areas],
            timeline=[bucket.to_payload() for bucket in self.timeline],
            recurring_patterns=[term.to_payload() for term in self.recurring_patterns],
            repeated_instructions=list(self.repeated_instructions),
            open_threads=[thread.to_payload() for thread in self.open_threads],
            coverage=self.coverage.to_payload(),
        )


@dataclass(frozen=True, slots=True)
class InsightsEnrichment:
    """The output of one optional enricher attached to the report."""

    level: InsightsLevel
    backend: str
    status: t.Literal["ok", "skipped", "error"]
    message: str
    data: dict[str, t.Any] = field(default_factory=dict)
    provenance: dict[str, t.Any] | None = None

    def to_payload(self) -> InsightsEnrichmentPayload:
        """Return the JSON-serializable form."""
        return InsightsEnrichmentPayload(
            level=self.level,
            backend=self.backend,
            status=self.status,
            message=self.message,
            data=self.data,
            provenance=self.provenance,
        )


@dataclass(frozen=True, slots=True)
class InsightsLevelStatus:
    """Availability of one enrichment level, for ``levels``/``doctor``."""

    level: InsightsLevel
    available: bool
    backend: str | None
    reason: str
    setup_command: str | None = None

    def to_payload(self) -> InsightsLevelStatusPayload:
        """Return the JSON-serializable form."""
        return InsightsLevelStatusPayload(
            level=self.level,
            available=self.available,
            backend=self.backend,
            reason=self.reason,
            setup_command=self.setup_command,
        )


@dataclass(frozen=True, slots=True)
class ReportRequest:
    """Normalized inputs for one report run."""

    scope: str = "prompts"
    requested_level: RequestedLevel = "builtin"
    record_limit: int | None = 500
    model: str | None = None
    llm_backend: str = "ollama"
    index_backend: str = "tantivy"
    allow_download: bool = False
    include_text: bool = False


@dataclass(frozen=True, slots=True)
class InsightsReport:
    """A complete report: deterministic facts plus optional enrichments."""

    status: ReportStatusName
    scope: str
    requested_level: RequestedLevel
    level: InsightsLevel
    records_analyzed: int
    record_limit: int | None
    sampled: bool
    agents: dict[str, int]
    stores: dict[str, int]
    kinds: dict[str, int]
    earliest_timestamp: str | None
    latest_timestamp: str | None
    top_terms: tuple[InsightsTerm, ...]
    activity: InsightsActivity
    enrichments: tuple[InsightsEnrichment, ...] = ()
    levels: tuple[InsightsLevelStatus, ...] = ()
    diagnostics: tuple[ReportDiagnostic, ...] = ()
    next_actions: tuple[str, ...] = ()

    def to_payload(self) -> InsightsReportPayload:
        """Return the JSON-serializable report payload (``schema_version`` included)."""
        return InsightsReportPayload(
            schema_version=SCHEMA_VERSION,
            status=self.status,
            scope=self.scope,
            requested_level=self.requested_level,
            level=self.level,
            records_analyzed=self.records_analyzed,
            record_limit=self.record_limit,
            sampled=self.sampled,
            agents=dict(self.agents),
            stores=dict(self.stores),
            kinds=dict(self.kinds),
            earliest_timestamp=self.earliest_timestamp,
            latest_timestamp=self.latest_timestamp,
            top_terms=[term.to_payload() for term in self.top_terms],
            activity=self.activity.to_payload(),
            enrichments=[enr.to_payload() for enr in self.enrichments],
            levels=[status.to_payload() for status in self.levels],
            diagnostics=[diag.to_payload() for diag in self.diagnostics],
            next_actions=list(self.next_actions),
        )
