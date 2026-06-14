"""Report builder: deterministic facts plus optional enrichment.

:func:`build_report` is the single entry point. It is headless and
deterministic first (level 0 always runs); higher levels attach as
:class:`~agentgrep.insights.model.InsightsEnrichment` records only when
their backend is installed and selected.

The ``import_module`` parameter is the load-bearing test seam: production
passes :func:`importlib.import_module`; tests pass a fake that returns
stub backend modules, so the full ladder is exercised in an environment
where none of the heavy libraries are installed.
"""

from __future__ import annotations

import collections
import dataclasses
import typing as t

from agentgrep.insights.activity import build_activity
from agentgrep.insights.model import (
    LEVEL_ORDER,
    InsightsLevel,
    InsightsLevelStatus,
    InsightsReport,
    RecordRef,
    ReportDiagnostic,
    ReportRequest,
    ReportStatusName,
    level_rank,
)

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from agentgrep import SearchRecord
    from agentgrep.insights.loader import BackendPolicy, ImportModule
    from agentgrep.insights.progress import InsightsProgress


def _counter_dict(values: cabc.Iterable[str]) -> dict[str, int]:
    """Return a plain ``dict`` of occurrence counts, ordered most-common-first."""
    counter: collections.Counter[str] = collections.Counter(values)
    return dict(counter.most_common())


def _timestamp_bounds(
    records: cabc.Sequence[SearchRecord],
) -> tuple[str | None, str | None]:
    """Return the lexicographic min/max ISO timestamps present in ``records``."""
    stamps = sorted(r.timestamp for r in records if r.timestamp)
    if not stamps:
        return None, None
    return stamps[0], stamps[-1]


def _resolve_level(
    requested: str,
    levels: cabc.Sequence[InsightsLevelStatus],
) -> tuple[InsightsLevel, bool]:
    """Return ``(effective_level, fell_back)`` for a requested level.

    ``best-installed`` selects the highest available rung without ever
    installing. A concrete request that is unavailable degrades to
    ``builtin`` and reports ``fell_back=True`` so the caller can attach a
    diagnostic with the precise setup command.
    """
    available = {status.level for status in levels if status.available}
    if requested == "best-installed":
        # Never auto-select the LLM level: it depends on an external daemon
        # (Ollama) whose reachability the import probe cannot see, and ADR 0005
        # keeps level 5 out of the "all stable levels" set. Request it explicitly.
        best: InsightsLevel = "builtin"
        for level in LEVEL_ORDER:
            if level == "llm":
                continue
            if level == "builtin" or level in available:
                best = level
        return best, False
    concrete = t.cast("InsightsLevel", requested)
    if concrete == "builtin":
        return "builtin", False
    if concrete in available:
        return concrete, False
    return "builtin", True


def _next_actions(
    effective_level: InsightsLevel,
    levels: cabc.Sequence[InsightsLevelStatus],
    has_records: bool,
) -> tuple[str, ...]:
    """Return grounded follow-up commands for the agentic loop."""
    actions: list[str] = ["agentgrep insights levels"]
    if not has_records:
        actions.append("agentgrep insights doctor")
        return tuple(actions)
    # Suggest the next installable rung above the current effective level.
    rank = level_rank(effective_level)
    for status in levels:
        if level_rank(status.level) > rank and not status.available and status.setup_command:
            actions.append(status.setup_command)
            break
    else:
        nxt = level_rank(effective_level) + 1
        if nxt < len(LEVEL_ORDER):
            actions.append(f"agentgrep insights report --level {LEVEL_ORDER[nxt]}")
    return tuple(actions)


def _status(
    has_records: bool,
    fell_back: bool,
    enrichment_skipped: bool,
) -> ReportStatusName:
    """Classify the run outcome into the coarse status vocabulary."""
    if not has_records:
        return "empty"
    if fell_back or enrichment_skipped:
        return "partial"
    return "ok"


def build_report(
    records: cabc.Iterable[SearchRecord],
    request: ReportRequest | None = None,
    *,
    import_module: ImportModule | None = None,
    policy: BackendPolicy | None = None,
    progress: InsightsProgress | None = None,
    model_cache: pathlib.Path | None = None,
) -> InsightsReport:
    """Build an insights report from a record stream.

    Parameters
    ----------
    records
        Normalized :class:`agentgrep.SearchRecord` objects (any
        duck-typed equivalent works).
    request
        Normalized report inputs. Defaults to a builtin, prompt-scope,
        500-record run.
    import_module
        Injectable importer for backend resolution (test seam). Defaults
        to :func:`importlib.import_module`.
    policy
        Download/network policy for model-backed levels.
    progress
        Optional phase progress sink.
    model_cache
        Override for the model artifact cache directory.

    Returns
    -------
    agentgrep.insights.model.InsightsReport
        Deterministic facts plus any attached enrichment.
    """
    from agentgrep.insights import enrichers
    from agentgrep.insights.loader import BackendPolicy

    active_request = request or ReportRequest()
    active_policy = policy or BackendPolicy(
        allow_download=active_request.allow_download,
        allow_network=active_request.allow_download,
    )
    materialized: list[SearchRecord] = list(records)
    sampled = (
        active_request.record_limit is not None and len(materialized) >= active_request.record_limit
    )

    activity = build_activity(materialized, sampled=sampled)
    earliest, latest = _timestamp_bounds(materialized)
    levels = enrichers.probe_levels(active_request, import_module=import_module)

    effective_level, fell_back = _resolve_level(active_request.requested_level, levels)

    diagnostics: list[ReportDiagnostic] = []
    if fell_back:
        target = next(
            (s for s in levels if s.level == active_request.requested_level),
            None,
        )
        diagnostics.append(
            ReportDiagnostic(
                severity="warning",
                code="level-unavailable",
                message=(
                    f"requested level {active_request.requested_level!r} is "
                    f"unavailable; produced the builtin report instead"
                ),
                setup_command=target.setup_command if target else None,
            )
        )

    report = InsightsReport(
        status="ok",
        scope=active_request.scope,
        requested_level=active_request.requested_level,
        level=effective_level,
        records_analyzed=len(materialized),
        record_limit=active_request.record_limit,
        sampled=sampled,
        agents=_counter_dict(str(r.agent) for r in materialized),
        stores=_counter_dict(r.store for r in materialized),
        kinds=_counter_dict(str(r.kind) for r in materialized),
        earliest_timestamp=earliest,
        latest_timestamp=latest,
        top_terms=activity.recurring_patterns,
        activity=activity,
        levels=levels,
    )

    enrichment_skipped = False
    if effective_level != "builtin":
        enrichment, extra_diagnostics = enrichers.run_level(
            effective_level,
            report=report,
            records=materialized,
            request=active_request,
            policy=active_policy,
            import_module=import_module,
            progress=progress,
            model_cache=model_cache,
        )
        diagnostics.extend(extra_diagnostics)
        enrichment_skipped = enrichment.status != "ok"
        report = dataclasses.replace(report, enrichments=(enrichment,))

    has_records = bool(materialized)
    return dataclasses.replace(
        report,
        status=_status(has_records, fell_back, enrichment_skipped),
        diagnostics=tuple(diagnostics),
        next_actions=_next_actions(effective_level, levels, has_records),
    )


def representative_refs(
    report: InsightsReport,
    *,
    limit: int = 5,
) -> tuple[RecordRef, ...]:
    """Return representative drilldown handles from a report's open threads.

    A convenience for renderers and MCP callers that want a few
    :class:`RecordRef` handles without re-deriving them.
    """
    refs = tuple(thread.ref for thread in report.activity.open_threads[:limit])
    return refs
