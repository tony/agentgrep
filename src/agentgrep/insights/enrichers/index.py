"""Level 4 enricher: persistent hybrid index (tantivy + sqlite-vec | LanceDB).

Two backends share one shape: build a persistent index under the cache
directory, add full-text documents, add vectors when an embedding model
is available (reusing the level-3 embedding pass), and run one sample
query to prove the index is usable. The default backend is
``tantivy`` (BM25) + ``sqlite-vec`` (KNN); ``lancedb`` is the single-store
alternative.
"""

from __future__ import annotations

import contextlib
import dataclasses
import typing as t

from agentgrep.insights import cache as cache_mod
from agentgrep.insights.activity import _record_ref
from agentgrep.insights.loader import BackendConfigurationError, load_modules
from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep import SearchRecord
    from agentgrep.insights.enrichers import EnricherContext
    from agentgrep.insights.enrichers.embeddings import EmbeddingResult

_SAMPLE_HITS = 5


def _sample_term(ctx: EnricherContext) -> str:
    """Return a representative full-text query term from the report."""
    if ctx.report.top_terms:
        return ctx.report.top_terms[0].term
    return "the"


def _try_embed(ctx: EnricherContext) -> EmbeddingResult | None:
    """Embed records by borrowing an available embedding backend, or ``None``.

    Returns ``None`` when no embedding backend is installed or the model
    is not provisioned, so the index degrades to full-text only instead
    of failing.
    """
    from agentgrep.insights import enrichers
    from agentgrep.insights.enrichers import embeddings as embeddings_mod

    backends = enrichers._ordered_backends("embeddings", ctx.request)
    chosen = enrichers._first_available(backends, import_module=ctx.import_module)
    if chosen is None:
        return None
    modules = load_modules(
        chosen.modules,
        level="embeddings",
        setup_command=chosen.setup_command,
        import_module=ctx.import_module,
    )
    emb_ctx = dataclasses.replace(ctx, backend=chosen.name, modules=modules)
    try:
        return embeddings_mod.embed_records(emb_ctx)
    except BackendConfigurationError:
        return None


def _build_tantivy_sqlitevec(
    ctx: EnricherContext,
    records: tuple[SearchRecord, ...],
    embedded: EmbeddingResult | None,
) -> dict[str, t.Any]:
    """Build a tantivy full-text index plus an optional sqlite-vec vector index."""
    import sqlite3

    tantivy = ctx.modules["tantivy"]
    index_dir = cache_mod.ensure_dir(cache_mod.index_cache_dir() / "tantivy")

    if ctx.progress is not None:
        ctx.progress.phase("index", detail=f"{len(records)} docs (tantivy)")
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("doc_id", stored=True)
    builder.add_text_field("text", stored=True)
    schema = builder.build()
    index = tantivy.Index(schema, path=str(index_dir))
    writer = index.writer()
    for position, record in enumerate(records):
        writer.add_document(tantivy.Document(doc_id=str(position), text=record.text))
    writer.commit()
    index.reload()

    vectors_included = False
    vector_db_path: pathlib.Path | None = None
    if embedded is not None and embedded.matrix.shape[0]:
        if ctx.progress is not None:
            ctx.progress.phase("index", detail="vectors (sqlite-vec)")
        sqlite_vec = ctx.modules["sqlite_vec"]
        vector_db_path = cache_mod.index_cache_dir() / "vectors.db"
        connection = sqlite3.connect(str(vector_db_path))
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            dim = int(embedded.matrix.shape[1])
            connection.execute("DROP TABLE IF EXISTS vec_items")
            connection.execute(f"CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[{dim}])")
            for position in range(embedded.matrix.shape[0]):
                vector = embedded.matrix[position].tolist()
                connection.execute(
                    "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
                    (position, sqlite_vec.serialize_float32(vector)),
                )
            connection.commit()
            vectors_included = True
        finally:
            connection.close()

    hits: list[t.Any] = []
    if ctx.progress is not None:
        ctx.progress.phase("search", detail="sample query")
    try:
        searcher = index.searcher()
        query = index.parse_query(_sample_term(ctx), ["text"])
        for _score, address in searcher.search(query, _SAMPLE_HITS).hits:
            doc = searcher.doc(address)
            doc_id = int(doc["doc_id"][0])
            hits.append(_record_ref(records[doc_id]).to_payload())
    except Exception:
        hits = []

    return {
        "backend": "tantivy+sqlite-vec",
        "documents_indexed": len(records),
        "index_path": str(index_dir),
        "vector_index_path": str(vector_db_path) if vector_db_path else None,
        "vectors_included": vectors_included,
        "sample_query": _sample_term(ctx),
        "hits": hits,
    }


def _build_lancedb(
    ctx: EnricherContext,
    records: tuple[SearchRecord, ...],
    embedded: EmbeddingResult | None,
) -> dict[str, t.Any]:
    """Build a single LanceDB table with full-text and optional vectors."""
    lancedb = ctx.modules["lancedb"]
    index_dir = cache_mod.ensure_dir(cache_mod.index_cache_dir() / "lancedb")

    if ctx.progress is not None:
        ctx.progress.phase("index", detail=f"{len(records)} docs (lancedb)")
    rows: list[dict[str, t.Any]] = []
    vectors_included = embedded is not None and embedded.matrix.shape[0] == len(records)
    for position, record in enumerate(records):
        row: dict[str, t.Any] = {"doc_id": str(position), "text": record.text}
        if vectors_included:
            row["vector"] = embedded.matrix[position].tolist()
        rows.append(row)

    connection = lancedb.connect(str(index_dir))
    table = connection.create_table("records", data=rows, mode="overwrite")
    with contextlib.suppress(Exception):
        table.create_fts_index("text", replace=True)

    hits: list[t.Any] = []
    if ctx.progress is not None:
        ctx.progress.phase("search", detail="sample query")
    try:
        results = table.search(_sample_term(ctx), query_type="fts").limit(_SAMPLE_HITS).to_list()
        hits = [_record_ref(records[int(result["doc_id"])]).to_payload() for result in results]
    except Exception:
        hits = []

    return {
        "backend": "lancedb",
        "documents_indexed": len(records),
        "index_path": str(index_dir),
        "vectors_included": vectors_included,
        "sample_query": _sample_term(ctx),
        "hits": hits,
    }


def build_index(ctx: EnricherContext) -> InsightsEnrichment:
    """Build a persistent hybrid index with the selected backend."""
    records = tuple(r for r in ctx.records if r.text and r.text.strip())
    if not records:
        return InsightsEnrichment(
            level="index",
            backend=ctx.backend,
            status="ok",
            message="no records to index",
            data={"documents_indexed": 0},
        )

    if ctx.progress is not None:
        ctx.progress.phase("embed", detail="resolving embedding model")
    embedded = _try_embed(ctx)

    if ctx.backend == "lancedb":
        data = _build_lancedb(ctx, records, embedded)
    else:
        data = _build_tantivy_sqlitevec(ctx, records, embedded)

    provenance = embedded.provenance if embedded is not None else None
    vector_note = "with vectors" if data.get("vectors_included") else "full-text only"
    return InsightsEnrichment(
        level="index",
        backend=ctx.backend,
        status="ok",
        message=f"indexed {data['documents_indexed']} documents ({vector_note})",
        data=data,
        provenance=provenance,
    )
