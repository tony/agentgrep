"""Curated model registry and the gemma4/phi4-style artifact downloader.

Listing models is static (a frozen tuple) so it never touches the
network or imports a backend. Provisioning is the only operation that
downloads, and only when explicitly requested.

Two download shapes share one cache layout and one manifest sidecar:

- **Single-artifact urllib fetch** — the gemma4 ``.litertlm`` / phi4
  ``.gguf`` pattern. The torch-free ``model2vec`` embedding model reuses
  this exact path, so a sentence-embedding model is "automatically
  downloaded in the same way we fetch gemma4 and phi4."
- **Snapshot fetch** — multi-file Hugging Face repos (e.g. a
  ``sentence-transformers`` model) land in the same cache via
  ``huggingface_hub.snapshot_download`` when that optional package is
  present.

Every install writes ``agentgrep-manifest.json`` recording backend,
model id, source URL, license, files, and byte size — the provenance the
report and ``models list`` surfaces read back.
"""

from __future__ import annotations

import json
import os
import pathlib
import typing as t
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from agentgrep.insights import cache as cache_mod
from agentgrep.insights.loader import (
    BackendConfigurationError,
    BackendRuntimeError,
)

if t.TYPE_CHECKING:
    from agentgrep.insights.loader import ImportModule
    from agentgrep.insights.progress import InsightsProgress

ModelKind = t.Literal["embeddings", "llm", "reranker"]
EmbeddingRuntime = t.Literal["sentence-transformers", "model2vec"]
LLMBackend = t.Literal["litert-lm", "llama-cpp", "ollama", "transformers"]

_HF_BASE = "https://huggingface.co"
_MANIFEST_NAME = "agentgrep-manifest.json"
_DOWNLOAD_CHUNK = 1 << 16
_MANIFEST_KIND = "agentgrep.insights.model-manifest"


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """A curated embedding model and how to fetch it locally."""

    model_id: str
    runtime: EmbeddingRuntime
    repo_id: str
    license: str
    source_url: str
    local_id: str
    dimensions: int
    notes: str
    revision: str = "main"
    files: tuple[str, ...] = ()

    kind: t.ClassVar[ModelKind] = "embeddings"

    @property
    def backend(self) -> str:
        """Return the runtime label used in cache paths and manifests."""
        return self.runtime


@dataclass(frozen=True, slots=True)
class LLMModelSpec:
    """A curated local-LLM model and how to fetch its artifact."""

    model_id: str
    backend: LLMBackend
    repo_id: str
    artifact_filename: str | None
    license: str
    source_url: str
    local_id: str
    notes: str
    revision: str = "main"
    quantization: t.Literal["none", "4bit"] = "none"
    trust_remote_code: bool = False

    kind: t.ClassVar[ModelKind] = "llm"

    @property
    def files(self) -> tuple[str, ...]:
        """Return the single artifact filename as a one-tuple, if any."""
        return (self.artifact_filename,) if self.artifact_filename else ()


@dataclass(frozen=True, slots=True)
class RerankerModelSpec:
    """A curated cross-encoder reranker and how to fetch it locally.

    A reranker joint-encodes a text pair and scores its relatedness — an
    orthogonal signal to the static embedding geometry, used to split
    over-merged archetype clusters. Fetched as a multi-file Hugging Face
    snapshot, like a ``sentence-transformers`` embedding model.
    """

    model_id: str
    repo_id: str
    license: str
    source_url: str
    local_id: str
    notes: str
    revision: str = "main"
    files: tuple[str, ...] = ()

    backend: t.ClassVar[str] = "sentence-transformers"
    kind: t.ClassVar[ModelKind] = "reranker"


ModelSpec = EmbeddingModelSpec | LLMModelSpec | RerankerModelSpec


