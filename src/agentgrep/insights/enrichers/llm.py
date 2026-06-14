"""Level 5 enricher: local-LLM narrative summary.

Three runtimes are wired: Ollama over local HTTP, LiteRT-LM in-process
(a Gemma ``.litertlm`` artifact), and transformers in-process (a small,
non-gated instruction-tuned model on GPU/CUDA via ``AutoModelForCausalLM``
— the GPU-capable path where LiteRT's OpenCL delegate is unavailable). The
summary is grounded in compact facts — counts, top terms, timeline, and
open-thread titles — never raw transcripts unless ``--include-text`` is
set. Tokens stream to the progress sink as they arrive so the CLI can
render the summary live. llama.cpp remains fetch-only.
"""

from __future__ import annotations

import contextlib
import json
import os
import typing as t

from agentgrep.insights.loader import BackendConfigurationError, BackendRuntimeError
from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    from agentgrep.insights.enrichers import EnricherContext
    from agentgrep.insights.model import InsightsReport

_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
_DEFAULT_OLLAMA_MODEL = "llama3.2"
_DEFAULT_LITERT_MODEL = "gemma-4-e2b"
# A grounded 3-5 sentence summary; bounded so CPU generation can't run away
# (the LiteRT GPU delegate needs OpenCL/WebGPU, absent on most CPU-only hosts).
_LITERT_MAX_TOKENS = 512
_TRANSFORMERS_MAX_TOKENS = 256
_MAX_FACT_TERMS = 12
_MAX_FACT_THREADS = 8


def _endpoint() -> str:
    """Return the Ollama base URL (overridable via ``AGENTGREP_OLLAMA_URL``)."""
    return os.environ.get("AGENTGREP_OLLAMA_URL", _DEFAULT_ENDPOINT).rstrip("/")


def _build_prompt(report: InsightsReport, *, include_text: bool) -> str:
    """Compose the grounded, compact-facts prompt for the LLM."""
    lines = [
        "You are summarizing a developer's local AI-assistant history.",
        "Write 3-5 sentences describing what they worked on and what is unresolved.",
        "Ground every claim in the facts below; do not invent specifics.",
        "",
        f"Scope: {report.scope}",
        f"Records analyzed: {report.records_analyzed}",
        f"Agents: {', '.join(f'{k} ({v})' for k, v in report.agents.items()) or 'none'}",
        f"Date range: {report.earliest_timestamp or '?'} to {report.latest_timestamp or '?'}",
        "",
        "Top terms: " + ", ".join(term.term for term in report.top_terms[:_MAX_FACT_TERMS]),
    ]
    if report.activity.timeline:
        busy = max(report.activity.timeline, key=lambda bucket: bucket.record_count)
        lines.append(f"Busiest day: {busy.date} ({busy.record_count} records)")
    if report.activity.open_threads:
        lines.append("")
        lines.append("Open threads:")
        for thread in report.activity.open_threads[:_MAX_FACT_THREADS]:
            detail = thread.title if include_text else thread.title[:80]
            lines.append(f"- {detail}")
    return "\n".join(lines)


def build_llm(ctx: EnricherContext) -> InsightsEnrichment:
    """Stream a grounded summary from the selected local-LLM runtime."""
    prompt = _build_prompt(ctx.report, include_text=ctx.request.include_text)
    if ctx.backend == "litert-lm":
        return _run_litert(ctx, prompt)
    if ctx.backend == "ollama":
        return _run_ollama(ctx, prompt)
    if ctx.backend == "transformers":
        return _run_transformers(ctx, prompt)
    message = f"local LLM backend {ctx.backend!r} is fetch-only in this build"
    raise BackendConfigurationError(message, level="llm")


