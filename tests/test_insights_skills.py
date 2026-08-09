"""Tests for SKILL.md drafting from graph skill suggestions."""

from __future__ import annotations

import typing as t

from agentgrep.insights import skills as skills_mod

_TEMPLATE_SUGGESTION: dict[str, t.Any] = {
    "type": "template",
    "name": "vcspull-commit",
    "evidence": "3 similar asks across 2 conversations",
    "rationale": "A parameterized skill for this recurring request.",
    "support": 3,
    "terms": ["vcspull", "commit", "yaml"],
    "examples": [
        "read .vcspull.yaml changes and commit",
        "commit the .vcspull.yaml updates",
    ],
}

_MACRO_SUGGESTION: dict[str, t.Any] = {
    "type": "macro",
    "name": "pr-pr-then-code-review",
    "evidence": "recurred 2x: /pr:pr → /code-review",
    "rationale": "A skill that runs this sequence in one step.",
    "support": 2,
    "steps": ["/pr:pr", "/code-review"],
}


def test_slugify_kebab_cases_and_defaults() -> None:
    """Slugify lowercases, hyphenates, and defaults empties."""
    assert skills_mod.slugify("Commit & Continue (separately)") == "commit-continue-separately"
    assert skills_mod.slugify("   ") == "recurring-task"


def test_render_template_skill_has_valid_frontmatter_and_examples() -> None:
    """A template suggestion renders valid SKILL.md with name/description + examples."""
    md = skills_mod.render_skill_md(_TEMPLATE_SUGGESTION)
    assert md.startswith("---\n")
    assert "name: vcspull-commit" in md
    assert "description: Use when the user makes a request about vcspull, commit, yaml." in md
    assert "## Example requests" in md
    assert "- read .vcspull.yaml changes and commit" in md
    # Frontmatter description must be a single line.
    description_line = next(line for line in md.splitlines() if line.startswith("description:"))
    assert "\n" not in description_line


def test_render_macro_skill_lists_numbered_steps() -> None:
    """A macro suggestion renders the chain as numbered steps."""
    md = skills_mod.render_skill_md(_MACRO_SUGGESTION)
    assert "## Steps" in md
    assert "1. /pr:pr" in md
    assert "2. /code-review" in md


def test_draft_skill_deterministic_without_llm() -> None:
    """Without a ``complete`` callable the draft is deterministic."""
    draft = skills_mod.draft_skill(_TEMPLATE_SUGGESTION)
    assert draft.source == "deterministic"
    assert draft.name == "vcspull-commit"


def test_draft_skill_uses_llm_name_when_parseable() -> None:
    """A valid LLM JSON reply overrides the name and description."""

    def _complete(_prompt: str) -> str:
        return (
            'Sure: {"name": "Sync And Commit Repos", '
            '"description": "Use when committing vcspull edits."}'
        )

    draft = skills_mod.draft_skill(_TEMPLATE_SUGGESTION, complete=_complete)
    assert draft.source == "llm"
    assert draft.name == "sync-and-commit-repos"
    assert draft.description == "Use when committing vcspull edits."
    assert "name: sync-and-commit-repos" in draft.markdown


def test_draft_skill_falls_back_when_llm_unparseable() -> None:
    """A non-JSON or empty LLM reply falls back to deterministic naming."""

    def _bad(_prompt: str) -> str:
        return "I cannot help with that."

    draft = skills_mod.draft_skill(_TEMPLATE_SUGGESTION, complete=_bad)
    assert draft.source == "deterministic"
    assert draft.name == "vcspull-commit"


def test_draft_skill_falls_back_when_llm_raises() -> None:
    """An LLM backend error degrades to deterministic naming, not a crash."""

    def _raise(_prompt: str) -> str:
        message = "backend unreachable"
        raise RuntimeError(message)

    draft = skills_mod.draft_skill(_MACRO_SUGGESTION, complete=_raise)
    assert draft.source == "deterministic"
    assert draft.name == "pr-pr-then-code-review"


def test_ollama_reachable_probe() -> None:
    """The probe is True only when the daemon answers without raising."""
    import types

    def _ok_get(_url: str, timeout: float = 2.0) -> t.Any:
        return types.SimpleNamespace(raise_for_status=lambda: None)

    def _down_get(_url: str, timeout: float = 2.0) -> t.Any:
        message = "connection refused"
        raise ConnectionError(message)

    up = types.SimpleNamespace(get=_ok_get)
    down = types.SimpleNamespace(get=_down_get)
    assert skills_mod.ollama_reachable(endpoint="http://x", import_module=lambda _n: up) is True
    assert skills_mod.ollama_reachable(endpoint="http://x", import_module=lambda _n: down) is False


def test_build_litert_complete_accumulates_chunks() -> None:
    """build_litert_complete concatenates the conversation's text chunks."""
    import types

    class _Conversation:
        def send_message_async(self, _prompt: str) -> t.Any:
            yield {"role": "model", "content": [{"type": "text", "text": "fish "}]}
            yield {"role": "model", "content": "completions"}

    class _Engine:
        def __init__(self, *_a: t.Any, **_k: t.Any) -> None:
            pass

        def create_conversation(self) -> _Conversation:
            return _Conversation()

        def close(self) -> None:
            pass

    fake = types.SimpleNamespace(Engine=_Engine, Backend=types.SimpleNamespace(CPU="cpu"))
    complete = skills_mod.build_litert_complete(
        model_path="/x", import_module=lambda _n: fake, max_tokens=64
    )
    assert complete("summarize") == "fish completions"
