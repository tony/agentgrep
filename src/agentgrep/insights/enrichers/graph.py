"""Graph level: a persistent prompt/reply/conversation similarity network.

Builds a multi-granularity vector network from conversation turns —
prompts, replies, prompt→reply exchanges (with a transformation-delta
vector), and whole conversations — persists vectors and typed edges into a
``sqlite-vec`` graph DB, clusters prompt/conversation *archetypes*, and
mines recurring conversations and recurring prompt-chains (workflows).

The expensive math (cross-node similarity, mean-pooling, clustering) is
factored into small pure functions over numpy arrays so they unit-test
without the persistence layer.
"""

from __future__ import annotations

import hashlib
import re
import typing as t

from agentgrep.insights.enrichers import rerank
from agentgrep.insights.enrichers.embeddings import (
    _cluster_embeddings,
    _greedy_clusters,
    encode_texts,
    load_embedder,
)
from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep import SearchRecord
    from agentgrep.insights.enrichers import EnricherContext

_SIMILAR_THRESHOLD = 0.55
_ARCHETYPE_THRESHOLD = 0.62
_TRANSFORM_THRESHOLD = 0.6
_TOPK = 8
_MIN_SUPPORT = 2
_MAX_SECTION = 12
# Skill suggestions: top multi-step macros, then up to this many ranked
# recurring-ask templates (was a hard cap of 8, which hid most of a large
# corpus's real recurring asks).
_MAX_MACRO_SUGGESTIONS = 5
_MAX_SKILL_SUGGESTIONS = 50
# A macro leads the suggestion list, so a barely-recurring chain (support 2)
# would outrank a broadly-repeated template. Require a macro to recur at least
# this often before it is surfaced as a skill candidate.
_MIN_MACRO_SUPPORT = 3


class Turn(t.NamedTuple):
    """One ordered turn within a conversation."""

    conversation_id: str
    position: int
    role: str  # "user" | "assistant"
    text: str


_MAX_ASK_CHARS = 2000
# Structural markers of pasted command output that can still appear inside a
# genuinely human-typed prompt. The bulk of tool noise is now removed upstream
# by the adapter's ``human_typed`` tag; this is a light secondary guard.
_TOOL_PREFIXES = ("[master ", "[main ", "diff --git")
_STATUS_HEADS = {"M", "A", "D", "R", "?", "+", "-"}
_COMMAND_NAME_RE = re.compile(
    r"<command-(?:name|message)>\s*(/?[\w:.\-]+)\s*</command-(?:name|message)>"
)
_COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)


def _conversation_key(record: SearchRecord) -> str:
    """Return a stable grouping key for a record's conversation."""
    return record.conversation_id or record.session_id or str(record.path)


def normalize_ask(text: str) -> str:
    """Collapse a slash-command invocation to clean ``/name args`` text.

    Claude wraps a typed ``/pr:pr`` invocation in ``<command-message>`` /
    ``<command-name>`` / ``<command-args>`` tags. Normalizing keeps the
    archetype clustering and the rendered workflow readable (every
    ``/pr:pr`` becomes one archetype instead of fragmenting on the tags).
    """
    match = _COMMAND_NAME_RE.search(text)
    if not match:
        return text
    name = match.group(1)
    name = name if name.startswith("/") else f"/{name}"
    args = _COMMAND_ARGS_RE.search(text)
    return f"{name} {args.group(1).strip()}".strip() if args else name


