"""Render Skill (``SKILL.md``) drafts from graph skill suggestions.

The graph level surfaces *skill suggestions* — recurring multi-step
workflows (``macro``) and recurring varied asks (``template``) that the user
repeats across conversations. This module turns each suggestion into a
drop-in ``SKILL.md`` document.

Naming is deterministic by default (slug from the suggestion, a templated
"Use when …" description). An optional bounded local-LLM pass can name and
describe the skill from its evidence; the LLM runtime is abstracted behind a
``complete`` callable so the naming logic is runtime-agnostic and testable.
The LLM pass degrades to deterministic naming on any error.

Output is print-by-default. Callers decide where (if anywhere) to write
files; skills are never written under ``~/.claude/skills`` automatically.
"""

from __future__ import annotations

import collections.abc as cabc
import json
import re
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.insights.loader import ImportModule

LLMComplete = cabc.Callable[[str], str]
"""A bounded prompt→completion callable (one non-streaming generation)."""

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 48
_LITERT_NAMING_TOKENS = 256


class SkillDraft(t.NamedTuple):
    """A named, described, rendered Skill document."""

    name: str
    description: str
    markdown: str
    source: str  # "deterministic" | "llm"


def slugify(text: str) -> str:
    """Return a kebab-case slug suitable for a Skill ``name``.

    Examples
    --------
    >>> slugify("Commit & Continue (separately)")
    'commit-continue-separately'
    >>> slugify("")
    'recurring-task'
    """
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    while len(slug) > _MAX_SLUG and "-" in slug:
        slug = slug.rsplit("-", 1)[0]
    return slug or "recurring-task"


def _examples(suggestion: cabc.Mapping[str, t.Any]) -> list[str]:
    """Return the concrete steps (macro) or example asks (template)."""
    if suggestion.get("type") == "macro":
        return [str(step) for step in suggestion.get("steps", [])]
    return [str(example) for example in suggestion.get("examples", [])]


def default_name_and_description(suggestion: cabc.Mapping[str, t.Any]) -> tuple[str, str]:
    """Return the deterministic ``(name, description)`` for a suggestion."""
    name = slugify(str(suggestion.get("name") or "recurring-task"))
    support = suggestion.get("support")
    if suggestion.get("type") == "macro":
        chain = " then ".join(step[:40] for step in _examples(suggestion))
        description = (
            f"Use when the user begins a workflow that repeats these steps: "
            f"{chain or 'the recurring chain'}. Runs the whole sequence in one step."
        )
    else:
        terms = ", ".join(str(term) for term in suggestion.get("terms", [])[:4])
        times = f"{support} times" if isinstance(support, int) else "several times"
        description = (
            f"Use when the user makes a request about {terms or 'this recurring topic'}. "
            f"Parameterizes a request they have made {times}."
        )
    return name, description


