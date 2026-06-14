"""Tests for the L1-L5 enrichers using injected fake backend modules.

No real scikit-learn, sentence-transformers, tantivy, LanceDB, or httpx is
needed: each backend is supplied as a fake module through the
``import_module`` seam, so the whole ladder runs in the base environment.
"""

from __future__ import annotations

import json
import pathlib
import types
import typing as t

import numpy as np

import agentgrep
from agentgrep.insights import build_report, models as models_mod
from agentgrep.insights.model import ReportRequest


def _rec(text: str, *, session_id: str | None = None) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for enricher tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="claude",
        store="proj",
        adapter_id="adapter.v1",
        path=pathlib.Path("/x/proj/file.jsonl"),
        text=text,
        timestamp="2026-06-10T10:00:00Z",
        session_id=session_id,
    )


def _importer(modules: dict[str, t.Any]) -> t.Callable[[str], t.Any]:
    """Return a fake importer that resolves only the given modules."""

    def _imp(name: str) -> t.Any:
        if name in modules:
            return modules[name]
        message = name
        raise ImportError(message)

    return _imp


_RECORDS = [
    _rec("Configure the tantivy parser", session_id="s1"),
    _rec("Add a sqlite-vec vector index", session_id="s2"),
    _rec("Configure the tantivy parser", session_id="s3"),
    _rec("Refactor the report builder for clarity", session_id="s4"),
]


# --- L1 html ---------------------------------------------------------------


def test_html_enricher_renders_via_jinja2() -> None:
    """The html level renders the report payload through a (fake) jinja2 Template."""

    class _Template:
        def __init__(self, text: str, autoescape: bool = False) -> None:
            self._text = text

        def render(self, **context: object) -> str:
            return "<html>RENDERED REPORT</html>"

    fake_jinja2 = types.SimpleNamespace(Template=_Template)

    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="html"),
        import_module=_importer({"jinja2": fake_jinja2}),
    )
    assert report.level == "html"
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert "RENDERED REPORT" in enrichment.data["html"]


# --- L2 ml -----------------------------------------------------------------


def test_ml_enricher_produces_topics() -> None:
    """The ml level clusters via fake TF-IDF + KMeans backed by real numpy."""

    class _Tfidf:
        def __init__(self, **_kwargs: object) -> None:
            self._n = 0

        def fit_transform(self, texts: list[str]) -> t.Any:
            self._n = len(texts)
            return np.eye(len(texts), dtype=float)

        def get_feature_names_out(self) -> t.Any:
            return np.array([f"term{i}" for i in range(self._n)])

    class _KMeans:
        def __init__(self, n_clusters: int, **_kwargs: object) -> None:
            self.n_clusters = n_clusters
            self.cluster_centers_: t.Any = None

        def fit_predict(self, matrix: t.Any) -> t.Any:
            self.cluster_centers_ = np.ones((self.n_clusters, matrix.shape[1]))
            return np.array([i % self.n_clusters for i in range(matrix.shape[0])])

    modules = {
        "sklearn": types.SimpleNamespace(),
        "sklearn.feature_extraction.text": types.SimpleNamespace(TfidfVectorizer=_Tfidf),
        "sklearn.cluster": types.SimpleNamespace(KMeans=_KMeans),
    }

    report = build_report(
        _RECORDS, ReportRequest(requested_level="ml"), import_module=_importer(modules)
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.data["n_clusters"] >= 2
    assert enrichment.data["topics"]


# --- L3 embeddings ---------------------------------------------------------


def _provision_fake_model(tmp_path: pathlib.Path) -> None:
    """Write a manifest so the embedding model counts as installed."""
    spec = models_mod.resolve_embedding_model("potion-base-8M")
    assert spec is not None
    target = models_mod.model_cache_path(spec, tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "agentgrep-manifest.json").write_text("{}", encoding="utf-8")


def _fake_model2vec() -> t.Any:
    """Return a fake model2vec whose encoder maps text to a deterministic vector."""

    class _Static:
        @classmethod
        def from_pretrained(cls, _path: str) -> _Static:
            return cls()

        def encode(self, texts: list[str]) -> t.Any:
            return np.array(
                [
                    [float(len(text)), float(text.count("a")), float(text.count("e"))]
                    for text in texts
                ],
                dtype=float,
            )

    return types.SimpleNamespace(StaticModel=_Static)


def test_embeddings_enricher_clusters_and_dedupes(tmp_path: pathlib.Path) -> None:
    """The embeddings level embeds via fake model2vec and flags duplicates."""
    _provision_fake_model(tmp_path)
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="embeddings"),
        import_module=_importer({"model2vec": _fake_model2vec(), "numpy": np}),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.provenance is not None
    assert enrichment.provenance["model"] == "potion-base-8M"
    # The two identical "Configure the tantivy parser" prompts are duplicates.
    assert enrichment.data["duplicates"]