def looks_like_user_ask(text: str) -> bool:
    """Secondary guard for pasted command output inside a human-typed prompt.

    The adapter's ``human_typed`` tag (see
    :func:`agentgrep.claude_event_is_human_authored`) removes tool results
    and subagent output at the source. This light filter only catches the
    residual case of a human prompt that is itself a pasted git-status or
    diff dump.

    Examples
    --------
    >>> looks_like_user_ask("rebase onto trunk and resolve conflicts")
    True
    >>> looks_like_user_ask("[master 3a9497f] ci: add marimo check gate")
    False
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_ASK_CHARS:
        return False
    if any(marker in stripped[:40] for marker in _TOOL_PREFIXES):
        return False
    lines = stripped.splitlines()
    if len(lines) >= 3:
        statusish = sum(
            1
            for line in lines
            if line[:2].strip()[:1] in _STATUS_HEADS
            or line.strip().startswith(("/", "src/", "docs/", "tests/"))
        )
        if statusish >= len(lines) * 0.5:
            return False
    return True


def reconstruct_turns(records: cabc.Sequence[SearchRecord]) -> dict[str, list[Turn]]:
    """Group records into ordered turns per conversation (emission order).

    Conversation records have no timestamps, so order is the order records
    are emitted (append-only JSONL ⇒ chronological). Role is taken from
    ``role`` (``user``/``assistant``); a bare prompt record counts as a user
    turn.
    """
    by_conversation: dict[str, list[Turn]] = {}
    for record in records:
        text = (record.text or "").strip()
        if not text:
            continue
        key = _conversation_key(record)
        turns = by_conversation.setdefault(key, [])
        # A user prompt is a bare prompt record or a user-role conversation
        # turn; everything else (assistant/model/tool/unset) is a reply.
        is_user = record.kind == "prompt" or record.role == "user"
        if is_user:
            # The adapter tags tool results / subagent output saved under a
            # user role; those are not the user's asks, so drop them.
            if record.metadata.get("human_typed", True) is False:
                continue
            if not looks_like_user_ask(text):
                continue
            text = normalize_ask(text)
        role = "user" if is_user else "assistant"
        turns.append(Turn(conversation_id=key, position=len(turns), role=role, text=text))
    return by_conversation


class GraphNodes(t.NamedTuple):
    """Extracted nodes and structural relationships, before embedding."""

    prompts: list[Turn]
    replies: list[Turn]
    exchanges: list[tuple[str, str]]  # (exchange_node_id, combined_text)
    structural_edges: list[tuple[str, str, str, float]]
    prompt_sequences: dict[str, list[str]]  # conversation -> ordered prompt node ids


def _node_id(node_type: str, conversation_id: str, position: int) -> str:
    """Return a stable per-occurrence node id."""
    return f"{node_type}:{conversation_id}:{position}"


def extract_nodes(by_conversation: dict[str, list[Turn]]) -> GraphNodes:
    """Derive prompt/reply/exchange nodes and structural edges from turns."""
    prompts: list[Turn] = []
    replies: list[Turn] = []
    exchanges: list[tuple[str, str]] = []
    edges: list[tuple[str, str, str, float]] = []
    prompt_sequences: dict[str, list[str]] = {}

    for conversation_id, turns in by_conversation.items():
        prior_prompt_id: str | None = None
        sequence: list[str] = []
        for turn in turns:
            node_type = "prompt" if turn.role == "user" else "reply"
            node_id = _node_id(node_type, conversation_id, turn.position)
            if turn.role == "user":
                prompts.append(turn)
                sequence.append(node_id)
                if prior_prompt_id is not None:
                    edges.append((prior_prompt_id, node_id, "next", 1.0))
                prior_prompt_id = node_id
            else:
                replies.append(turn)
                # responds_to the immediately preceding user turn, if any
                if turn.position > 0 and turns[turn.position - 1].role == "user":
                    user_id = _node_id("prompt", conversation_id, turn.position - 1)
                    edges.append((node_id, user_id, "responds_to", 1.0))
                    combined = f"{turns[turn.position - 1].text}\n\n{turn.text}"
                    exchanges.append(
                        (_node_id("exchange", conversation_id, turn.position - 1), combined)
                    )
        if sequence:
            prompt_sequences[conversation_id] = sequence

    return GraphNodes(
        prompts=prompts,
        replies=replies,
        exchanges=exchanges,
        structural_edges=edges,
        prompt_sequences=prompt_sequences,
    )


def labels_from_clusters(clusters: list[list[int]], count: int) -> list[int]:
    """Turn greedy cluster member-lists into a per-row archetype label."""
    labels = [-1] * count
    for cluster_id, members in enumerate(clusters):
        for member in members:
            labels[member] = cluster_id
    return labels


def topk_similar_edges(
    numpy: t.Any,
    matrix: t.Any,
    node_ids: list[str],
    *,
    k: int,
    threshold: float,
) -> list[tuple[str, str, str, float]]:
    """Return undirected top-k cosine-similarity edges above ``threshold``.

    The dense ``matrix @ matrix.T`` (O(N^2), materializes an NxN matrix) — exact
    and fast for small N. The memory-streaming and ANN equivalents live in
    :func:`topk_similar_edges_via_knn`.
    """
    count = matrix.shape[0]
    if count < 2:
        return []
    sims = matrix @ matrix.T
    numpy.fill_diagonal(sims, -1.0)
    neighbors = min(k, count - 1)
    top = numpy.argpartition(-sims, neighbors - 1, axis=1)[:, :neighbors]
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str, str, float]] = []
    for i in range(count):
        for raw_j in top[i]:
            j = int(raw_j)
            weight = float(sims[i, j])
            if weight < threshold:
                continue
            a, b = node_ids[i], node_ids[j]
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            edges.append((key[0], key[1], "similar", round(weight, 4)))
    return edges


# Above this many vectors, build the similar-edge graph with per-node kNN
# instead of the dense matrix. Below it, the dense path is exact and faster
# (a LanceDB IVF-PQ index also costs more to train than it saves at small N).
_KNN_EDGE_THRESHOLD = 2000


def topk_similar_edges_via_knn(
    knn: t.Any,
    matrix: t.Any,
    node_ids: list[str],
    *,
    k: int,
    threshold: float,
) -> list[tuple[str, str, str, float]]:
    """Build undirected similar-edges with one kNN query per node.

    ``knn(query_vector, k) -> [(node_id, distance)]`` is the backend's nearest-
    neighbor call (cosine *distance*, so similarity is ``1 - distance``). This
    never materializes the dense NxN matrix: it's the memory-streaming path on
    the sqlite-vec store and the true-ANN path on the LanceDB backend.
    """
    count = int(matrix.shape[0])
    if count < 2:
        return []
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str, str, float]] = []
    for index, node_id in enumerate(node_ids):
        for neighbor_id, distance in knn(matrix[index].tolist(), k + 1):
            if neighbor_id == node_id:
                continue
            weight = 1.0 - float(distance)
            if weight < threshold:
                continue
            key = (node_id, neighbor_id) if node_id < neighbor_id else (neighbor_id, node_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append((key[0], key[1], "similar", round(weight, 4)))
    return edges


def _build_similar_edges(
    ctx: EnricherContext,
    store: t.Any,
    numpy: t.Any,
    prompt_ids: list[str],
    prompt_matrix: t.Any,
) -> list[tuple[str, str, str, float]]:
    """Build the similar-prompt edges with the configured vector backend.

    - ``lancedb`` (opt-in): true IVF-PQ ANN over a Lance table.
    - ``sqlite-vec`` default: the dense matrix for small graphs (exact, fast),
      or per-node ``vec0`` kNN streaming past :data:`_KNN_EDGE_THRESHOLD`.
    """
    backend = (
        getattr(ctx.request, "graph_vector_backend", "sqlite-vec") if ctx.request else "sqlite-vec"
    )
    importer = ctx.import_module or __import__("importlib").import_module
    if backend == "lancedb":
        from agentgrep.insights import cache as cache_mod
        from agentgrep.insights.lancestore import LanceVectorBackend

        try:
            lance = LanceVectorBackend.build(
                cache_mod.index_cache_dir() / "graph" / "lancedb",
                prompt_ids,
                prompt_matrix,
                import_module=importer,
            )
        except Exception:
            # lancedb absent or backend error — fall back to the embedded default.
            backend = "sqlite-vec"
        else:
            if ctx.progress is not None:
                ctx.progress.phase("graph", detail="similar edges (lancedb ANN)")
            return topk_similar_edges_via_knn(
                lance.knn, prompt_matrix, prompt_ids, k=_TOPK, threshold=_SIMILAR_THRESHOLD
            )
    if int(prompt_matrix.shape[0]) >= _KNN_EDGE_THRESHOLD:
        return topk_similar_edges_via_knn(
            lambda vector, k: store.knn("prompt", vector, k=k),
            prompt_matrix,
            prompt_ids,
            k=_TOPK,
            threshold=_SIMILAR_THRESHOLD,
        )
    return topk_similar_edges(
        numpy, prompt_matrix, prompt_ids, k=_TOPK, threshold=_SIMILAR_THRESHOLD
    )


def mean_pool(numpy: t.Any, rows: list[t.Any]) -> t.Any:
    """Return the row-normalized mean of a list of vectors."""
    stacked = numpy.stack(rows)
    pooled = stacked.mean(axis=0)
    magnitude = float(numpy.linalg.norm(pooled)) or 1.0
    return pooled / magnitude


def _content_hash(text: str) -> str:
    """Return a short stable hash of node text (incremental-skip key)."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _encode_with_cache(
    ctx: EnricherContext,
    embedder: t.Any,
    numpy: t.Any,
    texts: list[str],
    cached_by_hash: dict[str, t.Any],
) -> tuple[t.Any, int]:
    """Embed ``texts``, reusing persisted vectors for unchanged content.

    Returns ``(matrix, reused_count)`` where ``matrix`` is row-aligned with
    ``texts``. Only texts whose content hash is absent from
    ``cached_by_hash`` are sent to the model; the rest are filled from the
    persisted (already row-normalized) vectors. This makes a rerun cost
    proportional to the *new* turns, not the whole history.
    """
    dim = int(embedder.spec.dimensions)
    if not texts:
        return numpy.zeros((0, dim), dtype=numpy.float32), 0
    hashes = [_content_hash(text) for text in texts]
    new_indices = [index for index, digest in enumerate(hashes) if digest not in cached_by_hash]
    new_matrix = encode_texts(ctx, embedder, [texts[index] for index in new_indices])
    if new_matrix.shape[0]:
        dim = int(new_matrix.shape[1])
    rows: list[t.Any] = []
    new_position = 0
    for digest in hashes:
        cached = cached_by_hash.get(digest)
        if cached is not None:
            rows.append(numpy.asarray(cached, dtype=numpy.float32))
        else:
            rows.append(new_matrix[new_position])
            new_position += 1
    matrix = numpy.stack(rows).astype(numpy.float32)
    return matrix, len(texts) - len(new_indices)


