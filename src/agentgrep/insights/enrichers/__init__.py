"""Enricher registry, capability probes, and dispatch.

This package owns the per-level backend requirements and the selection
logic (preference order, requested index backend first). Backend builder
implementations live in sibling modules and receive already-loaded
modules through :class:`EnricherContext`, so they never ``import`` a heavy
dependency directly — keeping the whole ladder testable with injected
fakes.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from agentgrep.insights.loader import (
    BackendError,
    BackendUnavailable,
    load_modules,
    probe_modules,
)
from agentgrep.insights.model import (
    LEVEL_ORDER,
    InsightsEnrichment,
    InsightsLevel,
    InsightsLevelStatus,
    ReportDiagnostic,
)

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from agentgrep import SearchRecord
    from agentgrep.insights.loader import BackendPolicy, ImportModule
    from agentgrep.insights.model import InsightsReport, ReportRequest
    from agentgrep.insights.progress import InsightsProgress


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """One candidate backend for a level: its modules and setup command."""

    name: str
    modules: tuple[str, ...]
    setup_command: str
    builder: str  # dotted attribute on the level's builder module


@dataclass(frozen=True, slots=True)
class EnricherContext:
    """Everything a backend builder needs to produce an enrichment."""

    level: InsightsLevel
    backend: str
    request: ReportRequest
    records: cabc.Sequence[SearchRecord]
    report: InsightsReport
    modules: dict[str, t.Any]
    policy: BackendPolicy
    import_module: ImportModule | None
    progress: InsightsProgress | None
    model_cache: pathlib.Path | None


# Builder modules are imported lazily by name to keep this registry import-cheap.
_BUILDER_MODULE: dict[InsightsLevel, str] = {
    "html": "agentgrep.insights.enrichers.html",
    "ml": "agentgrep.insights.enrichers.ml",
    "embeddings": "agentgrep.insights.enrichers.embeddings",
    "index": "agentgrep.insights.enrichers.index",
    "graph": "agentgrep.insights.enrichers.graph",
    "llm": "agentgrep.insights.enrichers.llm",
}

_BACKENDS: dict[InsightsLevel, tuple[BackendSpec, ...]] = {
    "html": (
        BackendSpec(
            name="jinja2",
            modules=("jinja2",),
            setup_command="uv pip install 'agentgrep[insights-html]'",
            builder="build_html",
        ),
    ),
    "ml": (
        BackendSpec(
            name="scikit-learn",
            modules=("sklearn",),
            setup_command="uv pip install 'agentgrep[insights-ml]'",
            builder="build_ml",
        ),
    ),
    "embeddings": (
        BackendSpec(
            name="sentence-transformers",
            modules=("sentence_transformers", "numpy"),
            setup_command="uv pip install 'agentgrep[insights-embeddings-st]'",
            builder="build_embeddings",
        ),
        BackendSpec(
            name="model2vec",
            modules=("model2vec", "numpy"),
            setup_command="uv pip install 'agentgrep[insights-embeddings]'",
            builder="build_embeddings",
        ),
    ),
    "index": (
        BackendSpec(
            name="tantivy+sqlite-vec",
            modules=("tantivy", "sqlite_vec", "numpy"),
            setup_command="uv pip install 'agentgrep[insights-index]'",
            builder="build_index",
        ),
        BackendSpec(
            name="lancedb",
            modules=("lancedb", "numpy"),
            setup_command="uv pip install 'agentgrep[insights-index-lancedb]'",
            builder="build_index",
        ),
    ),
    "graph": (
        BackendSpec(
            name="sentence-transformers",
            modules=("sentence_transformers", "sqlite_vec", "numpy"),
            setup_command="uv pip install 'agentgrep[insights-graph-st]'",
            builder="build_graph",
        ),
        BackendSpec(
            name="model2vec",
            modules=("model2vec", "sqlite_vec", "numpy"),
            setup_command="uv pip install 'agentgrep[insights-graph]'",
            builder="build_graph",
        ),
    ),
    "llm": (
        BackendSpec(
            name="ollama",
            modules=("httpx",),
            setup_command="uv pip install 'agentgrep[insights-llm]'",
            builder="build_llm",
        ),
        BackendSpec(
            name="litert-lm",
            modules=("litert_lm",),
            setup_command="uv pip install 'agentgrep[insights-llm-litert]'",
            builder="build_llm",
        ),
        BackendSpec(
            name="transformers",
            modules=("torch", "transformers"),
            setup_command="uv pip install 'agentgrep[insights-llm-transformers]'",
            builder="build_llm",
        ),
    ),
}


def _ordered_backends(level: InsightsLevel, request: ReportRequest) -> tuple[BackendSpec, ...]:
    """Return candidate backends for ``level`` in selection-preference order.

    For the index level, the user's ``index_backend`` choice is tried
    first; all other levels keep their declared preference order
    (sentence-transformers before model2vec, etc.).
    """
    backends = _BACKENDS.get(level, ())
    if level == "index" and request.index_backend == "lancedb":
        return tuple(sorted(backends, key=lambda b: 0 if b.name == "lancedb" else 1))
    if level == "llm" and request.llm_backend not in ("auto", ""):
        preferred = request.llm_backend
        return tuple(sorted(backends, key=lambda b: 0 if b.name == preferred else 1))
    return backends


def probe_levels(
    request: ReportRequest,
    *,
    import_module: ImportModule | None = None,
) -> tuple[InsightsLevelStatus, ...]:
    """Return availability for every level in ladder order."""
    statuses: list[InsightsLevelStatus] = [
        InsightsLevelStatus(
            level="builtin",
            available=True,
            backend="builtin",
            reason="always available",
        )
    ]
    for level in LEVEL_ORDER:
        if level == "builtin":
            continue
        statuses.append(_probe_level(level, request, import_module=import_module))
    return tuple(statuses)


def _probe_level(
    level: InsightsLevel,
    request: ReportRequest,
    *,
    import_module: ImportModule | None,
) -> InsightsLevelStatus:
    """Probe one level, choosing the first backend whose modules import."""
    backends = _ordered_backends(level, request)
    for backend in backends:
        present, _missing = probe_modules(backend.modules, import_module=import_module)
        if present:
            return InsightsLevelStatus(
                level=level,
                available=True,
                backend=backend.name,
                reason="available",
            )
    preferred = backends[0]
    _present, missing = probe_modules(preferred.modules, import_module=import_module)
    return InsightsLevelStatus(
        level=level,
        available=False,
        backend=None,
        reason=f"missing: {', '.join(missing)}",
        setup_command=preferred.setup_command,
    )


def run_level(
    level: InsightsLevel,
    *,
    report: InsightsReport,
    records: cabc.Sequence[SearchRecord],
    request: ReportRequest,
    policy: BackendPolicy,
    import_module: ImportModule | None = None,
    progress: InsightsProgress | None = None,
    model_cache: pathlib.Path | None = None,
) -> tuple[InsightsEnrichment, list[ReportDiagnostic]]:
    """Run the chosen backend for ``level``; never raises.

    Returns the enrichment (``status`` ``ok``/``skipped``/``error``) and
    any diagnostics. Backend resolution failures and runtime errors are
    captured as a skipped/error enrichment plus a diagnostic carrying the
    setup command, so the deterministic report still renders.
    """
    diagnostics: list[ReportDiagnostic] = []
    backends = _ordered_backends(level, request)
    if level == "llm" and request.llm_backend not in ("auto", "", *(b.name for b in backends)):
        valid = ", ".join(b.name for b in backends)
        unknown = f"unknown LLM backend {request.llm_backend!r}; valid backends: {valid}"
        diagnostics.append(
            ReportDiagnostic(severity="error", code="unknown-backend", message=unknown)
        )
        return (
            InsightsEnrichment(
                level=level,
                backend=request.llm_backend,
                status="error",
                message=unknown,
            ),
            diagnostics,
        )
    chosen = _first_available(backends, import_module=import_module)
    if chosen is None:
        preferred = backends[0]
        diagnostics.append(
            ReportDiagnostic(
                severity="warning",
                code="enricher-unavailable",
                message=f"no backend available for level {level!r}",
                setup_command=preferred.setup_command,
            )
        )
        return (
            InsightsEnrichment(
                level=level,
                backend=preferred.name,
                status="skipped",
                message=f"install a backend: {preferred.setup_command}",
            ),
            diagnostics,
        )

    importer = import_module
    try:
        modules = load_modules(
            chosen.modules,
            level=level,
            setup_command=chosen.setup_command,
            import_module=importer,
        )
        builder = _resolve_builder(level, chosen)
        context = EnricherContext(
            level=level,
            backend=chosen.name,
            request=request,
            records=records,
            report=report,
            modules=modules,
            policy=policy,
            import_module=importer,
            progress=progress,
            model_cache=model_cache,
        )
        enrichment = builder(context)
    except BackendUnavailable as exc:
        diagnostics.append(
            ReportDiagnostic(
                severity="warning",
                code="enricher-unavailable",
                message=str(exc),
                setup_command=exc.setup_command,
            )
        )
        return (
            InsightsEnrichment(
                level=level,
                backend=chosen.name,
                status="skipped",
                message=str(exc),
            ),
            diagnostics,
        )
    except BackendError as exc:
        diagnostics.append(
            ReportDiagnostic(
                severity="error",
                code="enricher-error",
                message=str(exc),
                setup_command=exc.setup_command,
            )
        )
        return (
            InsightsEnrichment(
                level=level,
                backend=chosen.name,
                status="error",
                message=str(exc),
            ),
            diagnostics,
        )
    except Exception as exc:
        diagnostics.append(
            ReportDiagnostic(
                severity="error",
                code="enricher-crash",
                message=f"{chosen.name} backend raised: {exc}",
            )
        )
        return (
            InsightsEnrichment(
                level=level,
                backend=chosen.name,
                status="error",
                message=f"{chosen.name} backend raised: {exc}",
            ),
            diagnostics,
        )
    return enrichment, diagnostics


def _first_available(
    backends: cabc.Sequence[BackendSpec],
    *,
    import_module: ImportModule | None,
) -> BackendSpec | None:
    """Return the first backend whose modules all import, or ``None``."""
    for backend in backends:
        present, _missing = probe_modules(backend.modules, import_module=import_module)
        if present:
            return backend
    return None


def _resolve_builder(
    level: InsightsLevel,
    backend: BackendSpec,
) -> cabc.Callable[[EnricherContext], InsightsEnrichment]:
    """Import the builder module lazily and return its build callable."""
    import importlib

    module = importlib.import_module(_BUILDER_MODULE[level])
    return t.cast(
        "cabc.Callable[[EnricherContext], InsightsEnrichment]",
        getattr(module, backend.builder),
    )