def test_embeddings_enricher_errors_when_model_not_provisioned(tmp_path: pathlib.Path) -> None:
    """An unprovisioned model yields an error enrichment with an install hint."""
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="embeddings", allow_download=False),
        import_module=_importer({"model2vec": _fake_model2vec(), "numpy": np}),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "error"
    assert report.status == "partial"
    setup = next(d.setup_command for d in report.diagnostics if d.setup_command)
    assert "models install" in setup


# --- L4 index --------------------------------------------------------------


def _fake_tantivy() -> t.Any:
    """Return a fake tantivy module sufficient for build + sample query."""

    class _Doc:
        def __init__(self, **fields: str) -> None:
            self._fields = {key: [value] for key, value in fields.items()}

        def __getitem__(self, key: str) -> list[str]:
            return self._fields[key]

    class _Writer:
        def __init__(self) -> None:
            self.docs: list[_Doc] = []

        def add_document(self, doc: _Doc) -> None:
            self.docs.append(doc)

        def commit(self) -> None:
            pass

    class _Searcher:
        def __init__(self, docs: list[_Doc]) -> None:
            self._docs = docs

        def search(self, _query: object, count: int) -> t.Any:
            hits = [(1.0, i) for i in range(min(count, len(self._docs)))]
            return types.SimpleNamespace(hits=hits)

        def doc(self, address: int) -> _Doc:
            return self._docs[address]

    class _Index:
        def __init__(self, _schema: object, path: str | None = None) -> None:
            self._writer: _Writer | None = None
            self._docs: list[_Doc] = []

        def writer(self) -> _Writer:
            self._writer = _Writer()
            return self._writer

        def reload(self) -> None:
            assert self._writer is not None
            self._docs = self._writer.docs

        def searcher(self) -> _Searcher:
            return _Searcher(self._docs)

        def parse_query(self, term: str, _fields: list[str]) -> object:
            return ("query", term)

    class _SchemaBuilder:
        def add_text_field(self, _name: str, stored: bool = False) -> None:
            pass

        def build(self) -> object:
            return object()

    return types.SimpleNamespace(SchemaBuilder=_SchemaBuilder, Index=_Index, Document=_Doc)


def test_index_enricher_builds_fulltext_only_without_embeddings(
    tmp_path: pathlib.Path, monkeypatch: t.Any
) -> None:
    """The tantivy index builds (full-text only) when no embedding backend exists."""
    monkeypatch.setenv("AGENTGREP_CACHE_DIR", str(tmp_path))
    modules = {
        "tantivy": _fake_tantivy(),
        "sqlite_vec": types.ModuleType("sqlite_vec"),
        "numpy": np,
    }
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="index", index_backend="tantivy"),
        import_module=_importer(modules),
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.data["documents_indexed"] == len(_RECORDS)
    assert enrichment.data["vectors_included"] is False
    assert enrichment.data["hits"]


# --- L5 llm ----------------------------------------------------------------


class _RecordingProgress:
    """A progress sink that records streamed LLM deltas."""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.phases: list[str] = []

    def phase(self, name: str, *, detail: str = "") -> None:
        self.phases.append(name)

    def download_progress(
        self, *, model: str, downloaded_bytes: int, total_bytes: int | None
    ) -> None:
        pass

    def llm_chunk(self, *, backend: str, model: str, delta: str, char_count: int) -> None:
        self.deltas.append(delta)


