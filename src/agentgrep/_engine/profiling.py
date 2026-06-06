"""Engine-only profiling helpers.

The profiler mirrors a small subset of OpenTelemetry's shape: spans
with scalar attributes and an in-memory export payload, without adding
a runtime dependency or emitting logs from library code.
"""

from __future__ import annotations

import collections
import collections.abc as cabc
import contextlib
import contextvars
import dataclasses
import pathlib
import subprocess
import time
import typing as t

if t.TYPE_CHECKING:
    import agentgrep
    from agentgrep.query.compile import CompiledQuery

type ProfileAttribute = str | int | float | bool | None
type ProfileAttributes = dict[str, ProfileAttribute]
type ProfilePayload = dict[str, ProfileAttribute | list[dict[str, object]]]
type FindProfileType = t.Literal["prompts", "history", "sessions", "all"]


@dataclasses.dataclass(frozen=True, slots=True)
class EnginePhaseSample:
    """One profiled engine phase or subprocess call."""

    name: str
    duration_seconds: float
    attributes: ProfileAttributes = dataclasses.field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-ready payload for this sample."""
        return {
            "name": self.name,
            "duration_seconds": self.duration_seconds,
            "attributes": dict(self.attributes),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class EngineProfile:
    """Immutable snapshot of collected engine profile samples."""

    samples: tuple[EnginePhaseSample, ...]

    def to_payload(self) -> ProfilePayload:
        """Return a JSON-ready payload for this profile."""
        return {"samples": [sample.to_payload() for sample in self.samples]}


@dataclasses.dataclass(slots=True)
class EngineProfiler:
    """In-memory span recorder for one engine run."""

    _samples: list[EnginePhaseSample] = dataclasses.field(default_factory=list)

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        **attributes: ProfileAttribute,
    ) -> cabc.Iterator[None]:
        """Record elapsed time for a named phase."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                name,
                time.perf_counter() - start,
                **attributes,
            )

    def record(
        self,
        name: str,
        duration_seconds: float,
        **attributes: ProfileAttribute,
    ) -> None:
        """Append one already-timed sample."""
        self._samples.append(
            EnginePhaseSample(
                name=name,
                duration_seconds=max(0.0, duration_seconds),
                attributes=dict(attributes),
            ),
        )

    def snapshot(self) -> EngineProfile:
        """Return an immutable profile snapshot."""
        return EngineProfile(samples=tuple(self._samples))


_ACTIVE_PROFILER: contextvars.ContextVar[EngineProfiler | None] = contextvars.ContextVar(
    "agentgrep_engine_profiler",
    default=None,
)


def current_engine_profiler() -> EngineProfiler | None:
    """Return the active engine profiler for this context, if any."""
    return _ACTIVE_PROFILER.get()


@contextlib.contextmanager
def use_engine_profiler(profiler: EngineProfiler) -> cabc.Iterator[None]:
    """Make ``profiler`` active for nested engine calls."""
    token = _ACTIVE_PROFILER.set(profiler)
    try:
        yield
    finally:
        _ACTIVE_PROFILER.reset(token)


def record_subprocess_run(
    command: cabc.Sequence[str],
    *,
    duration_seconds: float,
    completed: subprocess.CompletedProcess[str],
) -> None:
    """Record a subprocess run without storing argv or paths."""
    profiler = current_engine_profiler()
    if profiler is None:
        return
    profiler.record(
        "subprocess.run",
        duration_seconds,
        agentgrep_tool=_command_family(command),
        agentgrep_returncode=completed.returncode,
        agentgrep_stdout_bytes=len(completed.stdout.encode()),
        agentgrep_stderr_bytes=len(completed.stderr.encode()),
    )


def _command_family(command: cabc.Sequence[str]) -> str:
    """Return a coarse command family for profiler attributes."""
    if not command:
        return "unknown"
    name = pathlib.Path(command[0]).name
    if name == "fdfind":
        return "fd"
    if name == "jaq":
        return "jq"
    return name