_CURATED_EMBEDDINGS: tuple[EmbeddingModelSpec, ...] = (
    # First sentence-transformers spec = the deterministic default when the
    # ``insights-graph-st`` runtime is installed. gte-small leads on short-text
    # clustering/similarity per its size and needs no query prefix (unlike
    # e5/bge), so it suits the symmetric prompt-to-prompt archetype task.
    EmbeddingModelSpec(
        model_id="gte-small",
        runtime="sentence-transformers",
        repo_id="thenlper/gte-small",
        license="MIT",
        source_url="https://huggingface.co/thenlper/gte-small",
        local_id="gte-small",
        dimensions=384,
        notes="Default ST graph embedder; 384-dim, strong short-text similarity, no prefix.",
    ),
    EmbeddingModelSpec(
        model_id="all-MiniLM-L6-v2",
        runtime="sentence-transformers",
        repo_id="sentence-transformers/all-MiniLM-L6-v2",
        license="Apache-2.0",
        source_url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
        local_id="minilm-l6-v2",
        dimensions=384,
        notes="Fast 384-dim general-purpose sentence embedding model (override via --model).",
    ),
    EmbeddingModelSpec(
        model_id="all-mpnet-base-v2",
        runtime="sentence-transformers",
        repo_id="sentence-transformers/all-mpnet-base-v2",
        license="Apache-2.0",
        source_url="https://huggingface.co/sentence-transformers/all-mpnet-base-v2",
        local_id="mpnet-base-v2",
        dimensions=768,
        notes="Highest-quality 768-dim 'all-' model; slower on CPU (override via --model).",
    ),
    EmbeddingModelSpec(
        model_id="potion-base-8M",
        runtime="model2vec",
        repo_id="minishlab/potion-base-8M",
        license="MIT",
        source_url="https://huggingface.co/minishlab/potion-base-8M",
        local_id="potion-base-8m",
        dimensions=256,
        notes="Torch-free static embeddings; downloads via the urllib artifact path.",
        files=("config.json", "model.safetensors", "tokenizer.json"),
    ),
)

_CURATED_LLMS: tuple[LLMModelSpec, ...] = (
    LLMModelSpec(
        model_id="gemma-3-1b-it",
        backend="transformers",
        repo_id="google/gemma-3-1b-it",
        artifact_filename=None,
        license="Gemma",
        source_url="https://huggingface.co/google/gemma-3-1b-it",
        local_id="gemma-3-1b-it",
        notes="Instruction-tuned Gemma 3 1B on GPU via transformers/CUDA "
        "(agentgrep[insights-llm-transformers]); gated repo, needs HF_TOKEN + accepted license.",
    ),
    LLMModelSpec(
        model_id="phi-4-mini-instruct",
        backend="transformers",
        repo_id="microsoft/Phi-4-mini-instruct",
        artifact_filename=None,
        license="MIT",
        source_url="https://huggingface.co/microsoft/Phi-4-mini-instruct",
        local_id="phi-4-mini-instruct",
        quantization="4bit",
        notes="Microsoft Phi-4-mini (3.8B, MIT, non-gated). Loaded via the native "
        "transformers phi3 architecture (no remote code), 4-bit-quantized to fit a "
        "4 GB GPU; needs agentgrep[insights-llm-transformers-quant].",
    ),
    LLMModelSpec(
        model_id="smollm2-1.7b-instruct",
        backend="transformers",
        repo_id="HuggingFaceTB/SmolLM2-1.7B-Instruct",
        artifact_filename=None,
        license="Apache-2.0",
        source_url="https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B-Instruct",
        local_id="smollm2-1.7b-instruct",
        notes="HuggingFace SmolLM2 1.7B (Apache-2.0, non-gated). Runs fp16 with no "
        "quant library — the token-free default that works without bitsandbytes.",
    ),
    LLMModelSpec(
        model_id="granite-3.3-2b-instruct",
        backend="transformers",
        repo_id="ibm-granite/granite-3.3-2b-instruct",
        artifact_filename=None,
        license="Apache-2.0",
        source_url="https://huggingface.co/ibm-granite/granite-3.3-2b-instruct",
        local_id="granite-3.3-2b-instruct",
        quantization="4bit",
        notes="IBM Granite 3.3 2B (Apache-2.0, non-gated). 2.5B params 4-bit-quantized "
        "to fit a 4 GB GPU; needs agentgrep[insights-llm-transformers-quant].",
    ),
    LLMModelSpec(
        model_id="gemma-4-e2b",
        backend="litert-lm",
        repo_id="litert-community/gemma-4-E2B-it-litert-lm",
        artifact_filename="gemma-4-E2B-it.litertlm",
        license="Gemma",
        source_url="https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm",
        local_id="gemma4-e2b",
        notes="LiteRT-LM Gemma 4 E2B; runs in-process via agentgrep[insights-llm-litert].",
    ),
    LLMModelSpec(
        model_id="phi-4-mini-gguf",
        backend="llama-cpp",
        repo_id="microsoft/phi-4-gguf",
        artifact_filename="phi-4-q4.gguf",
        license="MIT",
        source_url="https://huggingface.co/microsoft/phi-4-gguf",
        local_id="phi4-mini",
        notes="llama.cpp Phi-4 GGUF; fetch-only registry parity in this MVP.",
    ),
    LLMModelSpec(
        model_id="llama3.2",
        backend="ollama",
        repo_id="library/llama3.2",
        artifact_filename=None,
        license="Llama-3.2-Community",
        source_url="https://ollama.com/library/llama3.2",
        local_id="llama3.2",
        notes="Managed by the Ollama daemon; provision with `ollama pull llama3.2`.",
    ),
)