def _emit_delta(ctx: EnricherContext, backend: str, model: str, accumulated: str, text: str) -> str:
    """Emit a streamed delta to the progress sink; return new accumulated text.

    Handles runtimes that yield either cumulative or incremental chunks by
    treating a chunk that extends the accumulated text as cumulative.
    """
    if not text:
        return accumulated
    if text.startswith(accumulated):
        delta = text[len(accumulated) :]
        new_accumulated = text
    else:
        delta = text
        new_accumulated = accumulated + text
    if delta and ctx.progress is not None:
        ctx.progress.llm_chunk(
            backend=backend,
            model=model,
            delta=delta,
            char_count=len(new_accumulated),
        )
    return new_accumulated


def _run_ollama(ctx: EnricherContext, prompt: str) -> InsightsEnrichment:
    """Stream a summary from a local Ollama model over HTTP."""
    httpx = ctx.modules["httpx"]
    model = ctx.request.model or _DEFAULT_OLLAMA_MODEL
    endpoint = _endpoint()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    if ctx.progress is not None:
        ctx.progress.phase("summarize", detail=f"ollama:{model}")

    accumulated = ""
    try:
        with httpx.stream("POST", f"{endpoint}/api/chat", json=payload, timeout=120.0) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                content = event.get("message", {}).get("content", "")
                accumulated = _emit_delta(ctx, "ollama", model, accumulated, accumulated + content)
                if event.get("done"):
                    break
    except Exception as exc:
        name = type(exc).__name__
        if "Connect" in name or "Timeout" in name:
            unreachable = (
                f"Ollama is not reachable at {endpoint}; start it with `ollama serve` "
                f"and `ollama pull {model}`"
            )
            raise BackendConfigurationError(unreachable, level="llm") from exc
        failed = f"Ollama summary failed: {exc}"
        raise BackendRuntimeError(failed, level="llm") from exc

    return _summary_enrichment(
        accumulated.strip(), backend="ollama", model=model, endpoint=endpoint
    )


def _run_litert(ctx: EnricherContext, prompt: str) -> InsightsEnrichment:
    """Stream a summary from an in-process LiteRT-LM model artifact."""
    from agentgrep.insights import models as models_mod

    litert_lm = ctx.modules["litert_lm"]
    model_id = ctx.request.model or _DEFAULT_LITERT_MODEL
    spec = models_mod.resolve_llm_model(model_id, "litert-lm")
    if spec is None or spec.artifact_filename is None:
        message = f"no curated LiteRT-LM model {model_id!r}"
        raise BackendConfigurationError(message, level="llm")

    if not models_mod.is_installed(spec, ctx.model_cache):
        if not ctx.policy.allow_download:
            message = f"LiteRT-LM model {spec.model_id!r} is not provisioned"
            install = (
                f"agentgrep insights models install {spec.model_id} "
                f"--level llm --backend litert-lm --yes"
            )
            raise BackendConfigurationError(message, level="llm", setup_command=install)
        models_mod.install_model(
            spec,
            model_cache=ctx.model_cache,
            progress=ctx.progress,
            import_module=ctx.import_module,
        )

    model_path = models_mod.model_cache_path(spec, ctx.model_cache) / spec.artifact_filename
    if ctx.progress is not None:
        ctx.progress.phase("summarize", detail=f"litert-lm:{spec.model_id}")

    # The LiteRT-LM C++ runtime logs model metadata to stderr at INFO; quiet it
    # so the streamed summary is the only thing the user sees.
    with contextlib.suppress(Exception):
        litert_lm.set_min_log_severity(litert_lm.LogSeverity.ERROR)

    accumulated = ""
    try:
        engine = litert_lm.Engine(
            str(model_path),
            backend=litert_lm.Backend.CPU,
            max_num_tokens=_LITERT_MAX_TOKENS,
        )
        try:
            conversation = engine.create_conversation()
            for chunk in conversation.send_message_async(prompt):
                accumulated = _emit_delta(
                    ctx, "litert-lm", spec.model_id, accumulated, _litert_chunk_text(chunk)
                )
        finally:
            engine.close()
    except Exception as exc:
        failed = f"LiteRT-LM summary failed: {exc}"
        raise BackendRuntimeError(failed, level="llm") from exc

    return _summary_enrichment(
        accumulated.strip(), backend="litert-lm", model=spec.model_id, endpoint=str(model_path)
    )


