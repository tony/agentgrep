"""Tests for the graph engine: pure helpers + a real sqlite-vec integration."""

from __future__ import annotations

import pathlib
import types
import typing as t

import pytest

import agentgrep
from agentgrep.insights.enrichers import graph as graph_mod


def _importer(modules: dict[str, t.Any]) -> t.Callable[[str], t.Any]:
    """Return a fake importer that resolves only the given modules."""

    def _imp(name: str) -> t.Any:
        if name in modules:
            return modules[name]
        raise ImportError(name)

    return _imp


def _rec(
    text: str,
    *,
    conversation: str,
    role: str = "user",
    kind: t.Literal["prompt", "history"] = "prompt",
) -> agentgrep.SearchRecord:
    """Build a synthetic conversation record."""
    return agentgrep.SearchRecord(
        kind=kind,
        agent="claude",
        store="claude.projects",
        adapter_id="a",
        path=pathlib.Path("/x/f.jsonl"),
        text=text,
        role=role,
        conversation_id=conversation,
    )


def test_claude_event_human_vs_tool() -> None:
    """The adapter separates typed prompts from tool results / subagent output."""
    human = {"type": "user", "promptSource": "cli", "message": {"role": "user", "content": "hi"}}
    tool = {
        "type": "user",
        "toolUseResult": {"x": 1},
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "out"}]},
    }
    stdout = {
        "type": "user",
        "message": {"content": "<local-command-stdout>done</local-command-stdout>"},
    }
    assistant = {"type": "assistant", "message": {"role": "assistant", "content": []}}
    assert agentgrep.claude_event_is_human_authored(human) is True
    assert agentgrep.claude_event_is_human_authored(tool) is False
    assert agentgrep.claude_event_is_human_authored(stdout) is False
    assert agentgrep.claude_event_is_human_authored(assistant) is False


# --- pure helpers ----------------------------------------------------------


def test_looks_like_user_ask_filters_tool_noise() -> None:
    """Short prose asks are kept; tool output and dumps are dropped."""
    assert graph_mod.looks_like_user_ask("rebase onto trunk and resolve conflicts")
    assert not graph_mod.looks_like_user_ask("[master 3a9497f] ci: add gate")
    assert not graph_mod.looks_like_user_ask("x" * 2500)
    assert not graph_mod.looks_like_user_ask("M .tmux\n M .tmuxp\n M .zshrc")


def test_normalize_ask_collapses_slash_commands() -> None:
    """A wrapped slash-command invocation collapses to clean ``/name args``."""
    wrapped = "<command-message>pr:pr</command-message>\n<command-name>/pr:pr</command-name>"
    assert graph_mod.normalize_ask(wrapped) == "/pr:pr"
    with_args = "<command-name>/pr:deslop</command-name><command-args>--apply-rebase</command-args>"
    assert graph_mod.normalize_ask(with_args) == "/pr:deslop --apply-rebase"
    assert graph_mod.normalize_ask("just a normal ask") == "just a normal ask"


def test_reconstruct_turns_classifies_roles_in_order() -> None:
    """Turns group by conversation, keep emission order, and route roles."""
    records = [
        _rec("add a lint rule", conversation="c1"),
        _rec("use the registry", conversation="c1", role="assistant", kind="history"),
        _rec("write a test", conversation="c1"),
    ]
    turns = graph_mod.reconstruct_turns(records)
    assert [(turn.position, turn.role) for turn in turns["c1"]] == [
        (0, "user"),
        (1, "assistant"),
        (2, "user"),
    ]


def test_reconstruct_turns_drops_tool_noise_prompts() -> None:
    """A tool-output user turn is dropped from the reconstructed turns."""
    records = [
        _rec("add a lint rule", conversation="c1"),
        _rec("[master abc1234] chore: bump deps", conversation="c1"),
    ]
    turns = graph_mod.reconstruct_turns(records)
    assert [turn.text for turn in turns["c1"]] == ["add a lint rule"]