_SUMMARY_INSTRUCTION = (
    "Summarize what the user wanted in this conversation in ONE short sentence "
    "(under 20 words). Reply with only the sentence.\n\nUser asks:\n"
)
_MAX_SUMMARY_ASKS = 8
_MAX_ASK_SNIPPET = 300


def _ensure_llm_model(
    models_mod: t.Any, spec: t.Any, ctx: EnricherContext, importer: t.Any
) -> bool:
    """Return whether ``spec`` is available, provisioning it if download is allowed.

    The summary path needs the model on disk before the per-conversation loop;
    with ``--auto-download-models`` it provisions once here, else it reports
    unavailable so the caller falls back to the prompt-mean vector.
    """
    if models_mod.is_installed(spec):
        return True
    if not ctx.policy.allow_download:
        return False
    try:
        models_mod.install_model(spec, progress=ctx.progress, import_module=importer)
    except Exception:
        return False
    return True


def _build_conversation_summarizer(ctx: EnricherContext) -> t.Any:
    """Return a bounded ``str -> str`` summarizer, or ``None`` when unavailable.

    Opt-in via ``ReportRequest.conversation_summaries``. Reuses the skills
    module's bounded LLM runtimes (Ollama ``stream=false`` or in-process
    LiteRT-LM). Returns ``None`` on any unavailability so the caller falls
    back to the prompt-mean conversation vector.
    """
    request = ctx.request
    if request is None or not getattr(request, "conversation_summaries", False):
        return None
    import os

    from agentgrep.insights import skills as skills_mod

    backend = getattr(request, "llm_backend", "ollama")
    importer = ctx.import_module or __import__("importlib").import_module
    try:
        if backend == "ollama":
            endpoint = os.environ.get("AGENTGREP_OLLAMA_URL", "http://127.0.0.1:11434")
            # Probe once: skip the whole summary pass when no daemon answers, so
            # an absent backend costs one short timeout, not one per conversation.
            if not skills_mod.ollama_reachable(endpoint=endpoint, import_module=importer):
                return None
            return skills_mod.build_ollama_complete(
                model=request.model or "llama3.2",
                endpoint=endpoint,
                import_module=importer,
                timeout=20.0,
            )
        if backend == "litert-lm":
            from agentgrep.insights import models as models_mod

            spec = models_mod.resolve_llm_model(request.model or "gemma-4-e2b", "litert-lm")
            if spec is None or spec.artifact_filename is None:
                return None
            if not _ensure_llm_model(models_mod, spec, ctx, importer):
                return None
            model_path = models_mod.model_cache_path(spec) / spec.artifact_filename
            # A conversation vector only needs a one-line summary — cap tokens
            # tight so CPU generation stays bounded.
            return skills_mod.build_litert_complete(
                model_path=str(model_path), import_module=importer, max_tokens=64
            )
        if backend == "transformers":
            from agentgrep.insights import models as models_mod

            # An explicit --model pins one spec; otherwise walk the non-gated
            # default chain (Phi-4-mini 4-bit → SmolLM2 fp16 → Granite 4-bit)
            # and keep the first that provisions and loads.
            if request.model:
                pinned = models_mod.resolve_llm_model(request.model, "transformers")
                candidates: tuple[t.Any, ...] = (pinned,) if pinned is not None else ()
            else:
                candidates = models_mod.default_transformers_chain()

            def _load_one(spec: t.Any) -> t.Any:
                if not _ensure_llm_model(models_mod, spec, ctx, importer):
                    return None
                # The model dir loads once and serves every conversation; on GPU
                # a one-line summary is ~1-2s, so the whole pass is bounded.
                return skills_mod.build_transformers_complete(
                    model_path=str(models_mod.model_cache_path(spec)),
                    import_module=importer,
                    max_tokens=64,
                    quantization=spec.quantization,
                    trust_remote_code=spec.trust_remote_code,
                )

            chosen = skills_mod.first_working_transformers(candidates, load_one=_load_one)
            return chosen[1] if chosen is not None else None
    except ImportError:
        return None
    return None


