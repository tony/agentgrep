"""Pure-Python insights report helpers."""

from __future__ import annotations

import collections
import collections.abc as cabc
import contextlib
import dataclasses
import importlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import types
import typing as t
import urllib.error
import urllib.parse
import urllib.request

import agentgrep
from agentgrep.insights_loader import (
    BackendConfigurationError,
    BackendLoadError,
    BackendPolicy,
    BackendRuntimeError,
    BackendUnavailable,
    ImportModule,
    LoadedBackend,
    load_backend_modules,
)

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
InsightsReportFormat = t.Literal["text", "markdown", "html"]
InsightsLLMBackend = t.Literal["auto", "llama-cpp", "ollama", "litert-lm"]
ConcreteInsightsLLMBackend = t.Literal["llama-cpp", "ollama", "litert-lm"]
InsightsIndexBackend = t.Literal["auto", "tantivy", "sqlite-vec"]
ModuleProbe = cabc.Callable[[str], bool]

import_module_for_backend: ImportModule = importlib.import_module

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
_OLLAMA_CONNECT_TIMEOUT_SECONDS = 5.0
_OLLAMA_WRITE_TIMEOUT_SECONDS = 30.0
_OLLAMA_POOL_TIMEOUT_SECONDS = 5.0
_LITERT_LM_FALLBACK_MAX_CHUNKS = 64
_LITERT_LM_FALLBACK_MAX_CHARS = 2048
_LLM_BACKEND_IMPORT_PATHS: dict[ConcreteInsightsLLMBackend, tuple[str, ...]] = {
    "llama-cpp": ("llama_cpp",),
    "ollama": ("httpx",),
    "litert-lm": ("litert_lm",),
}


class InsightsProgress(t.Protocol):
    """Progress callbacks for optional report enrichment."""

    def llm_started(self, *, backend: str, model: str, endpoint: str) -> None:
        """Report that a local LLM request is starting."""

    def llm_waiting(self, *, backend: str, model: str, endpoint: str) -> None:
        """Report that a local LLM request is waiting for tokens."""

    def llm_chunk(
        self,
        *,
        backend: str,
        model: str,
        chunk_count: int,
        char_count: int,
    ) -> None:
        """Report one or more streamed response chunks."""

    def llm_finished(
        self,
        *,
        backend: str,
        model: str,
        chunk_count: int,
        char_count: int,
    ) -> None:
        """Report that local LLM streaming has finished."""


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


class InsightsEnrichmentPayload(t.TypedDict):
    """JSON payload for one optional report enrichment."""

    level: str
    backend: str
    status: str
    message: str
    data: dict[str, object]


class InsightsLLMModelPayload(t.TypedDict):
    """JSON payload for one curated local LLM model."""

    backend: str
    model: str
    family: str
    steward: str
    jurisdiction: str
    license: str
    access: str
    source_url: str
    artifact_filename: str | None
    local_model_id: str | None
    install_hint: str
    report_hint: str
    notes: str


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
    enrichments: list[InsightsEnrichmentPayload]


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
        if setup_level == "llm":
            setup_command = "agentgrep insights setup llm"
        elif setup_level is not None:
            setup_command = f"agentgrep insights setup {setup_level} --install --yes"
        else:
            setup_command = None
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
class InsightsEnrichment:
    """One optional enrichment attached to an insights report."""

    level: str
    backend: str
    status: str
    message: str
    data: dict[str, object]

    def to_payload(self) -> InsightsEnrichmentPayload:
        """Return the JSON-compatible representation."""
        return {
            "level": self.level,
            "backend": self.backend,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class InsightsLLMModelSpec:
    """One curated local LLM model that agentgrep is willing to suggest."""

    backend: ConcreteInsightsLLMBackend
    model: str
    family: str
    steward: str
    jurisdiction: str
    license: str
    access: str
    source_url: str
    install_hint: str
    report_hint: str
    notes: str
    artifact_filename: str | None = None
    local_model_id: str | None = None

    def to_payload(self) -> InsightsLLMModelPayload:
        """Return the JSON-compatible representation."""
        return {
            "backend": self.backend,
            "model": self.model,
            "family": self.family,
            "steward": self.steward,
            "jurisdiction": self.jurisdiction,
            "license": self.license,
            "access": self.access,
            "source_url": self.source_url,
            "artifact_filename": self.artifact_filename,
            "local_model_id": self.local_model_id,
            "install_hint": self.install_hint,
            "report_hint": self.report_hint,
            "notes": self.notes,
        }


class InsightsModelInstallError(RuntimeError):
    """Raised when a curated insights model cannot be installed."""

    def __init__(self, detail: str, *, examples: cabc.Sequence[str] = ()) -> None:
        super().__init__(detail)
        self.detail = detail
        self.examples = tuple(examples)

    def __str__(self) -> str:
        """Return an actionable CLI error message."""
        if not self.examples:
            return self.detail
        lines = [self.detail, "Try:"]
        lines.extend(f"  {example}" for example in self.examples)
        return "\n".join(lines)


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
    enrichments: tuple[InsightsEnrichment, ...] = ()

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
            "enrichments": [enrichment.to_payload() for enrichment in self.enrichments],
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
        dependencies=(
            "httpx>=0.28",
            "llama-cpp-python>=0.3.28",
            "litert-lm-api>=0.13.1",
        ),
        modules=("llama_cpp", "httpx", "litert_lm"),
        description="Local narrative synthesis through embedded or local HTTP backends.",
        model_behavior="explicit local model or endpoint only",
    ),
)

_LITERT_LM_REPORT_HINT = (
    "agentgrep insights report --level llm --llm-backend litert-lm --model /path/to/model.litertlm"
)