@dataclasses.dataclass(frozen=True, slots=True)
class ProfiledSearchResult:
    """Search results plus phase timings and source counts."""

    records: tuple[agentgrep.SearchRecord, ...]
    profile: EngineProfile
    discovered_source_count: int
    planned_source_count: int

    @property
    def result_count(self) -> int:
        """Return the number of collected records."""
        return len(self.records)

    def to_payload(self) -> dict[str, object]:
        """Return a privacy-safe JSON-ready summary."""
        return {
            "kind": "search",
            "result_count": self.result_count,
            "discovered_source_count": self.discovered_source_count,
            "planned_source_count": self.planned_source_count,
            "profile": self.profile.to_payload(),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ProfiledFindResult:
    """Find results plus phase timings and source counts."""

    records: tuple[agentgrep.FindRecord, ...]
    profile: EngineProfile
    discovered_source_count: int

    @property
    def result_count(self) -> int:
        """Return the number of collected records."""
        return len(self.records)

    def to_payload(self) -> dict[str, object]:
        """Return a privacy-safe JSON-ready summary."""
        return {
            "kind": "find",
            "result_count": self.result_count,
            "discovered_source_count": self.discovered_source_count,
            "profile": self.profile.to_payload(),
        }


def profile_search_query(
    home: pathlib.Path,
    query: agentgrep.SearchQuery,
    *,
    backends: agentgrep.BackendSelection | None = None,
    control: agentgrep.SearchControl | None = None,
) -> ProfiledSearchResult:
    """Run a search query and return engine-only phase timings."""
    import agentgrep

    profiler = EngineProfiler()
    active_backends = agentgrep.select_backends() if backends is None else backends
    active_control = agentgrep.SearchControl() if control is None else control
    with use_engine_profiler(profiler):
        with profiler.span(
            "search.discover",
            agentgrep_scope=query.scope,
            agentgrep_agent_count=len(query.agents),
        ):
            sources = agentgrep.discover_sources_for_search(
                home,
                query,
                active_backends,
                version_detail="none",
            )
        _record_source_groups(profiler, "search.discover.group", sources)
        source_predicate = query.compiled.source_predicate if query.compiled is not None else None
        if source_predicate is not None:
            sources_for_plan = [source for source in sources if source_predicate(source)]
        else:
            sources_for_plan = sources
        with profiler.span(
            "search.plan",
            agentgrep_scope=query.scope,
            agentgrep_source_count=len(sources_for_plan),
        ):
            planned_sources = agentgrep.plan_search_sources(
                query,
                sources_for_plan,
                active_backends,
                control=active_control,
            )
        if active_control.answer_now_requested():
            records: list[agentgrep.SearchRecord] = []
        else:
            with profiler.span(
                "search.collect",
                agentgrep_scope=query.scope,
                agentgrep_source_count=len(planned_sources),
            ):
                records = agentgrep.collect_search_records(
                    query,
                    planned_sources,
                    control=active_control,
                )
    return ProfiledSearchResult(
        records=tuple(records),
        profile=profiler.snapshot(),
        discovered_source_count=len(sources),
        planned_source_count=len(planned_sources),
    )


def profile_find_query(
    home: pathlib.Path,
    agents: tuple[agentgrep.AgentName, ...],
    *,
    pattern: str | None,
    limit: int | None,
    backends: agentgrep.BackendSelection | None = None,
    type_filter: FindProfileType = "all",
    compiled: CompiledQuery | None = None,
) -> ProfiledFindResult:
    """Run a find query and return engine-only phase timings."""
    import agentgrep

    profiler = EngineProfiler()
    active_backends = agentgrep.select_backends() if backends is None else backends
    with use_engine_profiler(profiler):
        with profiler.span(
            "find.discover",
            agentgrep_agent_count=len(agents),
            agentgrep_type_filter=type_filter,
        ):
            sources = agentgrep.discover_sources(
                home,
                agents,
                active_backends,
                version_detail="none",
                store_roles=agentgrep.find_store_roles_for_type_filter(type_filter),
            )
        _record_source_groups(profiler, "find.discover.group", sources)
        with profiler.span(
            "find.filter",
            agentgrep_source_count=len(sources),
            agentgrep_type_filter=type_filter,
        ):
            records = _profile_find_records(
                sources,
                pattern=pattern,
                limit=limit,
                type_filter=type_filter,
                compiled=compiled,
                profiler=profiler,
            )
    return ProfiledFindResult(
        records=tuple(records),
        profile=profiler.snapshot(),
        discovered_source_count=len(sources),
    )


def _profile_find_records(
    sources: cabc.Sequence[agentgrep.SourceHandle],
    *,
    pattern: str | None,
    limit: int | None,
    type_filter: FindProfileType,
    compiled: CompiledQuery | None,
    profiler: EngineProfiler,
) -> list[agentgrep.FindRecord]:
    """Build filtered ``find`` records for profiling."""
    import agentgrep

    query = pattern.casefold() if pattern is not None else None
    source_predicate = compiled.source_predicate if compiled is not None else None
    results: list[agentgrep.FindRecord] = []
    for source in sources:
        started_at = time.perf_counter()
        matched = False
        if source_predicate is not None and not source_predicate(source):
            _record_find_source_sample(profiler, source, started_at, matched=matched)
            continue
        record = agentgrep.FindRecord(
            kind="find",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            path_kind=source.path_kind,
            metadata={"source_kind": source.source_kind},
        )
        if not _find_type_matches(record, type_filter):
            _record_find_source_sample(profiler, source, started_at, matched=matched)
            continue
        if query is not None and query not in _find_record_haystack(record):
            _record_find_source_sample(profiler, source, started_at, matched=matched)
            continue
        matched = True
        _record_find_source_sample(profiler, source, started_at, matched=matched)
        results.append(record)
        if limit is not None and len(results) >= limit:
            break
    return results


def _record_source_groups(
    profiler: EngineProfiler,
    name: str,
    sources: cabc.Sequence[agentgrep.SourceHandle],
) -> None:
    """Record aggregate source discovery groups without source paths."""
    groups: collections.Counter[tuple[str, str, str, str, str]] = collections.Counter(
        (
            source.agent,
            source.store,
            source.adapter_id,
            source.path_kind,
            source.source_kind,
        )
        for source in sources
    )
    for (
        agent,
        store,
        adapter_id,
        path_kind,
        source_kind,
    ), source_count in sorted(groups.items()):
        profiler.record(
            name,
            0.0,
            agentgrep_agent=agent,
            agentgrep_store=store,
            agentgrep_adapter_id=adapter_id,
            agentgrep_path_kind=path_kind,
            agentgrep_source_kind=source_kind,
            agentgrep_source_count=source_count,
        )


def _record_find_source_sample(
    profiler: EngineProfiler,
    source: agentgrep.SourceHandle,
    started_at: float,
    *,
    matched: bool,
) -> None:
    """Record one profiled find-source filter decision."""
    import agentgrep

    profiler.record(
        "find.filter.source",
        time.perf_counter() - started_at,
        **agentgrep._source_profile_attributes(source),
        agentgrep_matched=matched,
    )


def _find_type_matches(
    record: agentgrep.FindRecord,
    type_filter: FindProfileType,
) -> bool:
    """Return whether ``record`` survives the profiling type filter.

    ``history`` and ``prompts`` both map to the ``history_file`` path
    kind on purpose: the prompt/history split is a record-level concept
    (``search`` ``--scope``), while ``find`` filters at file granularity.
    Mirrors :func:`agentgrep.cli.render._type_matches`.
    """
    if type_filter == "all":
        return True
    expected_path_kind = {
        "sessions": "session_file",
        "history": "history_file",
        "prompts": "history_file",
    }[type_filter]
    return record.path_kind == expected_path_kind


def _find_record_haystack(record: agentgrep.FindRecord) -> str:
    """Return the casefolded substring-search surface for a find record."""
    return " ".join(
        (
            record.agent,
            record.store,
            record.adapter_id,
            str(record.path),
            record.path_kind,
        ),
    ).casefold()
