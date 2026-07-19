"""Split over-merged archetype clusters with an orthogonal-signal rerank.

model2vec's static geometry scores long dev-prompts as similar, so density
clustering over its vectors over-merges unrelated asks. A cosine guard in the
*same* space cannot fix this. This module re-judges cluster cohesion with a
signal independent of the embedding geometry, in a graceful ladder:

1. a **cross-encoder** (joint-encodes a text pair → 0-1 relatedness), opt-in
   via ``agentgrep[insights-graph-st]`` + a provisioned reranker model;
2. a **TF-IDF lexical gate** (token/bigram overlap), via scikit-learn only;
3. **no-op** (keep the embedding clusters as-is).

Each tier returns a full partition of the input rows — incohesive members are
demoted to singletons — so the result drops into the same downstream code as
:func:`agentgrep.insights.enrichers.embeddings._cluster_embeddings`.
"""

from __future__ import annotations

import statistics
import typing as t

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.insights.loader import ImportModule

RerankTier = t.Literal["cross-encoder", "tfidf", "none"]

# Cross-encoder: a GENTLE second-stage purifier on already-cohesive clusters.
# The curated duplicate-question model returns a 0-1 probability used directly
# (it sigmoids internally). Paraphrased recurring asks are not exact duplicates,
# so they score moderately — an absolute cutoff would nuke good clusters. Demote
# a member only when its mean pairwise relatedness is BOTH objectively weak
# (< floor) AND far below its cluster-mates (< ratio * median). Anchor-free.
_CE_ABS_FLOOR = 0.35
_CE_REL_RATIO = 0.5
# TF-IDF: demote a member only when its cosine to the cluster's lexical centroid
# is BOTH objectively low (< floor) AND well below the cluster norm
# (< ratio * median). Two signals avoid splitting cohesive clusters whose
# absolute overlap is just naturally low (sparse shared vocabulary).
_TFIDF_ABS_FLOOR = 0.5
_TFIDF_REL_RATIO = 0.8

# A member-keeper decides which rows of one cluster survive (the rest demote).
_Keeper = "cabc.Callable[[list[int]], list[int]]"


def rerank_clusters(
    clusters: list[list[int]],
    texts: list[str],
    *,
    import_module: ImportModule,
    model_cache: pathlib.Path | None = None,
    allow_download: bool = False,
) -> tuple[list[list[int]], RerankTier]:
    """Split incohesive members out of each multi-member cluster.

    Returns ``(clusters, tier)`` where ``tier`` names the signal that ran. The
    returned clusters are a full partition: real clusters first (largest
    first), then one singleton per demoted row.
    """
    keeper = _cross_encoder_keeper(texts, import_module, model_cache, allow_download)
    if keeper is not None:
        return _apply_keeper(clusters, keeper), "cross-encoder"
    keeper = _tfidf_keeper(texts, import_module)
    if keeper is not None:
        return _apply_keeper(clusters, keeper), "tfidf"
    return clusters, "none"


def _apply_keeper(clusters: list[list[int]], keeper: t.Any) -> list[list[int]]:
    """Re-partition: keep the cohesive core of each cluster, demote the rest."""
    out: list[list[int]] = []
    for members in clusters:
        if len(members) < 2:
            out.append(members)
            continue
        kept = keeper(members)
        demoted = [m for m in members if m not in set(kept)]
        if len(kept) >= 2:
            out.append(kept)
        else:
            demoted = members  # whole cluster failed cohesion -> all singletons
        out.extend([m] for m in demoted)
    out.sort(key=lambda members: (len(members) >= 2, len(members)), reverse=True)
    return out


def _cross_encoder_keeper(
    texts: list[str],
    import_module: ImportModule,
    model_cache: pathlib.Path | None,
    allow_download: bool,
) -> t.Any:
    """Return a member-keeper backed by a cross-encoder, or ``None``."""
    try:
        sentence_transformers = import_module("sentence_transformers")
    except ImportError:
        return None
    from agentgrep.insights import models as models_mod

    spec = models_mod.preferred_reranker_model()
    if spec is None:
        return None
    if not models_mod.is_installed(spec, model_cache):
        if not allow_download:
            return None
        try:
            models_mod.install_model(spec, model_cache=model_cache, import_module=import_module)
        except Exception:
            return None
    try:
        encoder = sentence_transformers.CrossEncoder(
            str(models_mod.model_cache_path(spec, model_cache))
        )
    except Exception:
        return None

    def _keep(members: list[int]) -> list[int]:
        # Score every unordered pair; a member survives when its mean related-
        # ness to the others clears the floor. Anchor-free (no privileged lead).
        index_pairs = [(i, j) for i in range(len(members)) for j in range(i + 1, len(members))]
        try:
            scores = encoder.predict(
                [(texts[members[i]], texts[members[j]]) for i, j in index_pairs]
            )
        except Exception:
            return members
        related: list[list[float]] = [[] for _ in members]
        for (i, j), score in zip(index_pairs, list(scores), strict=True):
            probability = float(score)
            related[i].append(probability)
            related[j].append(probability)
        mean_related = [(sum(values) / len(values)) if values else 1.0 for values in related]
        median = statistics.median(mean_related)
        return [
            member
            for member, mean in zip(members, mean_related, strict=True)
            if not (mean < _CE_ABS_FLOOR and mean < _CE_REL_RATIO * median)
        ]

    return _keep


def _tfidf_keeper(texts: list[str], import_module: ImportModule) -> t.Any:
    """Return a member-keeper backed by a TF-IDF lexical centroid, or ``None``."""
    try:
        feature_text = import_module("sklearn.feature_extraction.text")
    except ImportError:
        return None

    def _keep(members: list[int]) -> list[int]:
        member_texts = [texts[m] for m in members]
        try:
            vectorizer = feature_text.TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
            matrix = vectorizer.fit_transform(member_texts)
        except ValueError:
            # Empty vocabulary (e.g. all-stopword texts): no lexical signal.
            return members
        # Row-normalized TF-IDF rows + their mean centroid; cohesion = cosine to
        # the centroid (anchor-free).
        rows = matrix.toarray()
        norms = [(row @ row) ** 0.5 or 1.0 for row in rows]
        unit = [row / norm for row, norm in zip(rows, norms, strict=True)]
        centroid = [sum(col) / len(unit) for col in zip(*unit, strict=True)]
        centroid_norm = sum(value * value for value in centroid) ** 0.5 or 1.0
        cohesion = [
            sum(a * b for a, b in zip(row, centroid, strict=True)) / centroid_norm for row in unit
        ]
        median = statistics.median(cohesion)
        return [
            member
            for member, value in zip(members, cohesion, strict=True)
            if not (value < _TFIDF_ABS_FLOOR and value < _TFIDF_REL_RATIO * median)
        ]

    return _keep