# Ordered transformers fallback chain used when no ``--model`` is given. Phi and
# Granite need a 4-bit quant library to fit a 4 GB GPU; SmolLM2 runs fp16 with
# none, so it sits between them as the quant-free safety net.
_DEFAULT_TRANSFORMERS_CHAIN: tuple[str, ...] = (
    "phi-4-mini-instruct",
    "smollm2-1.7b-instruct",
    "granite-3.3-2b-instruct",
)


_CURATED_RERANKERS: tuple[RerankerModelSpec, ...] = (
    RerankerModelSpec(
        model_id="cross-encoder/quora-distilroberta-base",
        repo_id="cross-encoder/quora-distilroberta-base",
        license="Apache-2.0",
        source_url="https://huggingface.co/cross-encoder/quora-distilroberta-base",
        local_id="quora-distilroberta-base",
        notes="Duplicate-question cross-encoder; symmetric 0-1 score, purifies archetype clusters.",
    ),
)


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of an :func:`install_model` call."""

    model_id: str
    path: pathlib.Path
    cached: bool
    bytes_downloaded: int
    files: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = False


def list_embedding_models() -> tuple[EmbeddingModelSpec, ...]:
    """Return the curated embedding model registry."""
    return _CURATED_EMBEDDINGS


def list_llm_models(backend: str | None = None) -> tuple[LLMModelSpec, ...]:
    """Return curated LLM models, optionally filtered by backend."""
    if backend is None:
        return _CURATED_LLMS
    return tuple(spec for spec in _CURATED_LLMS if spec.backend == backend)


def list_models(kind: ModelKind) -> tuple[ModelSpec, ...]:
    """Return curated models for ``kind`` (``embeddings``, ``llm``, ``reranker``)."""
    if kind == "embeddings":
        return _CURATED_EMBEDDINGS
    if kind == "reranker":
        return _CURATED_RERANKERS
    return _CURATED_LLMS


def resolve_reranker_model(model_id: str) -> RerankerModelSpec | None:
    """Return the reranker spec matching ``model_id`` (by id or local id)."""
    for spec in _CURATED_RERANKERS:
        if model_id in (spec.model_id, spec.local_id):
            return spec
    return None


def preferred_reranker_model() -> RerankerModelSpec | None:
    """Return the default curated cross-encoder reranker, if any."""
    return _CURATED_RERANKERS[0] if _CURATED_RERANKERS else None


def resolve_embedding_model(model_id: str) -> EmbeddingModelSpec | None:
    """Return the embedding spec matching ``model_id`` (by id or local id)."""
    for spec in _CURATED_EMBEDDINGS:
        if model_id in (spec.model_id, spec.local_id):
            return spec
    return None


def resolve_llm_model(model_id: str, backend: str | None = None) -> LLMModelSpec | None:
    """Return the LLM spec matching ``model_id`` (and optional backend)."""
    for spec in _CURATED_LLMS:
        if model_id in (spec.model_id, spec.local_id) and (
            backend is None or spec.backend == backend
        ):
            return spec
    return None


def default_transformers_chain() -> tuple[LLMModelSpec, ...]:
    """Return the ordered non-gated transformers fallback specs.

    Resolves :data:`_DEFAULT_TRANSFORMERS_CHAIN` to curated specs in order,
    skipping any id that no longer resolves. The caller tries each in turn and
    keeps the first that loads, so a missing 4-bit quant library simply drops
    the quantized candidates and the fp16 SmolLM2 default serves instead.
    """
    resolved = (
        resolve_llm_model(model_id, "transformers") for model_id in _DEFAULT_TRANSFORMERS_CHAIN
    )
    return tuple(spec for spec in resolved if spec is not None)


def preferred_embedding_model(runtime: EmbeddingRuntime) -> EmbeddingModelSpec | None:
    """Return the first curated embedding model for ``runtime``."""
    for spec in _CURATED_EMBEDDINGS:
        if spec.runtime == runtime:
            return spec
    return None


def model_cache_path(spec: ModelSpec, model_cache: pathlib.Path | None = None) -> pathlib.Path:
    """Return the cache directory for ``spec``'s artifacts."""
    root = model_cache or cache_mod.model_cache_dir()
    return root / spec.kind / spec.backend / spec.local_id