def _conversation_summary_override(
    ctx: EnricherContext,
    embedder: t.Any,
    numpy: t.Any,
    store: t.Any,
) -> t.Any:
    """Return a ``(conversation_id, asks) -> vector|None`` override, or ``None``.

    When conversation summaries are enabled and a summarizer is reachable, the
    override embeds a cached one-line LLM summary of each conversation's user
    asks. Summaries are cached in the graph store keyed by the asks' content
    hash so reruns and repeated conversations skip the LLM call.
    """
    summarizer = _build_conversation_summarizer(ctx)
    if summarizer is None:
        return None

    def _override(_conversation_id: str, user_texts: list[str]) -> t.Any:
        if not user_texts:
            return None
        joined = "\n".join(user_texts)
        digest = _content_hash(joined)
        summary = store.get_summary(digest)
        if summary is None:
            asks = "\n".join(text[:_MAX_ASK_SNIPPET] for text in user_texts[:_MAX_SUMMARY_ASKS])
            try:
                summary = (summarizer(_SUMMARY_INSTRUCTION + asks) or "").strip()
            except Exception:
                return None
            if not summary:
                return None
            store.set_summary(digest, summary)
        matrix = encode_texts(ctx, embedder, [summary])
        return matrix[0] if matrix.shape[0] else None

    return _override


