"""Local insights reports with model-backed enrichment.

This package implements ADR 0005: a staged report pipeline over the same
local record stream as ``agentgrep search``. Level 0 (``builtin``) is
deterministic and always available; higher levels (HTML, classical ML,
embeddings, persistent index, local LLM) attach as optional enrichers
behind lazy capability probes.

Importing this package pulls in only the standard library and the
agentgrep record types. No optional backend (scikit-learn, PyTorch,
sentence-transformers, tantivy, sqlite-vec, LanceDB, httpx, jinja2) is
imported until a level that needs it is actually selected.
"""

from __future__ import annotations

from agentgrep.insights.cache import (
    cache_dir,
    index_cache_dir,
    model_cache_dir,
    prune_cache,
)
from agentgrep.insights.enrichers import probe_levels
from agentgrep.insights.loader import (
    BackendConfigurationError,
    BackendError,
    BackendLoadError,
    BackendPolicy,
    BackendRuntimeError,
    BackendUnavailable,
)
from agentgrep.insights.model import (
    LEVEL_ORDER,
    InsightsActivity,
    InsightsEnrichment,
    InsightsLevel,
    InsightsLevelStatus,
    InsightsReport,
    RecordRef,
    ReportDiagnostic,
    ReportRequest,
)
from agentgrep.insights.report import build_report, representative_refs

__all__ = [
    "LEVEL_ORDER",
    "BackendConfigurationError",
    "BackendError",
    "BackendLoadError",
    "BackendPolicy",
    "BackendRuntimeError",
    "BackendUnavailable",
    "InsightsActivity",
    "InsightsEnrichment",
    "InsightsLevel",
    "InsightsLevelStatus",
    "InsightsReport",
    "RecordRef",
    "ReportDiagnostic",
    "ReportRequest",
    "build_report",
    "cache_dir",
    "index_cache_dir",
    "model_cache_dir",
    "probe_levels",
    "prune_cache",
    "representative_refs",
]