def _litert_chunk_text(chunk: t.Any) -> str:
    """Extract response text from a LiteRT-LM conversation chunk."""
    content = chunk.get("content") if isinstance(chunk, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _run_transformers(ctx: EnricherContext, prompt: str) -> InsightsEnrichment:
    """Generate a grounded summary from a transformers model on GPU/CPU.

    With no ``--model`` the non-gated default chain is walked (Phi-4-mini 4-bit
    → SmolLM2 fp16 → Granite 4-bit) and the first model that provisions and
    loads serves; an explicit ``--model`` pins one curated spec. The shared
    :func:`~agentgrep.insights.skills.build_transformers_complete` loader formats
    the prompt with the chat template and generates a bounded summary on CUDA
    when available. The resolved device and quantization are recorded in the
    enrichment provenance.
    """
    from agentgrep.insights import models as models_mod, skills as skills_mod

    torch = ctx.modules["torch"]
    importer = ctx.import_module or __import__("importlib").import_module
    requested = ctx.request.model
    if requested:
        pinned = models_mod.resolve_llm_model(requested, "transformers")
        if pinned is None:
            message = f"no curated transformers model {requested!r}"
            raise BackendConfigurationError(message, level="llm")
        candidates: tuple[t.Any, ...] = (pinned,)
    else:
        candidates = models_mod.default_transformers_chain()

    device = "cuda" if torch.cuda.is_available() else "cpu"

    def _load_one(spec: t.Any) -> t.Any:
        if not models_mod.is_installed(spec, ctx.model_cache):
            if not ctx.policy.allow_download:
                return None
            models_mod.install_model(
                spec,
                model_cache=ctx.model_cache,
                progress=ctx.progress,
                import_module=importer,
            )
        if ctx.progress is not None:
            ctx.progress.phase("summarize", detail=f"transformers:{spec.model_id}@{device}")
        return skills_mod.build_transformers_complete(
            model_path=str(models_mod.model_cache_path(spec, ctx.model_cache)),
            import_module=importer,
            max_tokens=_TRANSFORMERS_MAX_TOKENS,
            quantization=spec.quantization,
            trust_remote_code=spec.trust_remote_code,
        )

    chosen = skills_mod.first_working_transformers(candidates, load_one=_load_one)
    if chosen is None:
        tried = ", ".join(spec.model_id for spec in candidates) or "(none)"
        message = f"no transformers model could be loaded (tried: {tried})"
        install = (
            "uv pip install 'agentgrep[insights-llm-transformers,insights-llm-transformers-quant]'"
        )
        raise BackendConfigurationError(message, level="llm", setup_command=install)

    spec, complete = chosen
    text = complete(prompt)
    accumulated = _emit_delta(ctx, "transformers", spec.model_id, "", text)
    return _summary_enrichment(
        accumulated.strip(),
        backend="transformers",
        model=spec.model_id,
        endpoint=device,
        extra={"quantization": spec.quantization},
    )


def _summary_enrichment(
    summary: str,
    *,
    backend: str,
    model: str,
    endpoint: str,
    extra: dict[str, t.Any] | None = None,
) -> InsightsEnrichment:
    """Build the enrichment payload for a generated summary.

    ``extra`` merges backend-specific provenance (e.g. the transformers device
    and quantization mode) without changing the stable ``backend``/``model``/
    ``endpoint`` keys other runtimes share.
    """
    provenance: dict[str, t.Any] = {"backend": backend, "model": model, "endpoint": endpoint}
    if extra:
        provenance.update(extra)
    return InsightsEnrichment(
        level="llm",
        backend=backend,
        status="ok",
        message=f"summarized via {backend}:{model}",
        data={"summary": summary, "model": model, "endpoint": endpoint},
        provenance=provenance,
    )
