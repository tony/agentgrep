"""Level 5 enricher: local-LLM narrative summary (Ollama over local HTTP).

The summary is grounded in compact facts — counts, top terms, timeline,
and open-thread titles — never raw transcripts unless ``--include-text``
is set. Tokens stream to the progress sink as they arrive so the CLI can
render the summary live. LiteRT-LM and llama.cpp remain fetch-only in
this MVP; requesting them raises a clear configuration error.
"""

from __future__ import annotations

import json
import os
import typing as t

from agentgrep.insights.loader import BackendConfigurationError, BackendRuntimeError
from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    from agentgrep.insights.enrichers import EnricherContext
    from agentgrep.insights.model import InsightsReport

_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
_DEFAULT_MODEL = "llama3.2"
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
    """Stream a grounded summary from a local Ollama model."""
    backend = ctx.request.llm_backend
    if backend not in ("ollama", "auto"):
        message = f"local LLM backend {backend!r} is fetch-only in this build; use --backend ollama"
        raise BackendConfigurationError(message, level="llm")

    httpx = ctx.modules["httpx"]
    model = ctx.request.model or _DEFAULT_MODEL
    endpoint = _endpoint()
    prompt = _build_prompt(ctx.report, include_text=ctx.request.include_text)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }

    if ctx.progress is not None:
        ctx.progress.phase("summarize", detail=f"ollama:{model}")

    summary_parts: list[str] = []
    try:
        with httpx.stream(
            "POST",
            f"{endpoint}/api/chat",
            json=payload,
            timeout=120.0,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                content = event.get("message", {}).get("content", "")
                if content:
                    summary_parts.append(content)
                    if ctx.progress is not None:
                        ctx.progress.llm_chunk(
                            backend="ollama",
                            model=model,
                            delta=content,
                            char_count=sum(len(part) for part in summary_parts),
                        )
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

    summary = "".join(summary_parts).strip()
    return InsightsEnrichment(
        level="llm",
        backend="ollama",
        status="ok",
        message=f"summarized via ollama:{model}",
        data={"summary": summary, "model": model, "endpoint": endpoint},
        provenance={"backend": "ollama", "model": model, "endpoint": endpoint},
    )