def build_graph(ctx: EnricherContext) -> InsightsEnrichment:
    """Build (or refresh) the similarity network and mine workflows."""
    from agentgrep.insights import cache as cache_mod, sequences as seq_mod
    from agentgrep.insights.graphstore import GraphStore

    numpy = ctx.modules["numpy"]
    sqlite_vec = ctx.modules["sqlite_vec"]

    if ctx.progress is not None:
        ctx.progress.phase("graph", detail="reconstruct turns")
    by_conversation = reconstruct_turns(ctx.records)
    nodes = extract_nodes(by_conversation)
    if not nodes.prompts:
        return InsightsEnrichment(
            level="graph",
            backend=ctx.backend,
            status="ok",
            message="no conversation turns to graph",
            data={"recurring_workflows": [], "recurring_conversations": []},
        )

    embedder = load_embedder(ctx)
    # Open the store before embedding so a rerun can reuse vectors for turns
    # whose text is unchanged (keyed by content hash). The store rebuilds
    # itself if the schema version, model, or vector dimension changed.
    store_path = cache_mod.index_cache_dir() / "graph" / "graph.db"
    store = GraphStore.open(
        store_path,
        sqlite_vec=sqlite_vec,
        dim=int(embedder.spec.dimensions),
        model_id=embedder.spec.model_id,
    )
    try:
        cached_prompt = store.vectors_by_hash("prompt", numpy)
        cached_reply = store.vectors_by_hash("reply", numpy)
        cached_exchange = store.vectors_by_hash("exchange", numpy)
        if ctx.progress is not None:
            ctx.progress.phase("graph", detail=f"embed {len(nodes.prompts)} prompts")
        prompt_matrix, reused = _encode_with_cache(
            ctx, embedder, numpy, [turn.text for turn in nodes.prompts], cached_prompt
        )
        reply_matrix, _ = _encode_with_cache(
            ctx, embedder, numpy, [turn.text for turn in nodes.replies], cached_reply
        )
        exchange_matrix, _ = _encode_with_cache(
            ctx, embedder, numpy, [text for _id, text in nodes.exchanges], cached_exchange
        )

        # Archetypes over prompts (cluster id per prompt). Density-based
        # HDBSCAN when scikit-learn is installed, greedy cosine otherwise.
        if ctx.progress is not None:
            ctx.progress.phase("graph", detail="cluster archetypes")
        prompt_clusters = _cluster_embeddings(
            numpy, prompt_matrix, _ARCHETYPE_THRESHOLD, import_module=ctx.import_module
        )
        # Re-judge cohesion with a signal orthogonal to the embedding geometry
        # (cross-encoder, else TF-IDF lexical) so weak static vectors can't keep
        # unrelated asks merged into one archetype.
        prompt_clusters, rerank_tier = rerank.rerank_clusters(
            prompt_clusters,
            [turn.text for turn in nodes.prompts],
            import_module=ctx.import_module or __import__("importlib").import_module,
            model_cache=ctx.model_cache,
            allow_download=ctx.policy.allow_download,
        )
        prompt_labels = labels_from_clusters(prompt_clusters, len(nodes.prompts))

        if ctx.progress is not None:
            ctx.progress.phase("graph", detail="persist store")
        prompt_ids = _persist_turns(store, "prompt", nodes.prompts, prompt_matrix, prompt_labels)
        _persist_turns(store, "reply", nodes.replies, reply_matrix, None)
        _persist_exchanges(store, nodes.exchanges, exchange_matrix)
        override = _conversation_summary_override(ctx, embedder, numpy, store)
        conversation_ids, conversation_matrix = _persist_conversations(
            store, numpy, by_conversation, nodes, prompt_matrix, reply_matrix, override
        )

        similar_edges = _build_similar_edges(ctx, store, numpy, prompt_ids, prompt_matrix)
        store.replace_edges([*nodes.structural_edges, *similar_edges])

        workflows = _mine_workflows(seq_mod, nodes, prompt_labels, prompt_ids, by_conversation)
        store.replace_sequences(
            [
                (rank, list(w["pattern"]), w["support"], w["example"])
                for rank, w in enumerate(workflows)
            ]
        )
        counts = store.counts()
        sections = {
            "similar_prompts": _similar_prompts(prompt_clusters, nodes.prompts),
            "recurring_workflows": workflows,
            "skill_suggestions": _skill_suggestions(workflows, prompt_clusters, nodes.prompts),
            "recurring_conversations": _recurring_conversations(
                numpy, conversation_ids, conversation_matrix, import_module=ctx.import_module
            ),
            "forgotten_similar": _forgotten_similar(store, conversation_ids, conversation_matrix),
            "transformation_patterns": _transformation_patterns(
                numpy, nodes, prompt_matrix, reply_matrix, exchange_matrix
            ),
            "store": {"path": str(store_path), "nodes": counts, "edges": counts.get("edges", 0)},
        }
    finally:
        store.close()

    reuse = f"; reused {reused} cached prompt vectors" if reused else ""
    message = (
        f"networked {counts['prompt']} prompts / {counts['reply']} replies across "
        f"{len(conversation_ids)} conversations; "
        f"{len(sections['recurring_workflows'])} workflows{reuse}"
    )
    return InsightsEnrichment(
        level="graph",
        backend=ctx.backend,
        status="ok",
        message=message,
        data=sections,
        provenance={
            "backend": embedder.spec.runtime,
            "model": embedder.spec.model_id,
            "device": embedder.device,
            "rerank": rerank_tier,
        },
    )


def _persist_turns(
    store: t.Any,
    node_type: str,
    turns: list[Turn],
    matrix: t.Any,
    labels: list[int] | None,
) -> list[str]:
    """Persist turn nodes + vectors; return their node ids in order."""
    ids: list[str] = []
    for position, turn in enumerate(turns):
        node_id = _node_id(node_type, turn.conversation_id, turn.position)
        ids.append(node_id)
        store.add_node(
            node_id=node_id,
            node_type=node_type,
            conversation_id=turn.conversation_id,
            text=turn.text,
            content_hash=_content_hash(turn.text),
            vector=matrix[position].tolist(),
            archetype=(labels[position] if labels is not None else None),
        )
    return ids


