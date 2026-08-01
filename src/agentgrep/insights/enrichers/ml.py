"""Level 2 enricher: TF-IDF + KMeans topic clusters (scikit-learn)."""

from __future__ import annotations

import importlib
import typing as t

from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    from agentgrep.insights.enrichers import EnricherContext

_MAX_TOPICS = 8
_TERMS_PER_TOPIC = 8


def build_ml(ctx: EnricherContext) -> InsightsEnrichment:
    """Cluster record texts into topic candidates with TF-IDF + KMeans."""
    importer: t.Any = ctx.import_module or importlib.import_module
    text_mod = importer("sklearn.feature_extraction.text")
    cluster_mod = importer("sklearn.cluster")

    texts = [r.text for r in ctx.records if r.text and r.text.strip()]
    if len(texts) < 2:
        return InsightsEnrichment(
            level="ml",
            backend=ctx.backend,
            status="ok",
            message="not enough records to cluster",
            data={"topics": [], "n_clusters": 0},
        )

    if ctx.progress is not None:
        ctx.progress.phase("vectorize", detail=f"{len(texts)} records")
    vectorizer = text_mod.TfidfVectorizer(
        max_features=2000,
        stop_words="english",
        min_df=1,
    )
    matrix = vectorizer.fit_transform(texts)

    n_clusters = max(2, min(_MAX_TOPICS, len(texts)))
    if ctx.progress is not None:
        ctx.progress.phase("cluster", detail=f"k={n_clusters}")
    kmeans = cluster_mod.KMeans(n_clusters=n_clusters, n_init=10, random_state=0)
    labels = kmeans.fit_predict(matrix)

    terms = vectorizer.get_feature_names_out()
    centers = kmeans.cluster_centers_
    topics: list[dict[str, t.Any]] = []
    for index in range(n_clusters):
        center = centers[index]
        ranked = center.argsort()[::-1][:_TERMS_PER_TOPIC]
        top_terms = [str(terms[j]) for j in ranked if center[j] > 0]
        size = int((labels == index).sum())
        topics.append({"topic": index, "size": size, "terms": top_terms})

    topics.sort(key=lambda topic: topic["size"], reverse=True)
    return InsightsEnrichment(
        level="ml",
        backend=ctx.backend,
        status="ok",
        message=f"clustered {len(texts)} records into {n_clusters} topics",
        data={"topics": topics, "n_clusters": n_clusters},
    )
