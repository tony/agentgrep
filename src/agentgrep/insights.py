"""Pure-Python insights report helpers."""

from __future__ import annotations

import collections
import collections.abc as cabc
import dataclasses
import importlib.util
import re
import shutil
import sys
import typing as t

import agentgrep

InsightsSetupLevel = t.Literal["html", "ml", "embeddings", "index", "llm"]
InsightsLevel = t.Literal[
    "builtin",
    "html",
    "ml",
    "embeddings",
    "index",
    "llm",
    "best-installed",
]
InsightsInstallManager = t.Literal["auto", "uv", "pip"]
ResolvedInsightsInstallManager = t.Literal["uv", "pip"]
ModuleProbe = cabc.Callable[[str], bool]

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


class InsightsLevelStatusPayload(t.TypedDict):
    """JSON payload for one optional insights level."""

    level: str
    extra: str | None
    dependencies: list[str]
    modules: list[str]
    installed: bool
    missing_modules: list[str]
    description: str
    model_behavior: str
    setup_command: str | None


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
class InsightsLevelSpec:
    """Static metadata for one insights capability level."""

    level: InsightsLevel
    extra: str | None
    dependencies: tuple[str, ...]
    modules: tuple[str, ...]
    description: str
    model_behavior: str

    @property
    def setup_level(self) -> InsightsSetupLevel | None:
        """Return the setup target for installable optional levels."""
        if self.level in {"html", "ml", "embeddings", "index", "llm"}:
            return t.cast("InsightsSetupLevel", self.level)
        return None


@dataclasses.dataclass(frozen=True, slots=True)
class InsightsLevelStatus:
    """Install status for one insights capability level."""

    spec: InsightsLevelSpec
    installed: bool
    missing_modules: tuple[str, ...]

    def to_payload(self) -> InsightsLevelStatusPayload:
        """Return the JSON-compatible representation."""
        setup_level = self.spec.setup_level
        setup_command = (
            f"agentgrep insights setup {setup_level} --install --yes"
            if setup_level is not None
            else None
        )
        return {
            "level": self.spec.level,
            "extra": self.spec.extra,
            "dependencies": list(self.spec.dependencies),
            "modules": list(self.spec.modules),
            "installed": self.installed,
            "missing_modules": list(self.missing_modules),
            "description": self.spec.description,
            "model_behavior": self.spec.model_behavior,
            "setup_command": setup_command,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class InsightsSetupPlan:
    """Resolved optional-extra install command."""

    level: InsightsSetupLevel
    extra: str
    manager: ResolvedInsightsInstallManager
    command: tuple[str, ...]
    command_text: str


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


INSIGHTS_LEVEL_SPECS: tuple[InsightsLevelSpec, ...] = (
    InsightsLevelSpec(
        level="builtin",
        extra=None,
        dependencies=(),
        modules=(),
        description="Deterministic local reports using the base agentgrep install.",
        model_behavior="no models",
    ),
    InsightsLevelSpec(
        level="html",
        extra="insights-html",
        dependencies=("jinja2>=3.1", "platformdirs>=4"),
        modules=("jinja2", "platformdirs"),
        description="Template-based report rendering and reusable report profiles.",
        model_behavior="no models",
    ),
    InsightsLevelSpec(
        level="ml",
        extra="insights-ml",
        dependencies=("scikit-learn>=1.9",),
        modules=("sklearn",),
        description="Classical TF-IDF features, topic candidates, and clustering.",
        model_behavior="no model downloads",
    ),
    InsightsLevelSpec(
        level="embeddings",
        extra="insights-embeddings",
        dependencies=("sentence-transformers>=5.5",),
        modules=("sentence_transformers",),
        description="Dense and sparse embedding backends for semantic grouping.",
        model_behavior="explicit model install only",
    ),
    InsightsLevelSpec(
        level="index",
        extra="insights-index",
        dependencies=("sqlite-vec>=0.1.9", "tantivy>=0.26"),
        modules=("sqlite_vec", "tantivy"),
        description="Persistent local indexes for repeated report refreshes.",
        model_behavior="reuses installed embedding models only",
    ),
    InsightsLevelSpec(
        level="llm",
        extra="insights-llm",
        dependencies=("llama-cpp-python>=0.3.28", "httpx>=0.28"),
        modules=("llama_cpp", "httpx"),
        description="Local narrative synthesis through embedded or local HTTP backends.",
        model_behavior="explicit local model or endpoint only",
    ),
)


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


def inspect_levels(probe: ModuleProbe | None = None) -> tuple[InsightsLevelStatus, ...]:
    """Probe optional insights levels without importing optional packages."""
    if probe is None:
        probe = _module_available
    return tuple(_inspect_level(spec, probe=probe) for spec in INSIGHTS_LEVEL_SPECS)


def build_setup_plan(
    level: InsightsSetupLevel,
    *,
    manager: InsightsInstallManager,
) -> InsightsSetupPlan:
    """Build the install command for one optional insights extra."""
    spec = _setup_spec(level)
    if spec.extra is None:  # pragma: no cover - guarded by _setup_spec
        msg = f"{level!r} is not an installable insights level"
        raise ValueError(msg)
    resolved_manager = _resolve_install_manager(manager)
    package_spec = f"agentgrep[{spec.extra}]"
    if resolved_manager == "uv":
        command = ("uv", "pip", "install", package_spec)
    else:
        command = (sys.executable, "-m", "pip", "install", package_spec)
    return InsightsSetupPlan(
        level=level,
        extra=spec.extra,
        manager=resolved_manager,
        command=command,
        command_text=format_install_command(command),
    )


def format_install_command(command: cabc.Sequence[str]) -> str:
    """Return a stable display form for an install command."""
    return " ".join(_quote_install_arg(argument) for argument in command)


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


def _inspect_level(spec: InsightsLevelSpec, *, probe: ModuleProbe) -> InsightsLevelStatus:
    missing = tuple(module for module in spec.modules if not probe(module))
    return InsightsLevelStatus(
        spec=spec,
        installed=not missing,
        missing_modules=missing,
    )


def _module_available(name: str) -> bool:
    """Return whether an optional module is importable without importing it."""
    return importlib.util.find_spec(name) is not None


def _setup_spec(level: InsightsSetupLevel) -> InsightsLevelSpec:
    for spec in INSIGHTS_LEVEL_SPECS:
        if spec.level == level:
            return spec
    msg = f"Unknown insights setup level: {level}"
    raise ValueError(msg)


def _resolve_install_manager(manager: InsightsInstallManager) -> ResolvedInsightsInstallManager:
    if manager == "uv" or manager == "pip":
        return manager
    return "uv" if shutil.which("uv") is not None else "pip"


def _quote_install_arg(argument: str) -> str:
    if any(character in argument for character in (" ", "[", "]")):
        return '"' + argument.replace('"', '\\"') + '"'
    return argument
