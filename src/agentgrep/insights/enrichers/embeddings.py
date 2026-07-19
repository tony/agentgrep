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
from agentgrep.insights.loader import BackendConfigurationError, default_import_module
from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep import SearchRecord
    from agentgrep.insights.enrichers import EnricherContext
    from agentgrep.insights.loader import ImportModule
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


class LoadedEmbedder(t.NamedTuple):
    """A loaded embedding model with its spec, local path, and resolved device."""

    model: t.Any
    spec: EmbeddingModelSpec
    local_path: pathlib.Path
    device: str


def _resolved_device(model: t.Any) -> str:
    """Return the device the model loaded onto (GPU/MPS/CPU), best-effort.

    ``SentenceTransformer`` auto-selects a device; recording it keeps graph
    reports reproducible about where the embeddings were computed.
    """
    device = getattr(model, "device", None)
    return str(device) if device is not None else "cpu"


def load_embedder(ctx: EnricherContext) -> LoadedEmbedder:
    """Resolve, provision, and load the embedding model once.

    Reused by the index and graph levels so a single model load serves
    multiple ``encode_texts`` passes over different node granularities.
    """
    spec = _select_spec(ctx)
    local_path = _ensure_local_model(ctx, spec)
    if ctx.progress is not None:
        ctx.progress.phase("load model", detail=spec.model_id)
    model = _load_model(ctx, spec, local_path)
    return LoadedEmbedder(
        model=model, spec=spec, local_path=local_path, device=_resolved_device(model)
    )


def encode_texts(ctx: EnricherContext, embedder: LoadedEmbedder, texts: list[str]) -> t.Any:
    """Return a row-normalized float32 matrix for ``texts`` (shape ``(N, dim)``)."""
    numpy = ctx.modules["numpy"]
    if not texts:
        return numpy.zeros((0, embedder.spec.dimensions), dtype=numpy.float32)
    raw = embedder.model.encode(texts)
    matrix = numpy.asarray(raw, dtype=numpy.float32).reshape(len(texts), -1)
    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


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
    embedder = load_embedder(ctx)
    spec, local_path = embedder.spec, embedder.local_path

    records = tuple(r for r in ctx.records if r.text and r.text.strip())
    texts = [r.text for r in records]
    if ctx.progress is not None:
        ctx.progress.phase("embed", detail=f"{len(texts)} records")
    matrix = encode_texts(ctx, embedder, texts)

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


def _cluster_embeddings(
    numpy: t.Any,
    matrix: t.Any,
    threshold: float,
    *,
    import_module: ImportModule | None = None,
    min_cluster_size: int = 2,
) -> list[list[int]]:
    """Cluster row-normalized vectors, preferring HDBSCAN over greedy cosine.

    Density-based HDBSCAN finds tighter, variable-shaped archetypes than the
    single global cosine threshold of :func:`_greedy_clusters`, and abstains on
    outliers instead of forcing them into a cluster. Outliers (HDBSCAN label
    ``-1``) are returned as singleton clusters so the result is a full
    partition of every row — the same contract as :func:`_greedy_clusters`,
    which downstream archetype labelling and workflow mining rely on.

    Falls back to the greedy pass when scikit-learn is not installed or
    HDBSCAN rejects the input (e.g. an unsupported metric on an older build).

    Parameters
    ----------
    numpy : module
        The resolved numpy module (used only by the greedy fallback).
    matrix : numpy.ndarray
        Row-normalized float32 vectors, shape ``(N, dim)``.
    threshold : float
        Cosine threshold handed to the greedy fallback.
    import_module : ImportModule, optional
        Injectable importer; defaults to :func:`importlib.import_module`.
    min_cluster_size : int
        Smallest archetype HDBSCAN will report (default 2).

    Returns
    -------
    list[list[int]]
        Member-index lists: real clusters first (largest first), then one
        singleton list per outlier row.
    """
    count = int(matrix.shape[0])
    if count < 2:
        return [[index] for index in range(count)]
    importer = import_module or default_import_module()
    try:
        sklearn_cluster = importer("sklearn.cluster")
    except ImportError:
        return _greedy_clusters(numpy, matrix, threshold)
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            labels = sklearn_cluster.HDBSCAN(
                min_cluster_size=min_cluster_size,
                metric="cosine",
                # "leaf" selects the most fine-grained clusters; the default
                # "eom" over-merges into broad groups on static-embedding
                # geometry, which blurs the "similar prompts" archetypes.
                cluster_selection_method="leaf",
            ).fit_predict(matrix)
    except ValueError, TypeError:
        # Degenerate input or a build without the cosine metric — stay useful.
        return _greedy_clusters(numpy, matrix, threshold)
    grouped: dict[int, list[int]] = {}
    singletons: list[list[int]] = []
    for index, raw_label in enumerate(labels):
        label = int(raw_label)
        if label < 0:
            singletons.append([index])
        else:
            grouped.setdefault(label, []).append(index)
    clusters: list[list[int]] = []
    for members in grouped.values():
        clusters.extend(_split_incohesive(numpy, matrix, members, threshold))
    clusters.extend(singletons)
    # Multi-member clusters first (largest first), then singletons.
    clusters.sort(key=lambda members: (len(members) >= 2, len(members)), reverse=True)
    return clusters


def _split_incohesive(
    numpy: t.Any,
    matrix: t.Any,
    members: list[int],
    threshold: float,
) -> list[list[int]]:
    """Enforce a cosine-cohesion floor on one HDBSCAN cluster.

    Density clustering on weak (static) embeddings can merge unrelated members.
    Members whose cosine to the cluster centroid is below ``threshold`` are
    demoted to singletons, so a returned multi-member cluster is at least as
    cohesive as a greedy-threshold cluster. Returns the kept cluster (if it
    still has 2+ members) followed by one singleton per demoted member.
    """
    if len(members) < 2:
        return [members]
    rows = matrix[members]
    centroid = rows.mean(axis=0)
    centroid = centroid / (float(numpy.linalg.norm(centroid)) or 1.0)
    sims = rows @ centroid
    kept = [member for member, sim in zip(members, sims, strict=True) if float(sim) >= threshold]
    demoted = [member for member, sim in zip(members, sims, strict=True) if float(sim) < threshold]
    result: list[list[int]] = []
    if len(kept) >= 2:
        result.append(kept)
    else:
        demoted = members  # whole cluster failed cohesion -> all singletons
    result.extend([member] for member in demoted)
    return result


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
