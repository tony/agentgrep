"""Fast-default and explicit deep-search contracts."""

from __future__ import annotations

import dataclasses
import pathlib
import typing as t

import pytest

from agentgrep import (
    BackendSelection,
    GrepArgs,
    SearchArgs,
    SearchQuery,
    create_parser,
    parse_args,
    run_search_query,
)
from agentgrep.query import build_query_from_input, default_registry


@pytest.mark.parametrize("subcommand", ["search", "grep"])
def test_depth_ladder_is_visible_in_subcommand_help(subcommand: str) -> None:
    """State the fast default before users choose bounded or exhaustive work."""
    bundle = create_parser("never")
    parser = bundle.search_parser if subcommand == "search" else bundle.grep_parser

    help_text = parser.format_help()

    assert "Fast by default: reads prompt-history stores only." in help_text
    assert "--deep" in help_text
    assert "--exhaustive" in help_text
    assert "every readable conversation backend" in help_text


def test_search_args_preserve_legacy_positional_fields() -> None:
    """Keep the former ninth positional argument bound to threshold."""
    args = SearchArgs(
        ("needle",),
        ("codex",),
        "prompts",
        False,
        None,
        "text",
        "auto",
        "auto",
        70,
    )

    assert args.threshold == 70
    assert args.effort == "prompt"


def test_grep_args_preserve_legacy_positional_fields() -> None:
    """Keep the former nineteenth positional argument bound to style."""
    args = GrepArgs(
        ("needle",),
        ("codex",),
        "prompts",
        "smart",
        "regex",
        False,
        False,
        False,
        False,
        False,
        None,
        None,
        None,
        False,
        False,
        "text",
        "auto",
        "auto",
        "pretty",
    )

    assert args.style == "pretty"
    assert args.effort == "prompt"


def test_legacy_broad_search_args_derive_exhaustive_effort() -> None:
    """Normalize omitted search effort without weakening a broad scope."""
    args = SearchArgs(
        ("needle",),
        ("codex",),
        "all",
        False,
        None,
        "text",
        "auto",
        "auto",
    )
    assert args.effort == "exhaustive"


def test_legacy_broad_grep_args_derive_exhaustive_effort() -> None:
    """Normalize omitted grep effort without weakening a broad scope."""
    args = GrepArgs(
        ("needle",),
        ("codex",),
        "all",
        "smart",
        "regex",
        False,
        False,
        False,
        False,
        False,
        None,
        None,
        None,
        False,
        False,
        "text",
        "auto",
        "auto",
    )
    assert args.effort == "exhaustive"


def test_search_args_reject_invalid_runtime_effort() -> None:
    """Do not interpret arbitrary search effort values as exhaustive."""
    parsed = parse_args(["search", "needle"])
    assert isinstance(parsed, SearchArgs)

    with pytest.raises(
        ValueError,
        match="effort must be 'prompt', 'targeted', or 'exhaustive'",
    ):
        dataclasses.replace(parsed, effort=t.cast("t.Any", "invalid"))


def test_grep_args_reject_invalid_runtime_effort() -> None:
    """Do not interpret arbitrary grep effort values as exhaustive."""
    parsed = parse_args(["grep", "needle"])
    assert isinstance(parsed, GrepArgs)

    with pytest.raises(
        ValueError,
        match="effort must be 'prompt', 'targeted', or 'exhaustive'",
    ):
        dataclasses.replace(parsed, effort=t.cast("t.Any", "invalid"))


@pytest.mark.parametrize(
    ("argv", "args_type", "expected_scope", "expected_effort"),
    [
        (["search", "needle"], SearchArgs, "prompts", "prompt"),
        (["search", "--deep", "needle"], SearchArgs, "all", "targeted"),
        (["search", "--exhaustive", "needle"], SearchArgs, "prompts", "exhaustive"),
        (["search", "--scope", "all", "needle"], SearchArgs, "all", "exhaustive"),
        (["search", "scope:prompts needle"], SearchArgs, "prompts", "prompt"),
        (
            ["search", "scope:conversations needle"],
            SearchArgs,
            "conversations",
            "exhaustive",
        ),
        (["search", "scope:all needle"], SearchArgs, "all", "exhaustive"),
        (
            ["search", "(scope:prompts OR model:gpt*) needle"],
            SearchArgs,
            "all",
            "exhaustive",
        ),
        (
            ["search", "NOT scope:conversations needle"],
            SearchArgs,
            "prompts",
            "prompt",
        ),
        (
            ["search", "(scope:conversations OR NOT agent:codex) needle"],
            SearchArgs,
            "all",
            "exhaustive",
        ),
        (
            ["search", "NOT (scope:prompts AND agent:codex) needle"],
            SearchArgs,
            "all",
            "exhaustive",
        ),
        (["grep", "needle"], GrepArgs, "prompts", "prompt"),
        (["grep", "--deep", "needle"], GrepArgs, "all", "targeted"),
        (["grep", "--exhaustive", "needle"], GrepArgs, "prompts", "exhaustive"),
        (["grep", "--scope", "all", "needle"], GrepArgs, "all", "exhaustive"),
        (["grep", "scope:prompts needle"], GrepArgs, "prompts", "prompt"),
        (["grep", "scope:all needle"], GrepArgs, "all", "exhaustive"),
    ],
)
def test_cli_normalizes_scope_and_read_effort(
    argv: list[str],
    args_type: type[SearchArgs | GrepArgs],
    expected_scope: str,
    expected_effort: str,
) -> None:
    """Catch either frontend reopening transcripts in the default path."""
    parsed = parse_args(argv)

    assert isinstance(parsed, args_type)
    assert parsed.scope == expected_scope
    assert parsed.effort == expected_effort