def _persist_exchanges(store: t.Any, exchanges: list[tuple[str, str]], matrix: t.Any) -> None:
    """Persist exchange (prompt→reply) nodes + vectors."""
    for position, (node_id, text) in enumerate(exchanges):
        conversation_id = node_id.split(":", 2)[1]
        store.add_node(
            node_id=node_id,
            node_type="exchange",
            conversation_id=conversation_id,
            text=text,
            content_hash=_content_hash(text),
            vector=matrix[position].tolist(),
        )


def _persist_conversations(
    store: t.Any,
    numpy: t.Any,
    by_conversation: dict[str, list[Turn]],
    nodes: GraphNodes,
    prompt_matrix: t.Any,
    reply_matrix: t.Any,
    vector_override: t.Any = None,
) -> tuple[list[str], t.Any]:
    """Pool each conversation into a vector + persist.

    The conversation vector is the mean of its *prompt* (human-ask) vectors,
    not all turns: verbose assistant replies dominate and blur a raw
    all-turn mean, collapsing distinct conversations together. The user's
    asks are the sharper topic signature. Conversations with no surviving
    prompt fall back to their reply vectors.

    ``vector_override(conversation_id, user_ask_texts)`` may return a vector
    to use instead of the prompt-mean (the opt-in LLM-summary path); when it
    returns ``None`` the prompt-mean is used.
    """
    prompt_index = {(t.conversation_id, t.position): i for i, t in enumerate(nodes.prompts)}
    reply_index = {(t.conversation_id, t.position): i for i, t in enumerate(nodes.replies)}
    ids: list[str] = []
    vectors: list[t.Any] = []
    for conversation_id, turns in by_conversation.items():
        prompt_rows = [
            prompt_matrix[prompt_index[(conversation_id, turn.position)]]
            for turn in turns
            if turn.role == "user"
        ]
        reply_rows = [
            reply_matrix[reply_index[(conversation_id, turn.position)]]
            for turn in turns
            if turn.role == "assistant"
        ]
        rows = prompt_rows or reply_rows
        if not rows:
            continue
        override = None
        if vector_override is not None:
            user_texts = [turn.text for turn in turns if turn.role == "user"]
            override = vector_override(conversation_id, user_texts)
        vector = override if override is not None else mean_pool(numpy, rows)
        node_id = f"conversation:{conversation_id}"
        snippet = turns[0].text
        store.add_node(
            node_id=node_id,
            node_type="conversation",
            conversation_id=conversation_id,
            text=snippet,
            content_hash=_content_hash(node_id),
            vector=vector.tolist(),
        )
        ids.append(conversation_id)
        vectors.append(vector)
    matrix = numpy.stack(vectors) if vectors else numpy.zeros((0, prompt_matrix.shape[1]))
    return ids, matrix


# Outcome heuristics for the FitnessScore reframe (after hermes-agent-self-
# evolution's multi-signal fitness): rank a workflow by whether it *worked*, not
# just how often it repeats. No ground truth exists, so these read the turns
# around each chain instance.
_RETRY_RE = re.compile(
    r"\b(still (?:failing|broken|not working|doesn'?t)|that (?:didn'?t|did not) work|"
    r"try again|does ?n'?t work|not working|revert|undo that|wrong|that's wrong|no that)\b",
    re.IGNORECASE,
)
_CLOSER_RE = re.compile(
    r"\b(thanks|thank you|perfect|great|lgtm|looks good|ship it|commit (?:it|this|that)|"
    r"that works|nice|awesome)\b",
    re.IGNORECASE,
)


def _conversation_resolved(turns: list[Turn]) -> bool:
    """Decide if the conversation ended cleanly (no trailing retry).

    Reads the last user turn — a retry/negative there signals the work did not
    land; a positive closer (or merely a non-retry) reads as resolved.
    """
    user_turns = [turn for turn in turns if turn.role == "user"]
    if not user_turns:
        return True
    return not _RETRY_RE.search(user_turns[-1].text)


def _is_subsequence(pattern: list[int], sequence: list[int]) -> bool:
    """Return whether ``pattern`` appears as an ordered subsequence of ``sequence``."""
    iterator = iter(sequence)
    return all(symbol in iterator for symbol in pattern)


def _is_contiguous(pattern: list[int], sequence: list[int]) -> bool:
    """Return whether ``pattern`` appears as a contiguous slice of ``sequence``."""
    if not pattern:
        return True
    span = len(pattern)
    return any(sequence[i : i + span] == pattern for i in range(len(sequence) - span + 1))