def test_extract_nodes_builds_structural_edges_and_sequences() -> None:
    """Exchanges, next/responds_to edges, and prompt sequences are derived."""
    records = [
        _rec("first ask", conversation="c1"),
        _rec("a reply", conversation="c1", role="assistant", kind="history"),
        _rec("second ask", conversation="c1"),
    ]
    nodes = graph_mod.extract_nodes(graph_mod.reconstruct_turns(records))
    assert len(nodes.prompts) == 2
    assert len(nodes.replies) == 1
    assert len(nodes.exchanges) == 1
    kinds = {edge[2] for edge in nodes.structural_edges}
    assert {"next", "responds_to"} <= kinds
    assert nodes.prompt_sequences["c1"] == ["prompt:c1:0", "prompt:c1:2"]


def test_topk_similar_edges_links_identical_vectors() -> None:
    """Identical prompt vectors across conversations become a similar edge."""
    numpy = pytest.importorskip("numpy")
    matrix = numpy.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)
    edges = graph_mod.topk_similar_edges(numpy, matrix, ["a", "b", "c"], k=2, threshold=0.99)
    assert ("a", "b", "similar", 1.0) in edges


# --- HDBSCAN clustering with the import seam --------------------------------


def _fake_sklearn_cluster(labels: list[int]) -> t.Any:
    """Fake ``sklearn.cluster`` whose HDBSCAN returns fixed ``labels``."""

    class _HDBSCAN:
        def __init__(self, **_kwargs: t.Any) -> None:
            pass

        def fit_predict(self, _matrix: t.Any) -> list[int]:
            return labels

    return types.SimpleNamespace(HDBSCAN=_HDBSCAN)


def test_cluster_embeddings_uses_hdbscan_and_keeps_noise_as_singletons() -> None:
    """HDBSCAN labels become member-lists; ``-1`` noise becomes singletons."""
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.enrichers.embeddings import _cluster_embeddings

    # rows 0,2 cohesive on axis 0; rows 1,3 cohesive on axis 1; row 4 distinct.
    matrix = numpy.array(
        [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0], [0.7, 0.7]],
        dtype=numpy.float32,
    )
    # HDBSCAN says: rows 0,2 -> cluster 0; rows 1,3 -> cluster 1; row 4 -> noise.
    importer = _importer({"sklearn.cluster": _fake_sklearn_cluster([0, 1, 0, 1, -1])})
    clusters = _cluster_embeddings(numpy, matrix, 0.62, import_module=importer)
    # Real clusters first (size-sorted), then the noise singleton.
    assert clusters == [[0, 2], [1, 3], [4]]
    covered = sorted(i for members in clusters for i in members)
    assert covered == [0, 1, 2, 3, 4]


def test_cluster_embeddings_splits_incohesive_hdbscan_clusters() -> None:
    """A member far from its HDBSCAN cluster centroid is demoted to a singleton."""
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.enrichers.embeddings import _cluster_embeddings

    # rows 0,1 cohesive; row 2 is orthogonal but HDBSCAN lumped it into cluster 0.
    matrix = numpy.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)
    importer = _importer({"sklearn.cluster": _fake_sklearn_cluster([0, 0, 0])})
    clusters = _cluster_embeddings(numpy, matrix, 0.62, import_module=importer)
    assert [0, 1] in clusters
    assert [2] in clusters


def test_cluster_embeddings_falls_back_to_greedy_without_sklearn() -> None:
    """Without scikit-learn the greedy cosine pass produces the partition."""
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.enrichers.embeddings import _cluster_embeddings, _greedy_clusters

    matrix = numpy.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)
    importer = _importer({})  # resolves nothing -> ImportError on sklearn.cluster
    clusters = _cluster_embeddings(numpy, matrix, 0.99, import_module=importer)
    assert clusters == _greedy_clusters(numpy, matrix, 0.99)