def render_skill_md(
    suggestion: cabc.Mapping[str, t.Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> str:
    """Render a valid ``SKILL.md`` document from a graph skill suggestion.

    Parameters
    ----------
    suggestion : Mapping
        One entry from the graph enrichment's ``skill_suggestions``.
    name, description : str, optional
        Override the deterministic name/description (e.g. from an LLM pass).
    """
    auto_name, auto_description = default_name_and_description(suggestion)
    final_name = slugify(name) if name else auto_name
    final_description = (description or auto_description).strip().replace("\n", " ")
    title = final_name.replace("-", " ").title()
    examples = _examples(suggestion)
    rationale = str(suggestion.get("rationale") or "Captures a recurring request.")

    lines = [
        "---",
        f"name: {final_name}",
        f"description: {final_description}",
        "---",
        "",
        f"# {title}",
        "",
        rationale,
        "",
    ]
    evidence = str(suggestion.get("evidence", "")).strip()
    if evidence:
        lines.extend([f"_Evidence: {evidence}_", ""])
    if suggestion.get("type") == "macro":
        lines.extend(["## Steps", ""])
        lines.extend(f"{index}. {step}" for index, step in enumerate(examples, start=1))
    else:
        lines.extend(["## Example requests", ""])
        lines.extend(f"- {example}" for example in examples)
    lines.extend(
        [
            "",
            "## Instructions",
            "",
            "Replace this section with the concrete actions to take. This draft was "
            "generated from your prompt history — review and edit before use.",
            "",
        ]
    )
    return "\n".join(lines)


def _naming_prompt(suggestion: cabc.Mapping[str, t.Any]) -> str:
    """Compose the bounded naming prompt grounded in the suggestion evidence."""
    examples = "\n".join(f"- {example}" for example in _examples(suggestion)[:4])
    kind = str(suggestion.get("type", "request"))
    return (
        "You name reusable developer Skills. Given evidence of a recurring "
        "request, reply with ONE JSON object and nothing else:\n"
        '{"name": "kebab-case-slug", "description": "Use when ..."}\n'
        "The description must state WHEN to use the skill so an agent can "
        "auto-trigger it. Keep the name under 6 words.\n\n"
        f"Recurring {kind} evidence:\n"
        f"{str(suggestion.get('evidence', '')).strip()}\n"
        f"{examples}\n"
    )


def _parse_naming(raw: str) -> tuple[str, str] | None:
    """Parse a ``{name, description}`` JSON object from an LLM reply."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError, ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name")
    description = parsed.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    name, description = name.strip(), description.strip()
    if not name or not description:
        return None
    return slugify(name), description


def llm_name_and_description(
    suggestion: cabc.Mapping[str, t.Any],
    complete: LLMComplete,
) -> tuple[str, str] | None:
    """Name + describe a skill via a bounded LLM ``complete`` call.

    Returns ``None`` on any failure (call raised, empty or unparsable reply)
    so the caller can fall back to :func:`default_name_and_description`.
    """
    try:
        raw = complete(_naming_prompt(suggestion))
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    return _parse_naming(raw)


def draft_skill(
    suggestion: cabc.Mapping[str, t.Any],
    *,
    complete: LLMComplete | None = None,
) -> SkillDraft:
    """Build a :class:`SkillDraft`, using the LLM namer when provided."""
    name: str | None = None
    description: str | None = None
    source = "deterministic"
    if complete is not None:
        named = llm_name_and_description(suggestion, complete)
        if named is not None:
            name, description = named
            source = "llm"
    markdown = render_skill_md(suggestion, name=name, description=description)
    final_name, final_description = (
        (name, description) if name and description else default_name_and_description(suggestion)
    )
    return SkillDraft(
        name=slugify(final_name),
        description=final_description,
        markdown=markdown,
        source=source,
    )


def ollama_reachable(
    *,
    endpoint: str,
    import_module: ImportModule,
    timeout: float = 2.0,
) -> bool:
    """Return whether an Ollama daemon answers at ``endpoint`` within ``timeout``.

    A single bounded probe: connecting to a closed port can stall for the full
    socket timeout, so callers probe once up front instead of paying that wait
    on every per-item generation call.
    """
    try:
        httpx = import_module("httpx")
        response = httpx.get(f"{endpoint.rstrip('/')}/api/tags", timeout=timeout)
        response.raise_for_status()
    except Exception:
        return False
    return True


def build_ollama_complete(
    *,
    model: str,
    endpoint: str,
    import_module: ImportModule,
    timeout: float = 60.0,
) -> LLMComplete:
    """Return a non-streaming Ollama ``complete`` callable (``stream=false``)."""
    httpx = import_module("httpx")
    base = endpoint.rstrip("/")

    def _complete(prompt: str) -> str:
        response = httpx.post(
            f"{base}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("response", ""))

    return _complete


def build_litert_complete(
    *,
    model_path: str,
    import_module: ImportModule,
    max_tokens: int = _LITERT_NAMING_TOKENS,
    timeout: float = 45.0,
) -> LLMComplete:
    """Return a bounded in-process LiteRT-LM ``complete`` callable.

    ``max_tokens`` caps the generation length (a short name/summary needs few
    tokens) and ``timeout`` stops streaming once it elapses, so CPU generation
    cannot run away. The caller treats an empty/partial return as a fallback.
    """
    import time

    litert_lm = import_module("litert_lm")

    def _complete(prompt: str) -> str:
        engine = litert_lm.Engine(
            model_path,
            backend=litert_lm.Backend.CPU,
            max_num_tokens=max_tokens,
        )
        try:
            conversation = engine.create_conversation()
            chunks: list[str] = []
            deadline = time.monotonic() + timeout
            for chunk in conversation.send_message_async(prompt):
                content = chunk.get("content") if isinstance(chunk, dict) else None
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    chunks.extend(
                        str(part.get("text", "")) for part in content if isinstance(part, dict)
                    )
                if time.monotonic() > deadline:
                    break
            return "".join(chunks)
        finally:
            engine.close()

    return _complete


def build_transformers_complete(
    *,
    model_path: str,
    import_module: ImportModule,
    max_tokens: int = 64,
    device: str | None = None,
    quantization: str = "none",
    trust_remote_code: bool = False,
) -> LLMComplete:
    """Return an in-process transformers ``complete`` callable on GPU/CPU.

    The model is loaded once here and reused across calls (so a batch of
    summaries pays one load, then fast per-call generation). ``device``
    defaults to CUDA when available, else CPU; ``max_tokens`` bounds the
    generation length (a one-line summary needs few tokens). Loads from the
    already-provisioned local model directory — no network or token needed.

    ``quantization="4bit"`` loads the weights through bitsandbytes NF4 so a
    larger model fits a small GPU; the quantized model is placed on the GPU by
    ``from_pretrained`` (via ``device_map``) and is therefore *not* moved with
    ``.to()``. A missing bitsandbytes library propagates as an exception so a
    caller walking a fallback chain skips this candidate. ``trust_remote_code``
    is required for repos that ship their own modeling code (e.g. Phi-4-mini).
    """
    torch = import_module("torch")
    transformers = import_module("transformers")
    resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")

    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_path, trust_remote_code=trust_remote_code
    )
    if quantization == "4bit":
        # bitsandbytes is a separate, GPU-only dependency; a failed import
        # (absent or built against a different CUDA) propagates to the caller.
        import_module("bitsandbytes")
        quant_config = transformers.BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_quant_type="nf4",
        )
        model = transformers.AutoModelForCausalLM.from_pretrained(
            model_path,
            quantization_config=quant_config,
            device_map="cuda",
            trust_remote_code=trust_remote_code,
        )
    else:
        dtype = torch.float16 if str(resolved_device).startswith("cuda") else torch.float32
        model = transformers.AutoModelForCausalLM.from_pretrained(
            model_path, dtype=dtype, trust_remote_code=trust_remote_code
        )
        model = model.to(resolved_device)
    model.eval()

    def _complete(prompt: str) -> str:
        inputs = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            add_generation_prompt=True,
            tokenize=True,
            return_tensors="pt",
            return_dict=True,
        ).to(resolved_device)
        with torch.inference_mode():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        new_tokens = outputs[0][inputs["input_ids"].shape[-1] :]
        return str(tokenizer.decode(new_tokens, skip_special_tokens=True)).strip()

    return _complete


def first_working_transformers(
    specs: cabc.Sequence[t.Any],
    *,
    load_one: cabc.Callable[[t.Any], LLMComplete | None],
) -> tuple[t.Any, LLMComplete] | None:
    """Return the first ``(spec, complete)`` that loads, else ``None``.

    Walks ``specs`` in order, calling ``load_one`` for each. Any failure — a
    candidate that is unprovisioned, a missing quant library, an out-of-memory
    load, or a ``trust_remote_code`` error — drops that candidate and the walk
    continues to the next. This honors the non-gated transformers fallback
    chain (Phi-4-mini 4-bit → SmolLM2 fp16 → Granite 4-bit) at both the graph
    conversation-summary and the L5 narrative call sites.
    """
    for spec in specs:
        try:
            complete = load_one(spec)
        except Exception:
            continue
        if complete is not None:
            return spec, complete
    return None