def _mine_workflows(
    seq_mod: t.Any,
    nodes: GraphNodes,
    prompt_labels: list[int],
    prompt_ids: list[str],
    by_conversation: dict[str, list[Turn]],
) -> list[dict[str, t.Any]]:
    """Mine recurring prompt-archetype chains and rank them by FitnessScore."""
    label_by_id = dict(zip(prompt_ids, prompt_labels, strict=True))
    example_text: dict[int, str] = {}
    for turn, label in zip(nodes.prompts, prompt_labels, strict=True):
        example_text.setdefault(label, turn.text)

    conversation_sequences = {
        conversation_id: seq_mod.collapse_runs([label_by_id[node_id] for node_id in ordered])
        for conversation_id, ordered in nodes.prompt_sequences.items()
    }
    resolved = {
        conversation_id: _conversation_resolved(by_conversation.get(conversation_id, []))
        for conversation_id in conversation_sequences
    }
    sequences = list(conversation_sequences.values())
    frequent = seq_mod.maximal(
        seq_mod.prefixspan(sequences, min_support=_MIN_SUPPORT, min_length=2)
    )

    # composite = 0.5*correctness + 0.3*procedure + 0.2*conciseness - length_penalty
    workflows: list[dict[str, t.Any]] = []
    for fs in frequent:
        pattern = list(fs.pattern)
        distinct = len(set(pattern))
        if distinct < 2:
            continue
        instances = [
            conversation_id
            for conversation_id, sequence in conversation_sequences.items()
            if _is_subsequence(pattern, sequence)
        ]
        if not instances:
            continue
        # correctness: the chain's conversations end without a retry.
        correctness = sum(resolved[c] for c in instances) / len(instances)
        # procedure: the archetypes recur *contiguously*, not scattered.
        procedure = sum(
            _is_contiguous(pattern, conversation_sequences[c]) for c in instances
        ) / len(instances)
        # conciseness: the chain is most of its conversation (little extra churn).
        efficiency = [len(pattern) / max(len(conversation_sequences[c]), 1) for c in instances]
        conciseness = min(1.0, sum(efficiency) / len(efficiency))
        length_penalty = max(0.0, 0.05 * (len(pattern) - 4))
        composite = round(
            max(0.0, 0.5 * correctness + 0.3 * procedure + 0.2 * conciseness - length_penalty),
            3,
        )
        steps = [example_text.get(symbol, f"archetype {symbol}")[:60] for symbol in pattern]
        workflows.append(
            {
                "pattern": pattern,
                "support": fs.support,
                "distinct_archetypes": distinct,
                "score": composite,
                "correctness": round(correctness, 2),
                "procedure": round(procedure, 2),
                "conciseness": round(conciseness, 2),
                "example": " → ".join(steps),
            }
        )
    workflows.sort(key=lambda workflow: (workflow["score"], workflow["support"]), reverse=True)
    return workflows[:_MAX_SECTION]


def _key_terms(texts: list[str]) -> list[str]:
    """Return the most common content terms across ``texts`` (stopword-filtered)."""
    import collections

    from agentgrep.insights.activity import _tokenize

    counter: collections.Counter[str] = collections.Counter()
    for text in texts:
        counter.update(_tokenize(text))
    return [term for term, _ in counter.most_common(4)]


def _similar_prompts(
    prompt_clusters: list[list[int]], prompts: list[Turn]
) -> list[dict[str, t.Any]]:
    """Surface clusters of semantically similar prompts (archetypes)."""
    out: list[dict[str, t.Any]] = []
    for members in prompt_clusters:
        if len(members) < 2:
            continue
        conversations = {prompts[i].conversation_id for i in members}
        out.append(
            {
                "size": len(members),
                "conversations": len(conversations),
                "example": prompts[members[0]].text[:90],
                "members": [prompts[i].text[:70] for i in members[:4]],
            }
        )
    out.sort(key=lambda c: (c["conversations"], c["size"]), reverse=True)
    return out[:_MAX_SECTION]


def _dedup_hyphen(slug: str) -> str:
    """Collapse consecutive duplicate hyphen tokens (``a-a-b`` -> ``a-b``)."""
    out: list[str] = []
    for token in slug.split("-"):
        if token and (not out or out[-1] != token):
            out.append(token)
    return "-".join(out)


def _skill_slug_from_steps(steps: list[str]) -> str:
    """Derive a hyphenated skill name from a workflow chain's steps."""
    parts: list[str] = []
    for raw in steps:
        step = raw.strip()
        if step.startswith("/"):
            parts.append(_dedup_hyphen(step.lstrip("/").split()[0].replace(":", "-")))
        else:
            terms = _key_terms([step])
            parts.append("-".join(terms[:2]) if terms else "ask")
    slug = "-then-".join(part for part in parts if part)
    while len(slug) > 48 and "-then-" in slug:
        slug = slug.rsplit("-then-", 1)[0]
    return slug or "macro"