_CURATED_LLM_MODELS: tuple[InsightsLLMModelSpec, ...] = (
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="litert-community/gemma-4-E2B-it-litert-lm",
        family="Gemma 4",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Apache-2.0",
        access="public",
        source_url="https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm",
        install_hint=(
            "agentgrep insights models install --llm-backend litert-lm "
            "litert-community/gemma-4-E2B-it-litert-lm --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Smallest default US LiteRT-LM suggestion from the allowlist.",
        artifact_filename="gemma-4-E2B-it.litertlm",
        local_model_id="gemma4-e2b",
    ),
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="litert-community/gemma-4-E4B-it-litert-lm",
        family="Gemma 4",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Apache-2.0",
        access="public",
        source_url="https://huggingface.co/litert-community/gemma-4-E4B-it-litert-lm",
        install_hint=(
            "agentgrep insights models install --llm-backend litert-lm "
            "litert-community/gemma-4-E4B-it-litert-lm --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Larger Apache-2.0 LiteRT-LM Gemma option.",
        artifact_filename="gemma-4-E4B-it.litertlm",
        local_model_id="gemma4-e4b",
    ),
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="litert-community/gemma-4-12B-it-litert-lm",
        family="Gemma 4",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Apache-2.0",
        access="public",
        source_url="https://huggingface.co/litert-community/gemma-4-12B-it-litert-lm",
        install_hint=(
            "agentgrep insights models install --llm-backend litert-lm "
            "litert-community/gemma-4-12B-it-litert-lm --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Heavier Apache-2.0 LiteRT-LM Gemma option.",
        artifact_filename="gemma-4-12B-it.litertlm",
        local_model_id="gemma4-12b",
    ),
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="google/gemma-3n-E2B-it-litert-lm",
        family="Gemma 3n",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Gemma Terms",
        access="gated",
        source_url="https://huggingface.co/google/gemma-3n-E2B-it-litert-lm",
        install_hint=(
            "Accept the gated Hugging Face terms, then run: "
            "agentgrep insights models install --llm-backend litert-lm "
            "google/gemma-3n-E2B-it-litert-lm --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Official Google LiteRT-LM model; requires license acceptance.",
        artifact_filename="gemma-3n-E2B-it-int4.litertlm",
        local_model_id="gemma3n-e2b",
    ),
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="google/gemma-3n-E4B-it-litert-lm",
        family="Gemma 3n",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Gemma Terms",
        access="gated",
        source_url="https://huggingface.co/google/gemma-3n-E4B-it-litert-lm",
        install_hint=(
            "Accept the gated Hugging Face terms, then run: "
            "agentgrep insights models install --llm-backend litert-lm "
            "google/gemma-3n-E4B-it-litert-lm --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Larger official Google LiteRT-LM model; requires license acceptance.",
        artifact_filename="gemma-3n-E4B-it-int4.litertlm",
        local_model_id="gemma3n-e4b",
    ),
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="litert-community/Gemma3-1B-IT",
        family="Gemma 3",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Gemma Terms",
        access="gated",
        source_url="https://huggingface.co/litert-community/Gemma3-1B-IT",
        install_hint=(
            "Accept the gated Hugging Face terms, then run: "
            "agentgrep insights models install --llm-backend litert-lm "
            "litert-community/Gemma3-1B-IT --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Small Gemma 3 LiteRT-LM option; requires license acceptance.",
        artifact_filename="gemma3-1b-it-int4.litertlm",
        local_model_id="gemma3-1b-it",
    ),
    InsightsLLMModelSpec(
        backend="litert-lm",
        model="litert-community/Phi-4-mini-instruct",
        family="Phi 4 Mini",
        steward="Microsoft/Phi",
        jurisdiction="US",
        license="MIT",
        access="public",
        source_url="https://huggingface.co/litert-community/Phi-4-mini-instruct",
        install_hint=(
            "agentgrep insights models install --llm-backend litert-lm "
            "litert-community/Phi-4-mini-instruct --yes"
        ),
        report_hint=_LITERT_LM_REPORT_HINT,
        notes="Permissive Microsoft LiteRT-LM option.",
        artifact_filename="Phi-4-mini-instruct_multi-prefill-seq_q8_ekv4096.litertlm",
        local_model_id="phi4-mini-instruct",
    ),
    InsightsLLMModelSpec(
        backend="ollama",
        model="gemma3n:e2b",
        family="Gemma 3n",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Gemma Terms",
        access="public",
        source_url="https://ollama.com/library/gemma3n:e2b",
        install_hint="ollama pull gemma3n:e2b",
        report_hint=(
            "agentgrep insights report --level llm --llm-backend ollama --model gemma3n:e2b"
        ),
        notes="Smallest curated Google model in the Ollama allowlist.",
    ),
    InsightsLLMModelSpec(
        backend="ollama",
        model="gemma3n:e4b",
        family="Gemma 3n",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Gemma Terms",
        access="public",
        source_url="https://ollama.com/library/gemma3n:e4b",
        install_hint="ollama pull gemma3n:e4b",
        report_hint=(
            "agentgrep insights report --level llm --llm-backend ollama --model gemma3n:e4b"
        ),
        notes="Larger curated Google model in the Ollama allowlist.",
    ),
    InsightsLLMModelSpec(
        backend="ollama",
        model="gemma3:1b",
        family="Gemma 3",
        steward="Google/Gemma",
        jurisdiction="US",
        license="Gemma Terms",
        access="public",
        source_url="https://ollama.com/library/gemma3:1b",
        install_hint="ollama pull gemma3:1b",
        report_hint=(
            "agentgrep insights report --level llm --llm-backend ollama --model gemma3:1b"
        ),
        notes="Small Gemma option available through Ollama.",
    ),
    InsightsLLMModelSpec(
        backend="ollama",
        model="phi4-mini",
        family="Phi 4 Mini",
        steward="Microsoft/Phi",
        jurisdiction="US",
        license="MIT",
        access="public",
        source_url="https://ollama.com/library/phi4-mini",
        install_hint="ollama pull phi4-mini",
        report_hint=(
            "agentgrep insights report --level llm --llm-backend ollama --model phi4-mini"
        ),
        notes="Permissive Microsoft model available through Ollama.",
    ),
)