def _fake_httpx() -> t.Any:
    """Return a fake httpx whose stream yields NDJSON chat chunks."""

    class _Stream:
        def __init__(self, lines: list[str]) -> None:
            self._lines = lines

        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def raise_for_status(self) -> None:
            pass

        def iter_lines(self) -> t.Iterator[str]:
            yield from self._lines

    def stream(_method: str, _url: str, **_kwargs: object) -> _Stream:
        return _Stream(
            [
                json.dumps({"message": {"content": "Worked on "}}),
                json.dumps({"message": {"content": "indexing."}, "done": True}),
            ]
        )

    return types.SimpleNamespace(stream=stream)


def test_llm_enricher_streams_grounded_summary() -> None:
    """The llm level streams a summary and records token deltas as provenance."""
    progress = _RecordingProgress()
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="llm", llm_backend="ollama"),
        import_module=_importer({"httpx": _fake_httpx()}),
        progress=progress,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.data["summary"] == "Worked on indexing."
    assert enrichment.provenance is not None
    assert enrichment.provenance["backend"] == "ollama"
    assert progress.deltas == ["Worked on ", "indexing."]


def _fake_litert_lm() -> t.Any:
    """Return a fake litert_lm whose conversation yields cumulative chunks."""

    class _Conversation:
        def send_message_async(self, _prompt: str) -> t.Iterator[dict[str, str]]:
            for text in ("Worked on ", "Worked on indexing."):
                yield {"role": "model", "content": text}

    class _Engine:
        def __init__(self, _path: str, **_kwargs: object) -> None:
            pass

        def create_conversation(self) -> _Conversation:
            return _Conversation()

        def close(self) -> None:
            pass

    return types.SimpleNamespace(
        Engine=_Engine,
        Backend=types.SimpleNamespace(CPU=object()),
        LogSeverity=types.SimpleNamespace(ERROR=object()),
        set_min_log_severity=lambda _severity: None,
    )


def test_llm_enricher_litert_streams_from_local_model(tmp_path: pathlib.Path) -> None:
    """The litert-lm backend loads a provisioned model and streams a summary."""
    spec = models_mod.resolve_llm_model("gemma-4-e2b", "litert-lm")
    assert spec is not None
    target = models_mod.model_cache_path(spec, tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "agentgrep-manifest.json").write_text("{}", encoding="utf-8")

    progress = _RecordingProgress()
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="llm", llm_backend="litert-lm", model="gemma-4-e2b"),
        import_module=_importer({"litert_lm": _fake_litert_lm()}),
        progress=progress,
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.backend == "litert-lm"
    assert enrichment.data["summary"] == "Worked on indexing."
    assert progress.deltas == ["Worked on ", "indexing."]


def test_llm_enricher_litert_errors_when_model_not_provisioned(tmp_path: pathlib.Path) -> None:
    """An unprovisioned litert model yields an error enrichment with an install hint."""
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="llm", llm_backend="litert-lm", model="gemma-4-e2b"),
        import_module=_importer({"litert_lm": _fake_litert_lm()}),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "error"
    setup = next(d.setup_command for d in report.diagnostics if d.setup_command)
    assert "models install" in setup


def _fake_transformers() -> tuple[t.Any, t.Any]:
    """Return fake ``(torch, transformers)`` for the transformers LLM backend."""

    class _InputIds:
        shape = (1, 3)  # prompt is 3 tokens

    class _Inputs(dict):
        def to(self, _device: str) -> _Inputs:
            return self

    class _Tokenizer:
        eos_token_id = 0

        @classmethod
        def from_pretrained(cls, _path: str, **_kwargs: object) -> _Tokenizer:
            return cls()

        def apply_chat_template(self, _messages: object, **_kwargs: object) -> _Inputs:
            return _Inputs(input_ids=_InputIds())

        def decode(self, _tokens: object, **_kwargs: object) -> str:
            return "Worked on indexing."

    class _Model:
        @classmethod
        def from_pretrained(cls, _path: str, **_kwargs: object) -> _Model:
            return cls()

        def to(self, _device: str) -> _Model:
            return self

        def eval(self) -> _Model:
            return self

        def generate(self, **_kwargs: object) -> list[list[int]]:
            return [[0, 1, 2, 7, 8]]  # outputs[0][3:] -> the new tokens

    class _InferenceMode:
        def __enter__(self) -> _InferenceMode:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        float16=object(),
        float32=object(),
        inference_mode=_InferenceMode,
    )
    transformers = types.SimpleNamespace(
        AutoTokenizer=_Tokenizer,
        AutoModelForCausalLM=_Model,
        BitsAndBytesConfig=lambda **_kwargs: object(),
    )
    return torch, transformers