def _skill_suggestions(
    workflows: list[dict[str, t.Any]],
    prompt_clusters: list[list[int]],
    prompts: list[Turn],
) -> list[dict[str, t.Any]]:
    """Suggest new Skills that would reduce the user's repeated work.

    Two sources: recurring multi-step workflows (→ a *macro* skill that runs
    the chain) and recurring varied asks (→ a *template* skill that
    parameterizes the request). Deterministic — names come from slash
    commands or the cluster's top terms.
    """
    macros: list[dict[str, t.Any]] = []
    for workflow in workflows[:_MAX_MACRO_SUGGESTIONS]:
        if workflow["support"] < _MIN_MACRO_SUPPORT:
            continue
        steps = [step.strip() for step in str(workflow["example"]).split(" → ")]
        macros.append(
            {
                "type": "macro",
                "name": _skill_slug_from_steps(steps),
                "evidence": f"recurred {workflow['support']}x: "
                + " → ".join(step[:28] for step in steps),
                "rationale": "A skill that runs this sequence in one step.",
                "support": workflow["support"],
                "steps": steps,
            }
        )
    templates: list[dict[str, t.Any]] = []
    for members in prompt_clusters:
        if len(members) < 3:
            continue
        conversations = {prompts[i].conversation_id for i in members}
        if len(conversations) < 2:
            continue
        texts = [prompts[i].text for i in members]
        if texts[0].lstrip().startswith("/"):
            continue  # already a slash command
        terms = _key_terms(texts)
        if not terms:
            continue
        templates.append(
            {
                "type": "template",
                "name": "-".join(terms[:3]),
                "evidence": f"{len(members)} similar asks across {len(conversations)} "
                f"conversations, e.g. {texts[0][:50]!r}",
                "rationale": "A parameterized skill for this recurring request.",
                "support": len(members),
                "conversations": len(conversations),
                # Reuse value: a recurring ask that spans many conversations is a
                # better skill candidate than one repeated inside a single thread.
                "score": len(members) * len(conversations),
                "terms": terms[:6],
                "examples": [text.strip()[:200] for text in texts[:4]],
            }
        )
    # Rank the recurring-ask templates by reuse value so the most broadly-repeated
    # asks surface first; macros (multi-step chains) are already fitness-ranked.
    templates.sort(key=lambda suggestion: suggestion["score"], reverse=True)
    seen: set[str] = set()
    unique: list[dict[str, t.Any]] = []
    for suggestion in (*macros, *templates):
        if suggestion["name"] in seen:
            continue
        seen.add(suggestion["name"])
        unique.append(suggestion)
    return unique[:_MAX_SKILL_SUGGESTIONS]


def _recurring_conversations(
    numpy: t.Any,
    conversation_ids: list[str],
    matrix: t.Any,
    *,
    import_module: t.Any = None,
) -> list[dict[str, t.Any]]:
    """Cluster conversation vectors; a dense cluster = a repeated conversation."""
    if matrix.shape[0] < 2:
        return []
    clusters = _cluster_embeddings(numpy, matrix, _ARCHETYPE_THRESHOLD, import_module=import_module)
    out: list[dict[str, t.Any]] = []
    for members in clusters:
        if len(members) < 2:
            continue
        out.append(
            {
                "size": len(members),
                "conversations": [conversation_ids[m] for m in members[:6]],
            }
        )
    out.sort(key=lambda item: item["size"], reverse=True)
    return out[:_MAX_SECTION]


def _forgotten_similar(
    store: t.Any,
    conversation_ids: list[str],
    matrix: t.Any,
) -> list[dict[str, t.Any]]:
    """For the most recent conversation, find nearest past conversations via vec0."""
    if matrix.shape[0] < 2:
        return []
    latest_id = conversation_ids[-1]
    neighbors = store.knn(
        "conversation",
        matrix[-1].tolist(),
        k=5,
        exclude_node_id=f"conversation:{latest_id}",
    )
    return [
        {
            "conversation": node_id.split(":", 1)[1],
            "similarity": round(1.0 - distance, 4),
        }
        for node_id, distance in neighbors
    ]


def _transformation_patterns(
    numpy: t.Any,
    nodes: GraphNodes,
    prompt_matrix: t.Any,
    reply_matrix: t.Any,
    exchange_matrix: t.Any,
) -> list[dict[str, t.Any]]:
    """Cluster prompt→reply delta vectors into recurring transformation types."""
    prompt_index = {(t.conversation_id, t.position): i for i, t in enumerate(nodes.prompts)}
    reply_index = {(t.conversation_id, t.position): i for i, t in enumerate(nodes.replies)}
    deltas: list[t.Any] = []
    labels_text: list[str] = []
    for node_id, _text in nodes.exchanges:
        _kind, conversation_id, user_position = node_id.split(":", 2)
        p = prompt_index.get((conversation_id, int(user_position)))
        r = reply_index.get((conversation_id, int(user_position) + 1))
        if p is None or r is None:
            continue
        delta = reply_matrix[r] - prompt_matrix[p]
        magnitude = float(numpy.linalg.norm(delta)) or 1.0
        deltas.append(delta / magnitude)
        labels_text.append(nodes.prompts[p].text[:60])
    if len(deltas) < 2:
        return []
    delta_matrix = numpy.stack(deltas)
    clusters = _greedy_clusters(numpy, delta_matrix, _TRANSFORM_THRESHOLD)
    out: list[dict[str, t.Any]] = []
    for members in clusters:
        if len(members) < 2:
            continue
        out.append({"size": len(members), "example_prompt": labels_text[members[0]]})
    out.sort(key=lambda item: item["size"], reverse=True)
    return out[:_MAX_SECTION]