def is_installed(spec: ModelSpec, model_cache: pathlib.Path | None = None) -> bool:
    """Return whether ``spec`` has a complete manifest in the cache."""
    return (model_cache_path(spec, model_cache) / _MANIFEST_NAME).is_file()


def _hf_resolve_url(repo_id: str, revision: str, filename: str) -> str:
    """Return the Hugging Face ``resolve`` URL for one repo file."""
    return f"{_HF_BASE}/{repo_id}/resolve/{revision}/{filename}"


def _download_file(
    url: str,
    dest: pathlib.Path,
    *,
    model_id: str,
    token: str | None,
    progress: InsightsProgress | None,
) -> int:
    """Download ``url`` to ``dest`` atomically; return bytes written.

    The download lands in ``dest.with_suffix('.tmp')`` and is renamed on
    success so an interrupted fetch never leaves a half-written artifact
    that looks complete.
    """
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    tmp = dest.with_name(dest.name + ".tmp")
    written = 0
    try:
        with urllib.request.urlopen(request) as response:
            total_header = response.headers.get("Content-Length")
            total = int(total_header) if total_header and total_header.isdigit() else None
            with tmp.open("wb") as handle:
                while chunk := response.read(_DOWNLOAD_CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress.download_progress(
                            model=model_id,
                            downloaded_bytes=written,
                            total_bytes=total,
                        )
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        if exc.code in (401, 403):
            gated = (
                f"access to {model_id!r} is gated ({exc.code}); set HF_TOKEN and "
                f"accept the model terms before downloading"
            )
            raise BackendConfigurationError(gated, level="models") from exc
        http_failure = f"download of {model_id!r} failed: HTTP {exc.code}"
        raise BackendRuntimeError(http_failure, level="models") from exc
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        url_failure = f"download of {model_id!r} failed: {exc.reason}"
        raise BackendRuntimeError(url_failure, level="models") from exc
    tmp.replace(dest)
    return written


def _write_manifest(
    target_dir: pathlib.Path,
    spec: ModelSpec,
    files: tuple[str, ...],
    bytes_downloaded: int,
) -> None:
    """Write the provenance manifest sidecar for an installed model."""
    manifest = {
        "artifact_kind": _MANIFEST_KIND,
        "kind": spec.kind,
        "backend": spec.backend,
        "model_id": spec.model_id,
        "local_id": spec.local_id,
        "repo_id": spec.repo_id,
        "revision": spec.revision,
        "license": spec.license,
        "source_url": spec.source_url,
        "files": list(files),
        "bytes": bytes_downloaded,
        "path": str(target_dir),
    }
    (target_dir / _MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _snapshot_files(
    spec: EmbeddingModelSpec | RerankerModelSpec | LLMModelSpec,
    target_dir: pathlib.Path,
    *,
    import_module: ImportModule | None,
) -> tuple[tuple[str, ...], int]:
    """Fetch a multi-file HF repo via ``huggingface_hub.snapshot_download``."""
    from agentgrep.insights.loader import load_modules

    modules = load_modules(
        ("huggingface_hub",),
        level="embeddings",
        setup_command="uv pip install 'agentgrep[insights-embeddings]'",
        import_module=import_module,
    )
    snapshot_download = modules["huggingface_hub"].snapshot_download
    token = os.environ.get("HF_TOKEN")
    snapshot_download(
        repo_id=spec.repo_id,
        revision=spec.revision,
        local_dir=str(target_dir),
        token=token,
    )
    files = tuple(
        sorted(
            child.name
            for child in target_dir.iterdir()
            if child.is_file() and child.name != _MANIFEST_NAME
        )
    )
    total = cache_mod.directory_size_bytes(target_dir)
    return files, total


def install_model(
    spec: ModelSpec,
    *,
    model_cache: pathlib.Path | None = None,
    progress: InsightsProgress | None = None,
    dry_run: bool = False,
    import_module: ImportModule | None = None,
) -> InstallResult:
    """Provision ``spec`` into the model cache, returning what happened.

    Cached models are a no-op. Ollama-managed models are not downloaded
    here (the daemon owns that cache); calling this for one raises
    :class:`BackendConfigurationError` with the ``ollama pull`` command.
    """
    if isinstance(spec, LLMModelSpec) and spec.backend == "ollama":
        ollama_managed = (
            f"{spec.model_id!r} is managed by Ollama; run `ollama pull {spec.model_id}`"
        )
        raise BackendConfigurationError(
            ollama_managed,
            level="llm",
        )

    target_dir = model_cache_path(spec, model_cache)
    if is_installed(spec, model_cache):
        existing = tuple(
            sorted(
                child.name
                for child in target_dir.iterdir()
                if child.is_file() and child.name != _MANIFEST_NAME
            )
        )
        return InstallResult(
            model_id=spec.model_id,
            path=target_dir,
            cached=True,
            bytes_downloaded=0,
            files=existing,
        )

    planned_files = spec.files
    if dry_run:
        return InstallResult(
            model_id=spec.model_id,
            path=target_dir,
            cached=False,
            bytes_downloaded=0,
            files=planned_files,
            dry_run=True,
        )

    if progress is not None:
        progress.phase("provision model", detail=spec.model_id)
    cache_mod.ensure_dir(target_dir)
    token = os.environ.get("HF_TOKEN")

    # Multi-file snapshot for embedding/reranker models and transformers LLMs
    # (a model dir of config.json + safetensors + tokenizer); single-file
    # artifacts (litert .litertlm, llama.cpp .gguf) take the urllib path below.
    needs_snapshot = not spec.files and (
        isinstance(spec, EmbeddingModelSpec | RerankerModelSpec)
        or (isinstance(spec, LLMModelSpec) and spec.backend == "transformers")
    )
    if needs_snapshot:
        files, total = _snapshot_files(spec, target_dir, import_module=import_module)
    else:
        total = 0
        for filename in spec.files:
            url = _hf_resolve_url(spec.repo_id, spec.revision, filename)
            total += _download_file(
                url,
                target_dir / filename,
                model_id=spec.model_id,
                token=token,
                progress=progress,
            )
        files = spec.files

    _write_manifest(target_dir, spec, files, total)
    return InstallResult(
        model_id=spec.model_id,
        path=target_dir,
        cached=False,
        bytes_downloaded=total,
        files=files,
    )