@pytest.mark.parametrize(
    ("argv", "expected_effort"),
    [
        (["search", "scope:all needle"], "prompt"),
        (["search", "--deep", "needle"], "targeted"),
        (["search", "--exhaustive", "scope:prompts needle"], "exhaustive"),
        (["search", "--scope", "all", "needle"], "exhaustive"),
        (["grep", "scope:all needle"], "prompt"),
    ],
)
def test_cli_keeps_launch_effort_separate_from_inline_scope(
    argv: list[str],
    expected_effort: str,
) -> None:
    """Provide stable TUI provenance for explicit versus derived depth."""
    parsed = parse_args(argv)

    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.base_effort == expected_effort


@pytest.mark.parametrize("command", ["search", "grep"])
def test_inline_scope_does_not_make_tui_base_scope_explicit(command: str) -> None:
    """Restore inferred prompt provenance after replacing an inline scope."""
    parsed = parse_args([command, "scope:all needle"])

    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.scope_provenance == "explicit"
    assert parsed.base_scope == "prompts"
    assert parsed.base_scope_provenance == "inferred"


@pytest.mark.parametrize("command", ["search", "grep"])
def test_deep_and_exhaustive_are_mutually_exclusive(command: str) -> None:
    """Keep targeted and exhaustive work as standalone effort selectors."""
    with pytest.raises(SystemExit):
        parse_args([command, "--deep", "--exhaustive", "needle"])


@pytest.mark.parametrize("command", ["search", "grep"])
def test_deep_accepts_a_positive_conversation_limit(command: str) -> None:
    """Expose the work bound independently from the result limit."""
    parsed = parse_args(
        [command, "--deep", "--conversation-limit", "7", "needle"],
    )

    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.scope == "all"
    assert parsed.scope_provenance == "inferred"
    assert parsed.effort == "targeted"
    assert parsed.conversation_limit == 7


@pytest.mark.parametrize("command", ["search", "grep"])
def test_conversation_limit_requires_deep(command: str) -> None:
    """Reject a targeted work bound when no targeted request exists."""
    with pytest.raises(SystemExit):
        parse_args([command, "--conversation-limit", "7", "needle"])


@pytest.mark.parametrize("command", ["search", "grep"])
def test_explicit_prompt_scope_rejects_deep(command: str) -> None:
    """Do not silently broaden an explicitly prompt-only request."""
    with pytest.raises(SystemExit):
        parse_args([command, "--deep", "--scope", "prompts", "needle"])


def test_exhaustive_prompt_search_is_the_only_flag_path_that_reads_transcripts(
    codex_transcript_home: pathlib.Path,
) -> None:
    """Catch prompt scope silently falling back to a transcript backend."""
    backends = BackendSelection(find_tool=None, grep_tool=None, json_tool=None)

    def search(effort: t.Literal["prompt", "exhaustive"]) -> list[str]:
        query = SearchQuery(
            terms=("deep-only",),
            scope="prompts",
            any_term=False,
            regex=False,
            case_sensitive=False,
            agents=("codex",),
            limit=None,
            effort=effort,
        )
        return [
            record.text
            for record in run_search_query(
                codex_transcript_home,
                query,
                backends=backends,
            )
        ]

    assert search("prompt") == []
    assert search("exhaustive") == ["deep-only prompt"]


@pytest.mark.parametrize("text", ["redacted user", "scope:prompts redacted"])
def test_interactive_query_rebuild_preserves_exhaustive_effort(text: str) -> None:
    """Catch a TUI edit silently dropping transcript authorization."""
    base = SearchQuery(
        terms=("initial",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="exhaustive",
    )

    result = build_query_from_input(text, base, default_registry())

    assert result.query is not None
    assert result.query.effort == "exhaustive"


def test_interactive_conversation_scope_enables_exhaustive_effort() -> None:
    """Let a TUI scope edit make the same explicit legacy opt-in as the CLI."""
    base = SearchQuery(
        terms=("initial",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="prompt",
    )

    result = build_query_from_input(
        "scope:conversations deep-only",
        base,
        default_registry(),
    )

    assert result.query is not None
    assert result.query.scope == "conversations"
    assert result.query.effort == "exhaustive"