def build_report(
    records: cabc.Iterable[agentgrep.SearchRecord],
    *,
    scope: agentgrep.SearchScope,
    requested_level: InsightsLevel,
    record_limit: int | None,
    sampled: bool,
    model: str | None = None,
    model_cache: pathlib.Path | None = None,
    allow_download: bool = False,
    llm_backend: InsightsLLMBackend = "auto",
    llm_endpoint: str = "http://127.0.0.1:11434",
    allow_network: bool = False,
    index_backend: InsightsIndexBackend = "auto",
    import_module_for_backend: ImportModule | None = None,
    progress: InsightsProgress | None = None,
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
    policy = BackendPolicy(allow_download=allow_download, allow_network=allow_network)
    importer = import_module_for_backend or globals()["import_module_for_backend"]
    effective_level = _resolve_report_level(
        requested_level,
        model=model,
        llm_backend=llm_backend,
        importer=importer,
        policy=policy,
    )
    enrichments: tuple[InsightsEnrichment, ...] = ()
    if effective_level != "builtin":
        enrichments = (
            _build_enrichment(
                effective_level,
                record_list,
                top_terms=top_terms,
                model=model,
                model_cache=model_cache,
                llm_backend=llm_backend,
                llm_endpoint=llm_endpoint,
                index_backend=index_backend,
                importer=importer,
                policy=policy,
                progress=progress,
            ),
        )

    return InsightsReport(
        level=effective_level,
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
        skipped_enrichers=_skipped_enrichers(requested_level, effective_level),
        enrichments=enrichments,
    )


def inspect_levels(probe: ModuleProbe | None = None) -> tuple[InsightsLevelStatus, ...]:
    """Probe optional insights levels without importing optional packages."""
    if probe is None:
        probe = _module_available
    return tuple(_inspect_level(spec, probe=probe) for spec in INSIGHTS_LEVEL_SPECS)


def list_llm_model_specs(
    llm_backend: InsightsLLMBackend = "auto",
) -> tuple[InsightsLLMModelSpec, ...]:
    """Return curated local LLM model suggestions for one backend.

    The registry is intentionally static and local. It does not import optional
    LLM packages, contact Hugging Face, or query an Ollama daemon.
    """
    if llm_backend == "auto":
        return _CURATED_LLM_MODELS
    return tuple(spec for spec in _CURATED_LLM_MODELS if spec.backend == llm_backend)


def resolve_llm_model_spec(
    model: str,
    *,
    llm_backend: InsightsLLMBackend,
) -> InsightsLLMModelSpec:
    """Return one curated model spec or raise an actionable install error."""
    candidates = [
        spec
        for spec in list_llm_model_specs(llm_backend)
        if spec.model == model or spec.local_model_id == model
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        detail = f"Model {model!r} is ambiguous across curated insights backends."
        raise InsightsModelInstallError(
            detail,
            examples=("agentgrep insights models list",),
        )
    backend_hint = "" if llm_backend == "auto" else f" for backend {llm_backend!r}"
    detail = f"No curated insights model {model!r}{backend_hint}."
    raise InsightsModelInstallError(
        detail,
        examples=(
            "agentgrep insights models list --llm-backend litert-lm",
            "agentgrep insights models list --llm-backend ollama",
        ),
    )


def default_model_cache_dir() -> pathlib.Path:
    """Return agentgrep's default local model cache directory."""
    model_dir = os.environ.get("AGENTGREP_MODEL_DIR")
    if model_dir:
        return pathlib.Path(model_dir).expanduser()
    cache_dir = os.environ.get("AGENTGREP_CACHE_DIR")
    if cache_dir:
        return pathlib.Path(cache_dir).expanduser() / "models"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return pathlib.Path(local_app_data) / "agentgrep" / "models"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Caches" / "agentgrep" / "models"
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return pathlib.Path(xdg_cache_home).expanduser() / "agentgrep" / "models"
    return pathlib.Path.home() / ".cache" / "agentgrep" / "models"


def litert_lm_model_install_target(
    model: str,
    *,
    model_cache: pathlib.Path | None,
    model_id: str | None = None,
) -> pathlib.Path:
    """Return the local path for one curated LiteRT-LM model artifact."""
    spec = resolve_llm_model_spec(model, llm_backend="litert-lm")
    artifact = _litert_lm_artifact_filename(spec)
    cache_root = model_cache.expanduser() if model_cache is not None else default_model_cache_dir()
    return cache_root / "litert-lm" / _safe_model_cache_key(model_id or spec.model) / artifact


def litert_lm_download_url(spec: InsightsLLMModelSpec) -> str:
    """Return the Hugging Face download URL for a curated LiteRT-LM model."""
    artifact = _litert_lm_artifact_filename(spec)
    quoted_artifact = urllib.parse.quote(artifact, safe="/")
    return f"https://huggingface.co/{spec.model}/resolve/main/{quoted_artifact}"


def install_litert_lm_model(
    model: str,
    *,
    model_cache: pathlib.Path | None,
    model_id: str | None = None,
) -> pathlib.Path:
    """Download a curated LiteRT-LM artifact into the agentgrep model cache."""
    spec = resolve_llm_model_spec(model, llm_backend="litert-lm")
    target_path = litert_lm_model_install_target(
        spec.model,
        model_cache=model_cache,
        model_id=model_id,
    )
    if target_path.is_file():
        _write_model_install_manifest(target_path, spec=spec, model_id=model_id)
        return target_path

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_name(target_path.name + ".tmp")
    headers = _hugging_face_download_headers()
    request = urllib.request.Request(litert_lm_download_url(spec), headers=headers)
    try:
        with urllib.request.urlopen(request) as response, temporary_path.open("wb") as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
    except urllib.error.HTTPError as exc:
        _remove_partial_download(temporary_path)
        if exc.code in {401, 403}:
            detail = (
                f"Could not download gated Hugging Face model {spec.model!r}. "
                "Accept the model terms and set HF_TOKEN before retrying."
            )
            example = (
                "HF_TOKEN=... agentgrep insights models install "
                f"--llm-backend litert-lm {spec.model} --yes"
            )
            raise InsightsModelInstallError(
                detail,
                examples=(
                    "hf auth login",
                    example,
                ),
            ) from exc
        detail = f"Could not download {spec.model!r}: HTTP {exc.code} {exc.reason}"
        raise InsightsModelInstallError(
            detail,
            examples=(spec.source_url,),
        ) from exc
    except urllib.error.URLError as exc:
        _remove_partial_download(temporary_path)
        detail = f"Could not download {spec.model!r}: {exc.reason}"
        raise InsightsModelInstallError(
            detail,
            examples=(spec.source_url,),
        ) from exc

    temporary_path.replace(target_path)
    _write_model_install_manifest(target_path, spec=spec, model_id=model_id)
    return target_path


def _litert_lm_artifact_filename(spec: InsightsLLMModelSpec) -> str:
    if spec.backend != "litert-lm" or spec.artifact_filename is None:
        detail = f"Curated model {spec.model!r} does not have a LiteRT-LM artifact."
        raise InsightsModelInstallError(
            detail,
            examples=("agentgrep insights models list --llm-backend litert-lm",),
        )
    return spec.artifact_filename


def _hugging_face_download_headers() -> dict[str, str]:
    token = os.environ.get("HF_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _safe_model_cache_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "--", value).strip("-")
    return key or "model"


def _write_model_install_manifest(
    model_path: pathlib.Path,
    *,
    spec: InsightsLLMModelSpec,
    model_id: str | None,
) -> None:
    manifest_path = model_path.with_name(model_path.name + ".agentgrep.json")
    payload = {
        "backend": spec.backend,
        "model": spec.model,
        "local_model_id": model_id or spec.local_model_id,
        "artifact_filename": spec.artifact_filename,
        "source_url": spec.source_url,
        "license": spec.license,
        "access": spec.access,
        "path": str(model_path),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_partial_download(path: pathlib.Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def build_setup_plan(
    level: InsightsSetupLevel,
    *,
    manager: InsightsInstallManager,
    llm_backend: InsightsLLMBackend = "auto",
) -> InsightsSetupPlan:
    """Build the install command for one optional insights extra."""
    spec = _setup_spec(level)
    extra = _setup_extra(spec, llm_backend=llm_backend)
    if extra is None:  # pragma: no cover - guarded by _setup_spec
        msg = f"{level!r} is not an installable insights level"
        raise ValueError(msg)
    resolved_manager = _resolve_install_manager(manager)
    package_spec = f"agentgrep[{extra}]"
    if resolved_manager == "uv":
        command = ("uv", "pip", "install", package_spec)
    else:
        command = (sys.executable, "-m", "pip", "install", package_spec)
    return InsightsSetupPlan(
        level=level,
        extra=extra,
        manager=resolved_manager,
        command=command,
        command_text=format_install_command(command),
    )


def format_install_command(command: cabc.Sequence[str]) -> str:
    """Return a stable display form for an install command."""
    return " ".join(_quote_install_arg(argument) for argument in command)


def render_report_document(
    payload: InsightsReportPayload,
    *,
    report_format: InsightsReportFormat,
) -> str:
    """Render a report payload as a terminal-adjacent document format."""
    if report_format == "text":
        msg = "text reports are rendered by the CLI"
        raise ValueError(msg)
    if report_format == "markdown":
        return _render_markdown_report(payload)
    html = _html_from_enrichments(payload["enrichments"])
    if html is None:
        level = "html"
        raise BackendConfigurationError(
            level,
            requirement="an HTML report enrichment",
            examples=("agentgrep insights report --level html --format html",),
        )
    return html


def _iter_tokens(text: str) -> cabc.Iterator[str]:
    """Yield normalized report tokens from record text."""
    for match in _TOKEN_RE.finditer(text.casefold()):
        token = match.group(0)
        if token in _STOPWORDS:
            continue
        yield token


def _resolve_report_level(
    requested_level: InsightsLevel,
    *,
    model: str | None,
    llm_backend: InsightsLLMBackend,
    importer: ImportModule,
    policy: BackendPolicy,
) -> str:
    """Return the effective report level for a requested optional level."""
    if requested_level == "builtin":
        return "builtin"
    if requested_level == "best-installed":
        for level in ("llm", "index", "embeddings", "ml", "html"):
            if _level_is_usable(
                level,
                model=model,
                llm_backend=llm_backend,
                importer=importer,
                policy=policy,
            ):
                return level
        return "builtin"
    setup_level = requested_level
    _assert_level_is_usable(
        setup_level,
        model=model,
        llm_backend=llm_backend,
        importer=importer,
        policy=policy,
    )
    return requested_level


def _level_is_usable(
    level: InsightsSetupLevel,
    *,
    model: str | None,
    llm_backend: InsightsLLMBackend,
    importer: ImportModule,
    policy: BackendPolicy,
) -> bool:
    try:
        _assert_level_is_usable(
            level,
            model=model,
            llm_backend=llm_backend,
            importer=importer,
            policy=policy,
        )
    except BackendConfigurationError, BackendLoadError, BackendUnavailable:
        return False
    return True


def _assert_level_is_usable(
    level: InsightsSetupLevel,
    *,
    model: str | None,
    llm_backend: InsightsLLMBackend,
    importer: ImportModule,
    policy: BackendPolicy,
) -> None:
    if level == "llm":
        llm_runtime_backend = _resolve_llm_runtime_backend(
            model,
            llm_backend=llm_backend,
        )
        if llm_runtime_backend is None:
            raise _llm_configuration_error()
        _ = _load_llm_backend(llm_runtime_backend, importer=importer)
        return
    _ = _load_level_backend(level, importer=importer)
    if level == "embeddings" and not _model_is_usable(model, policy=policy):
        raise _embedding_configuration_error()


def _build_enrichment(
    level: str,
    records: cabc.Sequence[agentgrep.SearchRecord],
    *,
    top_terms: cabc.Sequence[InsightsTerm],
    model: str | None,
    model_cache: pathlib.Path | None,
    llm_backend: InsightsLLMBackend,
    llm_endpoint: str,
    index_backend: InsightsIndexBackend,
    importer: ImportModule,
    policy: BackendPolicy,
    progress: InsightsProgress | None,
) -> InsightsEnrichment:
    setup_level = t.cast("InsightsSetupLevel", level)
    if setup_level == "html":
        return _build_html_enrichment(records, top_terms=top_terms, importer=importer)
    if setup_level == "ml":
        return _build_ml_enrichment(records, importer=importer)
    if setup_level == "embeddings":
        return _build_embeddings_enrichment(
            records,
            model=model,
            model_cache=model_cache,
            importer=importer,
            policy=policy,
        )
    if setup_level == "index":
        return _build_index_enrichment(
            records,
            index_backend=index_backend,
            importer=importer,
        )
    return _build_llm_enrichment(
        records,
        top_terms=top_terms,
        model=model,
        llm_backend=llm_backend,
        llm_endpoint=llm_endpoint,
        importer=importer,
        policy=policy,
        progress=progress,
    )


def _build_html_enrichment(
    records: cabc.Sequence[agentgrep.SearchRecord],
    *,
    top_terms: cabc.Sequence[InsightsTerm],
    importer: ImportModule,
) -> InsightsEnrichment:
    backend = _load_level_backend("html", importer=importer)
    jinja2 = backend.require("jinja2")
    template_factory = t.cast("type[t.Any]", t.cast("t.Any", jinja2).Template)
    template = template_factory(
        "<!doctype html><title>Insights report</title>"
        "<h1>Insights report</h1>"
        "<p>{{ records_analyzed }} records analyzed.</p>",
    )
    html = t.cast(
        "str",
        template.render(
            records_analyzed=len(records),
            top_terms=[term.to_payload() for term in top_terms],
        ),
    )
    return InsightsEnrichment(
        level="html",
        backend="jinja2",
        status="applied",
        message="Rendered an HTML report document.",
        data={"html": html, "records_rendered": len(records)},
    )


def _build_ml_enrichment(
    records: cabc.Sequence[agentgrep.SearchRecord],
    *,
    importer: ImportModule,
) -> InsightsEnrichment:
    backend = _load_level_backend("ml", importer=importer)
    text_module = backend.require("sklearn.feature_extraction.text")
    cluster_module = backend.require("sklearn.cluster")
    texts = [record.text for record in records if record.text.strip()]
    if not texts:
        topics: list[dict[str, object]] = []
    else:
        text_module_any = t.cast("t.Any", text_module)
        cluster_module_any = t.cast("t.Any", cluster_module)
        vectorizer_factory = t.cast("type[t.Any]", text_module_any.TfidfVectorizer)
        vectorizer = vectorizer_factory(max_features=50, stop_words="english")
        matrix = vectorizer.fit_transform(texts)
        n_clusters = max(1, min(3, len(texts)))
        cluster_factory = t.cast("type[t.Any]", cluster_module_any.MiniBatchKMeans)
        labels = list(
            t.cast(
                "cabc.Iterable[int]",
                cluster_factory(
                    n_clusters=n_clusters,
                    random_state=0,
                    n_init="auto",
                ).fit_predict(matrix),
            ),
        )
        features = [str(feature) for feature in vectorizer.get_feature_names_out()]
        topic_counts = collections.Counter(labels)
        topics = [
            {
                "label": f"topic-{label + 1}",
                "size": count,
                "top_terms": features[:5],
            }
            for label, count in sorted(topic_counts.items())
        ]
    return InsightsEnrichment(
        level="ml",
        backend="scikit-learn",
        status="applied",
        message="Computed TF-IDF topic candidates with classical ML.",
        data={"topics": topics},
    )


def _build_embeddings_enrichment(
    records: cabc.Sequence[agentgrep.SearchRecord],
    *,
    model: str | None,
    model_cache: pathlib.Path | None,
    importer: ImportModule,
    policy: BackendPolicy,
) -> InsightsEnrichment:
    if not _model_is_usable(model, policy=policy):
        raise _embedding_configuration_error()
    backend = _load_level_backend("embeddings", importer=importer)
    module = backend.require("sentence_transformers")
    transformer_factory = t.cast("type[t.Any]", t.cast("t.Any", module).SentenceTransformer)
    transformer = transformer_factory(
        t.cast("str", model),
        cache_folder=str(model_cache) if model_cache is not None else None,
        local_files_only=not policy.allow_download,
    )
    texts = [record.text for record in records if record.text.strip()]
    embeddings = list(t.cast("cabc.Iterable[object]", transformer.encode(texts)))
    dimensions = _embedding_dimensions(embeddings)
    return InsightsEnrichment(
        level="embeddings",
        backend="sentence-transformers",
        status="applied",
        message="Computed offline semantic embedding groups.",
        data={
            "model": model,
            "embedding_dimensions": dimensions,
            "semantic_groups": [
                {
                    "label": "semantic-group-1",
                    "size": len(embeddings),
                },
            ]
            if embeddings
            else [],
        },
    )


def _build_index_enrichment(
    records: cabc.Sequence[agentgrep.SearchRecord],
    *,
    index_backend: InsightsIndexBackend,
    importer: ImportModule,
) -> InsightsEnrichment:
    _ = index_backend
    backend = _load_level_backend("index", importer=importer)
    sqlite_vec = backend.require("sqlite_vec")
    tantivy = backend.require("tantivy")
    sqlite_version = _load_sqlite_vec_version(sqlite_vec)
    documents_indexed, segments = _build_tantivy_index_summary(tantivy, records)
    return InsightsEnrichment(
        level="index",
        backend="tantivy+sqlite-vec",
        status="applied",
        message="Built transient local text/vector index summaries.",
        data={
            "documents_indexed": documents_indexed,
            "tantivy_segments": segments,
            "sqlite_vec_version": sqlite_version,
        },
    )


def _build_llm_enrichment(
    records: cabc.Sequence[agentgrep.SearchRecord],
    *,
    top_terms: cabc.Sequence[InsightsTerm],
    model: str | None,
    llm_backend: InsightsLLMBackend,
    llm_endpoint: str,
    importer: ImportModule,
    policy: BackendPolicy,
    progress: InsightsProgress | None,
) -> InsightsEnrichment:
    runtime_backend = _resolve_llm_runtime_backend(model, llm_backend=llm_backend)
    if runtime_backend is None:
        raise _llm_configuration_error()
    if runtime_backend == "llama-cpp":
        backend = _load_llm_backend("llama-cpp", importer=importer)
        summary = _summarize_with_llama_cpp(
            backend,
            model=t.cast("str", model),
            records=records,
            top_terms=top_terms,
        )
        return InsightsEnrichment(
            level="llm",
            backend="llama-cpp",
            status="applied",
            message="Synthesized a local narrative with llama-cpp-python.",
            data={"summary": summary, "model": model},
        )
    if runtime_backend == "litert-lm":
        backend = _load_llm_backend("litert-lm", importer=importer)
        summary = _summarize_with_litert_lm(
            backend,
            model=t.cast("str", model),
            records=records,
            top_terms=top_terms,
            progress=progress,
        )
        return InsightsEnrichment(
            level="llm",
            backend="litert-lm",
            status="applied",
            message="Synthesized a local narrative with LiteRT-LM.",
            data={"summary": summary, "model": model},
        )
    if runtime_backend == "ollama" and _ollama_is_allowed(
        llm_endpoint,
        policy=policy,
    ):
        backend = _load_llm_backend("ollama", importer=importer)
        summary = _summarize_with_ollama(
            backend,
            model=t.cast("str", model),
            endpoint=llm_endpoint,
            records=records,
            top_terms=top_terms,
            progress=progress,
        )
        return InsightsEnrichment(
            level="llm",
            backend="ollama",
            status="applied",
            message="Synthesized a local narrative with Ollama.",
            data={"summary": summary, "model": model, "endpoint": llm_endpoint},
        )
    raise _llm_configuration_error()


def _embedding_configuration_error() -> BackendConfigurationError:
    return BackendConfigurationError(
        "embeddings",
        requirement="local embedding model path or explicit download permission",
        examples=(
            "agentgrep insights report --level embeddings --model /path/to/model",
            "agentgrep insights report --level embeddings --model all-MiniLM-L6-v2 "
            "--allow-download",
        ),
    )


def _llm_configuration_error() -> BackendConfigurationError:
    return BackendConfigurationError(
        "llm",
        requirement="local .gguf model path, local .litertlm model path, or Ollama model name",
        examples=(
            "agentgrep insights report --level llm --model /path/to/model.gguf",
            "agentgrep insights report --level llm --llm-backend litert-lm "
            "--model /path/to/model.litertlm",
            "agentgrep insights report --level llm --llm-backend ollama --model llama3",
        ),
    )


def _load_level_backend(level: InsightsSetupLevel, *, importer: ImportModule) -> LoadedBackend:
    return load_backend_modules(level, _backend_import_paths(level), import_module=importer)


def _load_llm_backend(
    backend: ConcreteInsightsLLMBackend,
    *,
    importer: ImportModule,
) -> LoadedBackend:
    return load_backend_modules(
        f"llm-{backend}",
        _LLM_BACKEND_IMPORT_PATHS[backend],
        import_module=importer,
    )


def _backend_import_paths(level: InsightsSetupLevel) -> tuple[str, ...]:
    if level == "html":
        return ("jinja2", "platformdirs")
    if level == "ml":
        return ("sklearn", "sklearn.feature_extraction.text", "sklearn.cluster")
    if level == "embeddings":
        return ("sentence_transformers",)
    if level == "index":
        return ("sqlite_vec", "tantivy")
    return tuple(module for modules in _LLM_BACKEND_IMPORT_PATHS.values() for module in modules)


def _model_is_usable(model: str | None, *, policy: BackendPolicy) -> bool:
    return _local_path_exists(model) or (policy.allow_download and bool(model))


def _resolve_llm_runtime_backend(
    model: str | None,
    *,
    llm_backend: InsightsLLMBackend,
) -> ConcreteInsightsLLMBackend | None:
    if llm_backend == "auto":
        if _local_path_exists(model):
            return "litert-lm" if _is_litert_lm_model_path(model) else "llama-cpp"
        return "ollama" if model else None
    concrete_backend = llm_backend
    if concrete_backend == "ollama":
        return concrete_backend if bool(model) and not _local_path_exists(model) else None
    return concrete_backend if _local_path_exists(model) else None


def _is_litert_lm_model_path(value: str | None) -> bool:
    return bool(value) and pathlib.Path(t.cast("str", value)).suffix == ".litertlm"


def _local_path_exists(value: str | None) -> bool:
    return bool(value) and pathlib.Path(t.cast("str", value)).expanduser().exists()


def _ollama_is_allowed(endpoint: str, *, policy: BackendPolicy) -> bool:
    parsed = urllib.parse.urlparse(endpoint)
    hostname = parsed.hostname or ""
    if hostname in {"127.0.0.1", "::1", "localhost"}:
        return True
    return policy.allow_network


def _summarize_with_llama_cpp(
    backend: LoadedBackend,
    *,
    model: str,
    records: cabc.Sequence[agentgrep.SearchRecord],
    top_terms: cabc.Sequence[InsightsTerm],
) -> str:
    module = backend.require("llama_cpp")
    llama_factory = t.cast("type[t.Any]", t.cast("t.Any", module).Llama)
    llama = llama_factory(model_path=model, n_ctx=2048, verbose=False)
    response = llama.create_chat_completion(
        messages=[
            {
                "role": "system",
                "content": "Summarize local aggregate agentgrep report facts.",
            },
            {"role": "user", "content": _llm_prompt(records=records, top_terms=top_terms)},
        ],
        temperature=0.0,
        max_tokens=256,
    )
    return _extract_llm_summary(response)


def _summarize_with_litert_lm(
    backend: LoadedBackend,
    *,
    model: str,
    records: cabc.Sequence[agentgrep.SearchRecord],
    top_terms: cabc.Sequence[InsightsTerm],
    progress: InsightsProgress | None,
) -> str:
    module = backend.require("litert_lm")
    _configure_litert_lm_logging(module)
    module_any = t.cast("t.Any", module)
    engine_factory = t.cast("type[t.Any]", module_any.Engine)
    backend_factory = module_any.Backend.CPU
    sampler_factory = t.cast("type[t.Any] | None", getattr(module, "SamplerConfig", None))
    model_path = str(pathlib.Path(model).expanduser())
    try:
        _notify_llm_started(
            progress,
            backend="litert-lm",
            model=model_path,
            endpoint="local",
        )
        conversation_kwargs: dict[str, object] = {
            "system_message": "Summarize local aggregate agentgrep report facts.",
        }
        if sampler_factory is not None:
            conversation_kwargs["sampler_config"] = sampler_factory(temperature=0.0)
        with (
            engine_factory(
                model_path,
                backend=backend_factory(),
                max_num_tokens=2048,
            ) as engine,
            engine.create_conversation(**conversation_kwargs) as conversation,
        ):
            _notify_llm_waiting(
                progress,
                backend="litert-lm",
                model=model_path,
                endpoint="local",
            )
            chunks = _send_litert_lm_message_async(
                conversation,
                _llm_prompt(records=records, top_terms=top_terms),
            )
            return _extract_litert_lm_stream_summary(
                chunks,
                conversation=conversation,
                model=model_path,
                progress=progress,
            )
    except BackendRuntimeError:
        raise
    except Exception as exc:
        raise _litert_lm_runtime_error(
            model=model_path,
            detail=f"LiteRT-LM execution failed: {_exception_detail(exc)}",
        ) from exc


def _configure_litert_lm_logging(module: types.ModuleType) -> None:
    severity = getattr(getattr(module, "LogSeverity", None), "ERROR", None)
    set_min_log_severity = getattr(module, "set_min_log_severity", None)
    if callable(set_min_log_severity) and severity is not None:
        set_min_log_severity(severity)


def _send_litert_lm_message_async(
    conversation: object,
    prompt: str,
) -> cabc.Iterable[object]:
    send_message_async = t.cast("t.Any", conversation).send_message_async
    try:
        return t.cast(
            "cabc.Iterable[object]",
            send_message_async(prompt, max_output_tokens=256),
        )
    except TypeError as exc:
        if "max_output_tokens" not in _exception_detail(exc):
            raise
        return t.cast("cabc.Iterable[object]", send_message_async(prompt))


def _summarize_with_ollama(
    backend: LoadedBackend,
    *,
    model: str,
    endpoint: str,
    records: cabc.Sequence[agentgrep.SearchRecord],
    top_terms: cabc.Sequence[InsightsTerm],
    progress: InsightsProgress | None,
) -> str:
    httpx = backend.require("httpx")
    client_factory = t.cast("type[t.Any]", t.cast("t.Any", httpx).Client)
    url = endpoint.rstrip("/") + "/api/chat"
    try:
        with client_factory(timeout=_ollama_http_timeout(httpx)) as client:
            _notify_llm_started(
                progress,
                backend="ollama",
                model=model,
                endpoint=endpoint,
            )
            with client.stream(
                "POST",
                url,
                json={
                    "model": model,
                    "stream": True,
                    "messages": [
                        {
                            "role": "user",
                            "content": _llm_prompt(records=records, top_terms=top_terms),
                        },
                    ],
                },
            ) as response:
                response.raise_for_status()
                _notify_llm_waiting(
                    progress,
                    backend="ollama",
                    model=model,
                    endpoint=endpoint,
                )
                return _extract_ollama_stream_summary(
                    response,
                    endpoint=endpoint,
                    model=model,
                    progress=progress,
                )
    except Exception as exc:
        if _is_module_exception(exc, httpx, "ConnectTimeout"):
            raise _ollama_runtime_error(
                endpoint=endpoint,
                model=model,
                detail=(
                    f"could not connect to {endpoint} within "
                    f"{_OLLAMA_CONNECT_TIMEOUT_SECONDS:g}s: {_exception_detail(exc)}"
                ),
            ) from exc
        if _is_module_exception(exc, httpx, "ReadTimeout"):
            raise _ollama_runtime_error(
                endpoint=endpoint,
                model=model,
                detail=f"timed out waiting for Ollama response from {endpoint}: "
                f"{_exception_detail(exc)}",
            ) from exc
        if _is_module_exception(exc, httpx, "TimeoutException"):
            raise _ollama_runtime_error(
                endpoint=endpoint,
                model=model,
                detail=f"timed out while contacting {endpoint}: {_exception_detail(exc)}",
            ) from exc
        if _is_module_exception(exc, httpx, "HTTPError"):
            raise _ollama_runtime_error(
                endpoint=endpoint,
                model=model,
                detail=f"request to {endpoint} failed: {_exception_detail(exc)}",
            ) from exc
        raise


def _ollama_http_timeout(httpx: types.ModuleType) -> object:
    timeout_factory = getattr(httpx, "Timeout", None)
    if not callable(timeout_factory):
        return 60.0
    return timeout_factory(
        connect=_OLLAMA_CONNECT_TIMEOUT_SECONDS,
        read=None,
        write=_OLLAMA_WRITE_TIMEOUT_SECONDS,
        pool=_OLLAMA_POOL_TIMEOUT_SECONDS,
    )


def _extract_litert_lm_stream_summary(
    chunks: cabc.Iterable[object],
    *,
    conversation: object,
    model: str,
    progress: InsightsProgress | None,
) -> str:
    parts: list[str] = []
    chunk_count = 0
    char_count = 0
    chunk_iterator = iter(chunks)
    for chunk in chunk_iterator:
        content = _litert_lm_chunk_content(chunk)
        if not content:
            continue
        chunk_count += 1
        parts.append(content)
        char_count += len(content)
        _notify_llm_chunk(
            progress,
            backend="litert-lm",
            model=model,
            chunk_count=chunk_count,
            char_count=char_count,
        )
        if (
            chunk_count >= _LITERT_LM_FALLBACK_MAX_CHUNKS
            or char_count >= _LITERT_LM_FALLBACK_MAX_CHARS
        ):
            _cancel_and_drain_litert_lm_stream(conversation, chunk_iterator)
            break
    summary = "".join(parts).strip()
    if not summary:
        raise _litert_lm_runtime_error(
            model=model,
            detail="empty response from LiteRT-LM model",
        )
    _notify_llm_finished(
        progress,
        backend="litert-lm",
        model=model,
        chunk_count=chunk_count,
        char_count=char_count,
    )
    return summary


def _cancel_and_drain_litert_lm_stream(
    conversation: object,
    chunks: cabc.Iterator[object],
) -> None:
    cancel_process = getattr(conversation, "cancel_process", None)
    if callable(cancel_process):
        cancel_process()
    try:
        for _ in chunks:
            pass
    except RuntimeError as exc:
        detail = _exception_detail(exc)
        if "CANCELLED" not in detail and "Max number of tokens reached" not in detail:
            raise


def _litert_lm_chunk_content(chunk: object) -> str:
    if not isinstance(chunk, cabc.Mapping):
        return ""
    chunk_mapping = t.cast("cabc.Mapping[str, object]", chunk)
    content = chunk_mapping.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, cabc.Sequence) or isinstance(content, bytes | str):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, cabc.Mapping):
            item_mapping = t.cast("cabc.Mapping[str, object]", item)
            text = item_mapping.get("text") if item_mapping.get("type") == "text" else None
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _extract_ollama_stream_summary(
    response: object,
    *,
    endpoint: str,
    model: str,
    progress: InsightsProgress | None,
) -> str:
    parts: list[str] = []
    last_payload: dict[str, object] | None = None
    chunk_count = 0
    char_count = 0
    response_any = t.cast("t.Any", response)
    for raw_line in response_any.iter_lines():
        payload = _parse_ollama_stream_line(raw_line, endpoint=endpoint, model=model)
        if not payload:
            continue
        last_payload = payload
        _raise_for_ollama_stream_error(payload, endpoint=endpoint, model=model)
        chunk_count += 1
        content = _ollama_stream_content(payload)
        if content:
            parts.append(content)
            char_count += len(content)
        _notify_llm_chunk(
            progress,
            backend="ollama",
            model=model,
            chunk_count=chunk_count,
            char_count=char_count,
        )
    _notify_llm_finished(
        progress,
        backend="ollama",
        model=model,
        chunk_count=chunk_count,
        char_count=char_count,
    )
    summary = "".join(parts)
    if summary:
        return summary
    if last_payload is not None:
        return _extract_llm_summary(last_payload)
    return ""


def _parse_ollama_stream_line(
    raw_line: object,
    *,
    endpoint: str,
    model: str,
) -> dict[str, object]:
    if isinstance(raw_line, bytes):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ollama_runtime_error(
                endpoint=endpoint,
                model=model,
                detail=(f"invalid streaming response from {endpoint}: {_exception_detail(exc)}"),
            ) from exc
    elif isinstance(raw_line, str):
        line = raw_line
    else:
        line = str(raw_line)
    line = line.strip()
    if not line:
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _ollama_runtime_error(
            endpoint=endpoint,
            model=model,
            detail=f"invalid streaming response from {endpoint}: {_exception_detail(exc)}",
        ) from exc
    if not isinstance(payload, dict):
        raise _ollama_runtime_error(
            endpoint=endpoint,
            model=model,
            detail=f"invalid streaming response from {endpoint}: expected JSON object",
        )
    return t.cast("dict[str, object]", payload)


def _raise_for_ollama_stream_error(
    payload: dict[str, object],
    *,
    endpoint: str,
    model: str,
) -> None:
    error = payload.get("error")
    if isinstance(error, str) and error:
        raise _ollama_runtime_error(
            endpoint=endpoint,
            model=model,
            detail=f"streaming response from {endpoint} failed: {error}",
        )


def _ollama_stream_content(payload: dict[str, object]) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        message_map = t.cast("dict[str, object]", message)
        content = message_map.get("content")
        if isinstance(content, str):
            return content
    response = payload.get("response")
    if isinstance(response, str):
        return response
    return ""


def _notify_llm_started(
    progress: InsightsProgress | None,
    *,
    backend: str,
    model: str,
    endpoint: str,
) -> None:
    if progress is not None:
        progress.llm_started(backend=backend, model=model, endpoint=endpoint)


def _notify_llm_waiting(
    progress: InsightsProgress | None,
    *,
    backend: str,
    model: str,
    endpoint: str,
) -> None:
    if progress is not None:
        progress.llm_waiting(backend=backend, model=model, endpoint=endpoint)


def _notify_llm_chunk(
    progress: InsightsProgress | None,
    *,
    backend: str,
    model: str,
    chunk_count: int,
    char_count: int,
) -> None:
    if progress is not None:
        progress.llm_chunk(
            backend=backend,
            model=model,
            chunk_count=chunk_count,
            char_count=char_count,
        )


def _notify_llm_finished(
    progress: InsightsProgress | None,
    *,
    backend: str,
    model: str,
    chunk_count: int,
    char_count: int,
) -> None:
    if progress is not None:
        progress.llm_finished(
            backend=backend,
            model=model,
            chunk_count=chunk_count,
            char_count=char_count,
        )


def _is_module_exception(
    exc: BaseException,
    module: types.ModuleType,
    name: str,
) -> bool:
    exception_type = getattr(module, name, None)
    if not isinstance(exception_type, type):
        return False
    try:
        if not issubclass(exception_type, BaseException):
            return False
    except TypeError:
        return False
    return isinstance(exc, exception_type)


def _exception_detail(exc: BaseException) -> str:
    return str(exc).strip() or exc.__class__.__name__


def _ollama_runtime_error(*, endpoint: str, model: str, detail: str) -> BackendRuntimeError:
    return BackendRuntimeError(
        "llm",
        "Ollama",
        detail=detail,
        examples=(
            "ollama serve",
            f"ollama pull {model}",
            f"agentgrep insights report --level llm --llm-backend ollama --model {model}",
        ),
    )


def _litert_lm_runtime_error(*, model: str, detail: str) -> BackendRuntimeError:
    return BackendRuntimeError(
        "llm",
        "LiteRT-LM",
        detail=detail,
        examples=(
            "agentgrep insights setup llm --llm-backend litert-lm --install --yes",
            f"agentgrep insights report --level llm --llm-backend litert-lm --model {model}",
        ),
    )


def _llm_prompt(
    *,
    records: cabc.Sequence[agentgrep.SearchRecord],
    top_terms: cabc.Sequence[InsightsTerm],
) -> str:
    terms = ", ".join(f"{term.term}={term.count}" for term in top_terms[:8]) or "none"
    return f"Records analyzed: {len(records)}. Top terms: {terms}."


def _extract_llm_summary(response: object) -> str:
    if isinstance(response, dict):
        response_map = t.cast("dict[str, object]", response)
        message = response_map.get("message")
        if isinstance(message, dict):
            message_map = t.cast("dict[str, object]", message)
            content = message_map.get("content")
            if isinstance(content, str):
                return content
        choices = response_map.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                first_map = t.cast("dict[str, object]", first)
                first_message = first_map.get("message")
                if isinstance(first_message, dict):
                    first_message_map = t.cast("dict[str, object]", first_message)
                    content = first_message_map.get("content")
                    if isinstance(content, str):
                        return content
                text = first_map.get("text")
                if isinstance(text, str):
                    return text
    return str(response)


def _embedding_dimensions(embeddings: cabc.Sequence[object]) -> int:
    if not embeddings:
        return 0
    first = embeddings[0]
    if isinstance(first, cabc.Sized):
        return len(first)
    return 0


def _load_sqlite_vec_version(sqlite_vec: object) -> str | None:
    connection = sqlite3.connect(":memory:")
    try:
        if hasattr(connection, "enable_load_extension"):
            connection.enable_load_extension(True)
        sqlite_vec_any = t.cast("t.Any", sqlite_vec)
        t.cast("cabc.Callable[[object], object]", sqlite_vec_any.load)(connection)
        if hasattr(connection, "enable_load_extension"):
            connection.enable_load_extension(False)
        row = connection.execute("select vec_version()").fetchone()
        if row is None:
            return None
        return str(row[0])
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _build_tantivy_index_summary(
    tantivy: object,
    records: cabc.Sequence[agentgrep.SearchRecord],
) -> tuple[int, int]:
    tantivy_any = t.cast("t.Any", tantivy)
    schema_builder = tantivy_any.SchemaBuilder()
    _ = schema_builder.add_text_field("text", stored=True)
    schema = schema_builder.build()
    index = tantivy_any.Index(schema)
    writer = index.writer()
    for record in records:
        document = tantivy_any.Document()
        document.add_text("text", record.text)
        _ = writer.add_document(document)
    _ = writer.commit()
    index.reload()
    searcher = index.searcher()
    return (
        int(getattr(searcher, "num_docs", len(records))),
        int(getattr(searcher, "num_segments", 0)),
    )


def _render_markdown_report(payload: InsightsReportPayload) -> str:
    terms = ", ".join(f"{term['term']}={term['count']}" for term in payload["top_terms"][:8])
    return "\n".join(
        (
            "# Insights report",
            "",
            f"- level: {payload['level']}",
            f"- records analyzed: {payload['records_analyzed']}",
            f"- top terms: {terms or 'none'}",
        ),
    )


def _html_from_enrichments(
    enrichments: cabc.Sequence[InsightsEnrichmentPayload],
) -> str | None:
    for enrichment in enrichments:
        data = enrichment["data"]
        html = data.get("html")
        if isinstance(html, str):
            return html
    return None


def _skipped_enrichers(
    requested_level: InsightsLevel,
    effective_level: str,
) -> tuple[str, ...]:
    """Return skipped optional enrichers for the selected concept level."""
    if effective_level != "builtin":
        return ()
    if requested_level == "builtin":
        return (
            "html templates",
            "classical ML",
            "embeddings",
            "persistent index",
            "local LLM",
        )
    if requested_level == "best-installed":
        return ("no optional insights backend usable under the current offline policy",)
    return (f"{requested_level} backend unavailable",)


def _inspect_level(spec: InsightsLevelSpec, *, probe: ModuleProbe) -> InsightsLevelStatus:
    if spec.level == "llm":
        missing = () if _any_llm_backend_available(probe) else spec.modules
        return InsightsLevelStatus(
            spec=spec,
            installed=not missing,
            missing_modules=missing,
        )
    missing = tuple(module for module in spec.modules if not probe(module))
    return InsightsLevelStatus(
        spec=spec,
        installed=not missing,
        missing_modules=missing,
    )


def _any_llm_backend_available(probe: ModuleProbe) -> bool:
    return any(
        all(probe(module) for module in modules) for modules in _LLM_BACKEND_IMPORT_PATHS.values()
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


def _setup_extra(
    spec: InsightsLevelSpec,
    *,
    llm_backend: InsightsLLMBackend,
) -> str | None:
    if spec.level != "llm" or llm_backend == "auto":
        return spec.extra
    return _llm_backend_extra(llm_backend)


def _llm_backend_extra(backend: ConcreteInsightsLLMBackend) -> str:
    return f"insights-llm-{backend}"


def _resolve_install_manager(manager: InsightsInstallManager) -> ResolvedInsightsInstallManager:
    if manager == "uv" or manager == "pip":
        return manager
    return "uv" if shutil.which("uv") is not None else "pip"


def _quote_install_arg(argument: str) -> str:
    if any(character in argument for character in (" ", "[", "]")):
        return '"' + argument.replace('"', '\\"') + '"'
    return argument
