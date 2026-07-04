(adr-agentic-insights-engine)=

# ADR 0017: Agentic insights engine

## Status

Accepted.

Initial implementation landed with a deterministic `InsightEngine` over the
SQLite DB store. It records duplicate variant edges, omission
findings, insight runs, clusters, and evidence rows without adding LanceDB as
a required dependency.

## Context

The DB index from {ref}`adr-persistent-agentic-db-index`
normalizes agent history into a durable local read model. That is necessary
but not enough for insight workflows. Similarity, variants, missing pieces,
meaningful omissions, and ranked evidence are interpretations over records,
not record storage itself.

agentgrep already has lightweight ranking and near-duplicate logic for search
results, but global insight generation needs a different shape. It must avoid
pairwise comparison over the entire history, preserve evidence, and separate
deterministic candidate generation from optional LLM judgment.

Prior systems point to the same direction:

- LanceDB combines vector search, full-text search, scalar filtering, hybrid
  execution, and reranking in one table-oriented retrieval API. That is the
  useful model for optional semantic insight storage, not for the required
  DB cache:
  [query API](https://github.com/lancedb/lancedb/blob/v0.30.0/rust/lancedb/src/query.rs),
  [hybrid query helpers](https://github.com/lancedb/lancedb/blob/v0.30.0/rust/lancedb/src/query/hybrid.rs),
  [RRF reranker](https://github.com/lancedb/lancedb/blob/v0.30.0/rust/lancedb/src/rerankers/rrf.rs),
  and [index builders](https://github.com/lancedb/lancedb/blob/v0.30.0/rust/lancedb/src/index.rs).
- Lance's index formats show why semantic, scalar, and full-text structures
  should remain index artifacts over row identifiers instead of redefining the
  base record model:
  [index overview](https://github.com/lance-format/lance/blob/v7.0.0/docs/src/format/index/index.md),
  [vector indices](https://github.com/lance-format/lance/blob/v7.0.0/docs/src/format/index/vector/index.md),
  and [FTS indices](https://github.com/lance-format/lance/blob/v7.0.0/docs/src/format/index/scalar/fts.md).
- Chroma's embedded mode is the closest shape to agentgrep's: one local
  SQLite file carries the system catalog, record metadata, and an FTS5
  full-text index, while vector indexes live beside it as per-collection
  segment artifacts a local segment manager opens on demand. Chroma's own
  0.4 release consolidated onto SQLite for exactly the reasons ADR 0005
  chose it — fewer moving parts and robust local full-text search — which
  makes its segment layout the reference for attaching an optional vector
  segment without changing the SQLite-first base:
  [SQLite metadata and FTS segment](https://github.com/chroma-core/chroma/blob/1.5.9/chromadb/segment/impl/metadata/sqlite.py),
  [local persistent HNSW segment](https://github.com/chroma-core/chroma/blob/1.5.9/chromadb/segment/impl/vector/local_persistent_hnsw.py),
  [local segment manager](https://github.com/chroma-core/chroma/blob/1.5.9/rust/segment/src/local_segment_manager.rs),
  and the [Chroma 0.4 storage consolidation note](https://www.trychroma.com/blog/chroma_0.4.0).
- Lucene and Tantivy keep candidate generation, scoring, and result gathering as
  query-time behaviors over immutable index state:
  [Lucene IndexSearcher](https://github.com/apache/lucene/blob/releases/lucene/9.12.3/lucene/core/src/java/org/apache/lucene/search/IndexSearcher.java),
  [Lucene TopDocs](https://github.com/apache/lucene/blob/releases/lucene/9.12.3/lucene/core/src/java/org/apache/lucene/search/TopDocs.java),
  [Tantivy query trait](https://github.com/quickwit-oss/tantivy/blob/0.26.1/src/query/query.rs),
  and [Tantivy collector module](https://github.com/quickwit-oss/tantivy/blob/0.26.1/src/collector/mod.rs).
- DataFusion's session and planner boundaries are useful for explainable
  insight execution: logical intent is lowered into a physical plan, and
  runtime state stays separate:
  [session state](https://github.com/apache/datafusion/blob/53.1.0/datafusion/core/src/execution/session_state.rs),
  [physical planner](https://github.com/apache/datafusion/blob/53.1.0/datafusion/core/src/physical_planner.rs),
  and [execution plan](https://github.com/apache/datafusion/blob/53.1.0/datafusion/physical-plan/src/execution_plan.rs).

agentgrep does not need to become a vector database or a general workflow
engine. The useful pattern is narrower: deterministic retrieval builds small
candidate sets, then optional judgment operates only on evidence packs.

## Decision

agentgrep will introduce an insights engine above the persistent DB
index.

The insights engine writes separate derived artifacts. It must not mutate the
DB index's normalized source and record rows. It must be possible to delete
and recompute insight artifacts without rebuilding the DB index.

The default insights implementation is deterministic and local. Optional
semantic retrieval may attach behind a typed store boundary, and Chroma's
embedded segment shape — vector artifacts keyed by record id living beside
the same SQLite substrate — is the preferred integration model, with
LanceDB's table-oriented hybrid retrieval as the alternative. Neither is a
required dependency for normal agentgrep installation, import, CLI search,
or MCP search.

The first insight categories are:

1. **Similarity clusters**: groups of prompts, conversations, instructions,
   or agent guidance with shared intent or text shape.
2. **Variant edges**: typed relationships such as exact duplicate, near
   duplicate, same intent in a different project, same project but different
   issue, toolchain variant, and instruction variant.
3. **Omission findings**: evidence that a meaningful recurring instruction or
   pattern is absent from a target project or instruction file.
4. **Evidence packs**: bounded, reviewable source records and feature scores
   used by a human or LLM judge.
5. **Insight runs**: provenance for algorithm versions, optional model
   versions, thresholds, inputs, and generated artifacts.

## Interfaces

Names below describe intended internal contracts. They are not public APIs
until implemented and documented.

`InsightStore`
: Stores insight runs, feature rows, clusters, variant edges, omission
  findings, and evidence packs. The default store uses SQLite. Optional
  semantic stores may attach by stable DB record id.

`FeatureExtractor`
: Builds deterministic features from normalized records: exact hashes,
  normalized hashes, token shingles, SimHash, MinHash signatures, token
  counts, path/project hints, agent/store hints, and quality flags.

`CandidateGenerator`
: Produces bounded candidate pairs or candidate groups from independent
  signals: FTS5/BM25, metadata filters, hash equality, SimHash distance,
  MinHash overlap, and optional embedding nearest neighbors.

`VariantClassifier`
: Assigns relationship types to candidate pairs using deterministic feature
  agreement before any LLM judgment is considered.

`OmissionDetector`
: Compares recurring cluster evidence with a target project, AGENTS.md file,
  or skill corpus to find meaningful missing pieces.

`InsightJudge`
: Optional judging boundary. It receives a small evidence pack and returns a
  structured judgment with confidence and rationale. It does not mutate files.

## Similarity and confidence rules

The insights engine must generate candidates before ranking or judging them.
It must not run unbounded pairwise comparison across the entire DB index.

Candidate signals are intentionally independent:

- exact and normalized hashes for duplicates;
- SimHash distance for near duplicates;
- MinHash or shingled Jaccard for prompt-template variants;
- FTS5/BM25 for lexical candidates;
- metadata agreement for agent, project, session, role, time, and toolchain;
- optional embedding distance for semantic similarity.

RapidFuzz remains useful for small candidate reranking. It is not the global
similarity engine.

High confidence requires either a direct proof, such as an exact normalized
hash match, or agreement across multiple independent signals. A semantic match
alone is not enough for a high-confidence variant edge or omission finding.

Omission detection is conservative. A missing piece is meaningful only when:

- the source pattern recurs in neighboring or comparable projects;
- the target project has matching context, tooling, or workflow;
- the target instruction surface lacks the pattern;
- the absence is not explained by different tooling or project scope;
- the evidence pack is small enough for review.

## Consequences

### Positive

- Similarity and omission workflows become reproducible and inspectable.
- LLM calls, when configured, judge evidence instead of searching the whole
  local history.
- LanceDB can be valuable without becoming a required dependency.
- Future central/local contrast workflows can reuse the same insight artifacts.

### Tradeoffs

- The project gains another derived-data lifecycle beyond the DB
  cache.
- Thresholds and feature versions must be recorded so old insight runs remain
  explainable.
- Optional embedding backends introduce model/version drift that deterministic
  tests cannot fully cover.

### Risks

False positives: prompts can look similar while solving different issues. The
mitigation is typed variant edges plus metadata-aware confidence.

False omissions: a repeated instruction can be absent for a good reason. The
mitigation is conservative omission rules and human review before suggestions.

Opaque model judgment: an LLM could overstate weak evidence. The mitigation is
to make deterministic signals and source evidence primary, and to record the
LLM output as a judgment artifact rather than a fact.

## Final position

The insights engine is a derived, explainable analysis layer over the
DB index. It finds candidates deterministically, records evidence and
provenance, and uses optional semantic or LLM components only behind explicit
boundaries. Its outputs feed suggestion workflows, but it does not edit
project instructions or skills.