def _provision(model_id: str, model_cache: pathlib.Path) -> None:
    """Write an empty manifest so ``models_mod.is_installed`` reports the model."""
    spec = models_mod.resolve_llm_model(model_id, "transformers")
    assert spec is not None
    target = models_mod.model_cache_path(spec, model_cache)
    target.mkdir(parents=True, exist_ok=True)
    (target / "agentgrep-manifest.json").write_text("{}", encoding="utf-8")


def test_llm_enricher_transformers_generates_from_local_model(tmp_path: pathlib.Path) -> None:
    """The transformers backend loads a provisioned model and returns a summary."""
    spec = models_mod.resolve_llm_model("gemma-3-1b-it", "transformers")
    assert spec is not None
    target = models_mod.model_cache_path(spec, tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "agentgrep-manifest.json").write_text("{}", encoding="utf-8")

    torch, transformers = _fake_transformers()
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="llm", llm_backend="transformers", model="gemma-3-1b-it"),
        import_module=_importer({"torch": torch, "transformers": transformers}),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.backend == "transformers"
    assert enrichment.data["summary"] == "Worked on indexing."
    assert enrichment.provenance is not None and enrichment.provenance["endpoint"] == "cpu"


def test_llm_enricher_transformers_default_chain_uses_first_loadable(
    tmp_path: pathlib.Path,
) -> None:
    """With no --model, the default chain skips unprovisioned specs to the first ready one."""
    # Phi (chain head) is left unprovisioned; download is off, so it is skipped.
    _provision("smollm2-1.7b-instruct", tmp_path)

    torch, transformers = _fake_transformers()
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="llm", llm_backend="transformers"),
        import_module=_importer({"torch": torch, "transformers": transformers}),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.backend == "transformers"
    assert enrichment.data["model"] == "smollm2-1.7b-instruct"
    assert enrichment.provenance is not None
    assert enrichment.provenance["quantization"] == "none"


def test_llm_enricher_transformers_falls_back_when_quant_lib_missing(
    tmp_path: pathlib.Path,
) -> None:
    """A 4-bit candidate whose bitsandbytes import fails drops to the next chain spec."""
    # Both provisioned, but bitsandbytes is absent from the importer, so the
    # 4-bit Phi head raises on load and the fp16 SmolLM2 default serves.
    _provision("phi-4-mini-instruct", tmp_path)
    _provision("smollm2-1.7b-instruct", tmp_path)

    torch, transformers = _fake_transformers()
    report = build_report(
        _RECORDS,
        ReportRequest(requested_level="llm", llm_backend="transformers"),
        import_module=_importer({"torch": torch, "transformers": transformers}),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.data["model"] == "smollm2-1.7b-instruct"


def test_llm_enricher_transformers_loads_4bit_when_quant_lib_present(
    tmp_path: pathlib.Path,
) -> None:
    """A pinned 4-bit model loads through the quant path when bitsandbytes is importable."""
    _provision("phi-4-mini-instruct", tmp_path)

    torch, transformers = _fake_transformers()
    report = build_report(
        _RECORDS,
        ReportRequest(
            requested_level="llm",
            llm_backend="transformers",
            model="phi-4-mini-instruct",
        ),
        import_module=_importer(
            {"torch": torch, "transformers": transformers, "bitsandbytes": object()}
        ),
        model_cache=tmp_path,
    )
    enrichment = report.enrichments[0]
    assert enrichment.status == "ok"
    assert enrichment.data["model"] == "phi-4-mini-instruct"
    assert enrichment.provenance is not None
    assert enrichment.provenance["quantization"] == "4bit"