def test_cluster_embeddings_falls_back_when_hdbscan_raises() -> None:
    """A backend that rejects the metric degrades to the greedy pass."""
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.enrichers.embeddings import _cluster_embeddings, _greedy_clusters

    class _Raising:
        def __init__(self, **_kwargs: t.Any) -> None:
            pass

        def fit_predict(self, _matrix: t.Any) -> list[int]:
            message = "unsupported metric"
            raise ValueError(message)

    matrix = numpy.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=numpy.float32)
    importer = _importer({"sklearn.cluster": types.SimpleNamespace(HDBSCAN=_Raising)})
    clusters = _cluster_embeddings(numpy, matrix, 0.62, import_module=importer)
    assert clusters == _greedy_clusters(numpy, matrix, 0.62)


# --- real sqlite-vec integration -------------------------------------------


def _fake_model2vec() -> t.Any:
    """Fake model2vec: identical text -> identical deterministic vector."""
    numpy = pytest.importorskip("numpy")

    class _Static:
        @classmethod
        def from_pretrained(cls, _path: str) -> _Static:
            return cls()

        def encode(self, texts: list[str]) -> t.Any:
            # Match potion-base-8M's declared 256 dims so the persisted vec0
            # table dimension (taken from the model spec) lines up.
            return numpy.array(
                [numpy.random.RandomState(abs(hash(text)) % 2**31).randn(256) for text in texts],
                dtype=float,
            )

    return types.SimpleNamespace(StaticModel=_Static)


