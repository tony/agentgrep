"""Level 3 enricher: dense embeddings, semantic clusters, and dedupe.

Prefers ``sentence-transformers`` and falls back to torch-free
``model2vec`` (the registry picks whichever is installed). The model is
loaded from the local cache; provisioning happens only when the caller
allowed a download. The embedding helper :func:`embed_records` is reused
by the index level.
"""

from __future__ import annotations

import typing as t

from agentgrep.insights import models as models_mod
from agentgrep.insights.activity import _record_ref
from agentgrep.insights.loader import BackendConfigurationError
from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep import SearchRecord
    from agentgrep.insights.enrichers import EnricherContext
    from agentgrep.insights.models import EmbeddingModelSpec

_DUP_THRESHOLD = 0.93
_CLUSTER_THRESHOLD = 0.62
_MAX_CLUSTERS = 12
_MAX_DUPLICATES = 12


def _select_spec(ctx: EnricherContext) -> EmbeddingModelSpec:
    """Choose the embedding spec matching the resolved backend runtime."""
    runtime = t.cast("t.Any", ctx.backend)
    if ctx.request.model:
        requested = models_mod.resolve_embedding_model(ctx.request.model)
        if requested is not None and requested.runtime == runtime:
            return requested
    spec = models_mod.preferred_embedding_model(runtime)
    if spec is None:  # pragma: no cover — registry always has both runtimes
        message = f"no curated embedding model for runtime {ctx.backend!r}"
        raise BackendConfigurationError(message, level="embeddings")
    return spec


def _ensure_local_model(ctx: EnricherContext, spec: EmbeddingModelSpec) -> pathlib.Path:
    """Return the local model path, downloading it only if allowed."""
    if models_mod.is_installed(spec, ctx.model_cache):
        return models_mod.model_cache_path(spec, ctx.model_cache)
    if not ctx.policy.allow_download:
        message = f"embedding model {spec.model_id!r} is not provisioned"
        install = f"agentgrep insights models install {spec.model_id} --level embeddings --yes"
        raise BackendConfigurationError(message, level="embeddings", setup_command=install)
    result = models_mod.install_model(
        spec,
        model_cache=ctx.model_cache,
        progress=ctx.progress,
        import_module=ctx.import_module,
    )
    return result.path


def _load_model(ctx: EnricherContext, spec: EmbeddingModelSpec, local_path: pathlib.Path) -> t.Any:
    """Instantiate the embedding model from the local cache path."""
    if spec.runtime == "sentence-transformers":
        sentence_transformers = ctx.modules["sentence_transformers"]
        return sentence_transformers.SentenceTransformer(str(local_path))
    model2vec = ctx.modules["model2vec"]
    return model2vec.StaticModel.from_pretrained(str(local_path))


class EmbeddingResult(t.NamedTuple):
    """Embeddings plus the records and provenance they came from."""

    spec: EmbeddingModelSpec
    records: tuple[SearchRecord, ...]
    matrix: t.Any  # numpy.ndarray, row-normalized float32
    provenance: dict[str, t.Any]


def embed_records(ctx: EnricherContext) -> EmbeddingResult:
    """Resolve, provision, load, and run the embedding model on the records.

    Returns row-normalized embeddings aligned with the non-empty records.
    Reused by the index level so vector indexing shares one embedding
    pass.
    """
    numpy = ctx.modules["numpy"]
    spec = _select_spec(ctx)
    local_path = _ensure_local_model(ctx, spec)

    if ctx.progress is not None:
        ctx.progress.phase("load model", detail=spec.model_id)
    model = _load_model(ctx, spec, local_path)

    records = tuple(r for r in ctx.records if r.text and r.text.strip())
    texts = [r.text for r in records]
    if ctx.progress is not None:
        ctx.progress.phase("embed", detail=f"{len(texts)} records")
    raw = model.encode(texts) if texts else []
    matrix = (
        numpy.asarray(raw, dtype=numpy.float32).reshape(len(texts), -1)
        if texts
        else (numpy.zeros((0, spec.dimensions), dtype=numpy.float32))
    )
    if matrix.shape[0]:
        norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

    provenance = {
        "backend": spec.runtime,
        "model": spec.model_id,
        "local_path": str(local_path),
        "dimensions": int(matrix.shape[1]) if matrix.shape[0] else spec.dimensions,
    }
    return EmbeddingResult(spec=spec, records=records, matrix=matrix, provenance=provenance)


def _greedy_clusters(numpy: t.Any, matrix: t.Any, threshold: float) -> list[list[int]]:
    """Greedy single-pass cosine clustering over row-normalized vectors."""
    count = matrix.shape[0]
    assigned = [False] * count
    clusters: list[list[int]] = []
    for seed in range(count):
        if assigned[seed]:
            continue
        sims = matrix @ matrix[seed]
        members = [
            index
            for index in range(count)
            if not assigned[index] and float(sims[index]) >= threshold
        ]
        for index in members:
            assigned[index] = True
        clusters.append(members)
    clusters.sort(key=len, reverse=True)
    return clusters


def build_embeddings(ctx: EnricherContext) -> InsightsEnrichment:
    """Embed records and produce semantic clusters, dedupe, and provenance."""
    numpy = ctx.modules["numpy"]
    embedded = embed_records(ctx)
    matrix = embedded.matrix
    records = embedded.records

    if matrix.shape[0] < 2:
        return InsightsEnrichment(
            level="embeddings",
            backend=ctx.backend,
            status="ok",
            message="not enough records to compare semantically",
            data={"semantic_groups": [], "duplicates": []},
            provenance=embedded.provenance,
        )

    if ctx.progress is not None:
        ctx.progress.phase("cluster", detail=f"{matrix.shape[0]} vectors")
    clusters = _greedy_clusters(numpy, matrix, _CLUSTER_THRESHOLD)
    semantic_groups: list[dict[str, t.Any]] = []
    for members in clusters[:_MAX_CLUSTERS]:
        if len(members) < 2:
            continue
        lead = records[members[0]]
        semantic_groups.append(
            {
                "size": len(members),
                "example": _record_ref(lead).to_payload(),
                "members": [_record_ref(records[i]).to_payload() for i in members[:5]],
            }
        )

    duplicates: list[dict[str, t.Any]] = []
    sims = matrix @ matrix.T
    count = matrix.shape[0]
    for i in range(count):
        for j in range(i + 1, count):
            if float(sims[i, j]) >= _DUP_THRESHOLD:
                duplicates.append(
                    {
                        "similarity": round(float(sims[i, j]), 4),
                        "a": _record_ref(records[i]).to_payload(),
                        "b": _record_ref(records[j]).to_payload(),
                    }
                )
                if len(duplicates) >= _MAX_DUPLICATES:
                    break
        if len(duplicates) >= _MAX_DUPLICATES:
            break

    return InsightsEnrichment(
        level="embeddings",
        backend=ctx.backend,
        status="ok",
        message=(
            f"embedded {count} records with {embedded.spec.model_id}; "
            f"{len(semantic_groups)} semantic groups, {len(duplicates)} near-duplicates"
        ),
        data={"semantic_groups": semantic_groups, "duplicates": duplicates},
        provenance=embedded.provenance,
    )