def test_build_graph_persists_and_mines_workflow(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a repeated prompt chain across conversations is mined and stored."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights import build_report, models as models_mod
    from agentgrep.insights.model import ReportRequest

    monkeypatch.setenv("AGENTGREP_CACHE_DIR", str(tmp_path / "cache"))
    spec = models_mod.resolve_embedding_model("potion-base-8M")
    assert spec is not None
    target = models_mod.model_cache_path(spec, tmp_path / "models")
    target.mkdir(parents=True, exist_ok=True)
    (target / "agentgrep-manifest.json").write_text("{}", encoding="utf-8")

    chain = ["add a lint rule", "write a test for it", "document the rule"]
    records = []
    for conversation in ("c1", "c2", "c3"):
        for ask in chain:
            records.append(_rec(ask, conversation=conversation))
            records.append(
                _rec("here you go", conversation=conversation, role="assistant", kind="history")
            )

    available = {"model2vec": _fake_model2vec(), "numpy": numpy, "sqlite_vec": sqlite_vec}

    def importer(name: str) -> t.Any:
        if name not in available:
            raise ImportError(name)
        return available[name]

    report = build_report(
        records,
        ReportRequest(requested_level="graph"),
        import_module=importer,
        model_cache=tmp_path / "models",
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.data["store"]["nodes"]["prompt"] == 9
    # The shared 3-step chain recurs across all three conversations.
    workflows = enrichment.data["recurring_workflows"]
    assert any(w["support"] == 3 and len(w["pattern"]) == 3 for w in workflows)
    # Similar-prompt clusters and skill suggestions are surfaced.
    assert enrichment.data["similar_prompts"]
    assert enrichment.data["skill_suggestions"]
    assert any(s["type"] == "macro" for s in enrichment.data["skill_suggestions"])
    assert pathlib.Path(enrichment.data["store"]["path"]).is_file()


def test_graphstore_rebuilds_on_model_change(tmp_path: pathlib.Path) -> None:
    """Switching the embedding model drops stale vectors instead of mixing them."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.graphstore import GraphStore

    path = tmp_path / "graph.db"
    store = GraphStore.open(path, sqlite_vec=sqlite_vec, dim=4, model_id="model-a")
    store.add_node(
        node_id="prompt:c1:0",
        node_type="prompt",
        conversation_id="c1",
        text="hello",
        content_hash="h1",
        vector=[0.5, 0.5, 0.5, 0.5],
    )
    store.close()

    # Reopening with the same model keeps the node.
    same = GraphStore.open(path, sqlite_vec=sqlite_vec, dim=4, model_id="model-a")
    assert same.existing_hashes() == {"h1"}
    assert "h1" in same.vectors_by_hash("prompt", numpy)
    same.close()

    # Reopening with a different model rebuilds (stale vectors dropped).
    changed = GraphStore.open(path, sqlite_vec=sqlite_vec, dim=4, model_id="model-b")
    assert changed.existing_hashes() == set()
    changed.close()


def test_graphstore_summary_cache_roundtrips(tmp_path: pathlib.Path) -> None:
    """Conversation summaries persist and survive reopening with the same model."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    pytest.importorskip("numpy")
    from agentgrep.insights.graphstore import GraphStore

    path = tmp_path / "graph.db"
    store = GraphStore.open(path, sqlite_vec=sqlite_vec, dim=4, model_id="model-a")
    assert store.get_summary("h1") is None
    store.set_summary("h1", "Wanted to add a lint rule and test it.")
    store.close()

    reopened = GraphStore.open(path, sqlite_vec=sqlite_vec, dim=4, model_id="model-a")
    assert reopened.get_summary("h1") == "Wanted to add a lint rule and test it."
    reopened.close()


def test_persist_conversations_uses_vector_override(tmp_path: pathlib.Path) -> None:
    """When a vector override is supplied, it replaces the prompt-mean vector."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.graphstore import GraphStore

    turns = {
        "c1": [
            graph_mod.Turn(conversation_id="c1", position=0, role="user", text="add a lint rule"),
            graph_mod.Turn(conversation_id="c1", position=1, role="assistant", text="done"),
        ]
    }
    nodes = graph_mod.extract_nodes(turns)
    prompt_matrix = numpy.array([[1.0, 0.0, 0.0, 0.0]], dtype=numpy.float32)
    reply_matrix = numpy.array([[0.0, 1.0, 0.0, 0.0]], dtype=numpy.float32)
    override_vector = numpy.array([0.0, 0.0, 1.0, 0.0], dtype=numpy.float32)

    seen: list[list[str]] = []

    def _override(_conversation_id: str, asks: list[str]) -> t.Any:
        seen.append(asks)
        return override_vector

    store = GraphStore.open(tmp_path / "g.db", sqlite_vec=sqlite_vec, dim=4, model_id="m")
    try:
        ids, matrix = graph_mod._persist_conversations(
            store, numpy, turns, nodes, prompt_matrix, reply_matrix, _override
        )
    finally:
        store.close()

    assert ids == ["c1"]
    assert seen == [["add a lint rule"]]  # override saw the user asks
    assert numpy.allclose(matrix[0], override_vector)  # not the prompt-mean


def _counting_model2vec() -> tuple[t.Any, list[int]]:
    """Fake model2vec that records how many texts it encodes per call."""
    numpy = pytest.importorskip("numpy")
    calls: list[int] = []

    class _Static:
        @classmethod
        def from_pretrained(cls, _path: str) -> _Static:
            return cls()

        def encode(self, texts: list[str]) -> t.Any:
            calls.append(len(texts))
            return numpy.array(
                [numpy.random.RandomState(abs(hash(text)) % 2**31).randn(256) for text in texts],
                dtype=float,
            )

    return types.SimpleNamespace(StaticModel=_Static), calls


def test_build_graph_reuses_cached_vectors_on_rerun(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run over unchanged turns re-encodes nothing (vectors reused)."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights import build_report, models as models_mod
    from agentgrep.insights.model import ReportRequest

    monkeypatch.setenv("AGENTGREP_CACHE_DIR", str(tmp_path / "cache"))
    spec = models_mod.resolve_embedding_model("potion-base-8M")
    assert spec is not None
    target = models_mod.model_cache_path(spec, tmp_path / "models")
    target.mkdir(parents=True, exist_ok=True)
    (target / "agentgrep-manifest.json").write_text("{}", encoding="utf-8")

    records = []
    for conversation in ("c1", "c2"):
        for ask in ("add a lint rule", "write a test"):
            records.append(_rec(ask, conversation=conversation))
            records.append(
                _rec("here you go", conversation=conversation, role="assistant", kind="history")
            )

    fake_module, calls = _counting_model2vec()
    available = {"model2vec": fake_module, "numpy": numpy, "sqlite_vec": sqlite_vec}

    def importer(name: str) -> t.Any:
        if name not in available:
            raise ImportError(name)
        return available[name]

    def run() -> t.Any:
        return build_report(
            records,
            ReportRequest(requested_level="graph"),
            import_module=importer,
            model_cache=tmp_path / "models",
        )

    run()
    first_total = sum(calls)
    assert first_total > 0  # cold run encodes every turn
    calls.clear()
    run()
    # Same turns, same text -> nothing re-encoded on the second run.
    assert sum(calls) == 0


def _normalize(numpy: t.Any, rows: list[list[float]]) -> t.Any:
    """Row-normalize a small float32 matrix for vector-backend tests."""
    matrix = numpy.array(rows, dtype=numpy.float32)
    return matrix / (numpy.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)


def test_topk_similar_edges_via_knn_matches_dense(tmp_path: pathlib.Path) -> None:
    """The per-node sqlite-vec kNN edge path matches the dense matrix path."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    numpy = pytest.importorskip("numpy")
    from agentgrep.insights.graphstore import GraphStore

    matrix = _normalize(numpy, [[1, 0, 0], [0.98, 0.02, 0], [0, 1, 0], [0, 0, 1]])
    ids = ["a", "b", "c", "d"]
    store = GraphStore.open(tmp_path / "g.db", sqlite_vec=sqlite_vec, dim=3)
    try:
        for index, node_id in enumerate(ids):
            store.add_node(
                node_id=node_id,
                node_type="prompt",
                conversation_id="conv",
                text=node_id,
                content_hash=node_id,
                vector=matrix[index].tolist(),
            )
        dense = {
            (a, b)
            for a, b, _kind, _w in graph_mod.topk_similar_edges(
                numpy, matrix, ids, k=3, threshold=0.5
            )
        }
        streamed = {
            (a, b)
            for a, b, _kind, _w in graph_mod.topk_similar_edges_via_knn(
                lambda vector, k: store.knn("prompt", vector, k=k),
                matrix,
                ids,
                k=3,
                threshold=0.5,
            )
        }
    finally:
        store.close()
    assert dense == streamed == {("a", "b")}


def test_lance_vector_backend_returns_nearest(tmp_path: pathlib.Path) -> None:
    """The LanceDB backend builds a table and answers nearest-neighbor queries."""
    pytest.importorskip("lancedb")
    numpy = pytest.importorskip("numpy")
    import importlib

    from agentgrep.insights.lancestore import LanceVectorBackend

    matrix = _normalize(numpy, [[1, 0, 0], [0.98, 0.02, 0], [0, 1, 0]])
    backend = LanceVectorBackend.build(
        tmp_path / "lance", ["a", "b", "c"], matrix, import_module=importlib.import_module
    )
    neighbors = backend.knn(matrix[0].tolist(), 2)
    ids = [node_id for node_id, _distance in neighbors]
    assert ids[0] == "a"  # nearest to itself
    assert "b" in ids  # then its close neighbor, not the orthogonal "c"


def test_fitness_heuristics_pure() -> None:
    """The subsequence/contiguity/resolution helpers behave as documented."""
    assert graph_mod._is_subsequence([0, 2], [0, 1, 2, 3]) is True
    assert graph_mod._is_subsequence([2, 0], [0, 1, 2, 3]) is False
    assert graph_mod._is_contiguous([1, 2], [0, 1, 2, 3]) is True
    assert graph_mod._is_contiguous([0, 2], [0, 1, 2, 3]) is False
    resolved = [graph_mod.Turn("c", 0, "user", "deploy the app")]
    retried = [
        graph_mod.Turn("c", 0, "user", "deploy the app"),
        graph_mod.Turn("c", 1, "user", "still failing"),
    ]
    assert graph_mod._conversation_resolved(resolved) is True
    assert graph_mod._conversation_resolved(retried) is False


def test_mine_workflows_ranks_resolved_chain_above_retried() -> None:
    """A recurring chain that resolves outranks an equally-frequent retried one."""
    from agentgrep.insights import sequences as seq_mod

    records = []
    # Chain A (deploy -> migrate) recurs in two conversations that end cleanly.
    for conversation in ("a1", "a2"):
        records.append(_rec("deploy the service", conversation=conversation))
        records.append(_rec("ok done", conversation=conversation, role="assistant", kind="history"))
        records.append(_rec("run the migration", conversation=conversation))
        records.append(_rec("perfect thanks", conversation=conversation))
    # Chain B (patch -> log) recurs in two conversations that end on a retry.
    for conversation in ("b1", "b2"):
        records.append(_rec("patch the parser", conversation=conversation))
        records.append(_rec("ok", conversation=conversation, role="assistant", kind="history"))
        records.append(_rec("check the logs", conversation=conversation))
        records.append(_rec("still failing", conversation=conversation))

    by_conversation = graph_mod.reconstruct_turns(records)
    nodes = graph_mod.extract_nodes(by_conversation)
    texts = [turn.text for turn in nodes.prompts]
    label_of = {text: index for index, text in enumerate(dict.fromkeys(texts))}
    prompt_labels = [label_of[text] for text in texts]
    prompt_ids = [
        graph_mod._node_id("prompt", turn.conversation_id, turn.position) for turn in nodes.prompts
    ]

    workflows = graph_mod._mine_workflows(
        seq_mod, nodes, prompt_labels, prompt_ids, by_conversation
    )
    assert workflows, "expected at least the two recurring chains"
    by_lead = {w["example"].split(" → ")[0][:12]: w for w in workflows}
    deploy = next(w for k, w in by_lead.items() if k.startswith("deploy"))
    patch = next(w for k, w in by_lead.items() if k.startswith("patch"))
    assert deploy["correctness"] == 1.0
    assert patch["correctness"] == 0.0
    assert deploy["score"] > patch["score"]
    assert workflows[0]["example"].startswith("deploy")  # resolved chain leads


def test_skill_suggestions_ranks_templates_and_lifts_cap() -> None:
    """Recurring-ask templates rank by reuse value, past the old hard cap of 8."""
    words = [
        "refactor",
        "deploy",
        "benchmark",
        "document",
        "migrate",
        "audit",
        "optimize",
        "cluster",
        "embed",
        "rerank",
        "summarize",
        "validate",
    ]
    prompts: list[graph_mod.Turn] = []
    clusters: list[list[int]] = []
    # 12 distinct recurring asks with decreasing support (14..3), each spanning
    # three conversations, so the raised cap and the ranking are both observable.
    for index, word in enumerate(words):
        support = 14 - index
        members: list[int] = []
        for occurrence in range(support):
            members.append(len(prompts))
            prompts.append(
                graph_mod.Turn(
                    conversation_id=f"conv-{index}-{occurrence % 3}",
                    position=occurrence,
                    role="user",
                    text=f"{word} the whole codebase now",
                )
            )
        clusters.append(members)

    suggestions = graph_mod._skill_suggestions([], clusters, prompts)
    templates = [s for s in suggestions if s["type"] == "template"]
    assert len(templates) > 8  # the old hard cap of 8 is lifted
    scores = [s["score"] for s in templates]
    assert scores == sorted(scores, reverse=True)  # ranked by reuse value desc


def test_skill_suggestions_drops_barely_recurring_macros() -> None:
    """A macro leads the list, so a support-2 chain must not be surfaced there."""
    workflows = [
        {"support": 2, "example": "commit → push", "pattern": ["commit", "push"]},
        {"support": 4, "example": "test → commit → push", "pattern": ["test", "commit", "push"]},
    ]
    suggestions = graph_mod._skill_suggestions(workflows, [], [])
    macros = [s for s in suggestions if s["type"] == "macro"]
    assert [s["support"] for s in macros] == [4]  # the support-2 chain is dropped
