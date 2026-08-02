"""``depth:``/``effort:`` field contract.

Today four independent, non-composable mechanisms set the engine's read
policy (:data:`~agentgrep.records.SearchEffort`): CLI ``--deep``/
``--exhaustive`` flags, the MCP ``search`` tool's structured ``effort=``
parameter, the TUI's ``/deep``/``/exhaustive`` slash commands (which escalate
an already-running query rather than letting a user type effort upfront),
and the engine-authored depth-offer panel. None of them let a user type the
read policy as literal query text, composable with other predicates
(``depth:exhaustive scope:all model:gpt* foo``), in one line, working
identically whether it came from a CLI positional, an MCP ``terms`` entry,
or the TUI search box.

This module proves the new ``depth`` field closes that gap: registered in
:func:`agentgrep.query.registry.default_registry` with
``layer="request"`` (a third :data:`~agentgrep.query.registry.FieldLayer`
alongside ``source``/``record`` for a directive that filters no source or
record), canonical name ``depth``, alias ``effort``, and enum values
``prompt``/``targeted``/``deep``/``exhaustive`` (``deep`` is a friendly
synonym for ``targeted``, mirroring ``--deep``/``/deep``). The shared
extraction/resolution logic lives in
:func:`agentgrep.query.compile.resolve_request_modifiers`, which both the
CLI (:mod:`agentgrep.cli.parser`) and the TUI
(:func:`agentgrep.query.build_query_from_input`) now route through instead
of each keeping its own copy of the scope-widening-implies-deeper-reads
ladder. See ``tests/test_search_depth.py`` for the pre-existing CLI
flag/scope-derived-effort contract (unchanged by this module) and
``tests/test_mcp_query_depth_field.py`` for the MCP ``terms=`` proof of this
same field.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import typing as t

import pytest

from agentgrep import GrepArgs, SearchArgs, parse_args
from agentgrep.query import (
    FieldRegistry,
    FieldSpec,
    QueryCompileError,
    build_query_from_input,
    compile_query,
    compose_query_ast,
    default_registry,
    parse_query,
    resolve_request_modifiers,
)
from agentgrep.records import SearchQuery, SearchRecord, SourceHandle


def _base_query() -> SearchQuery:
    """Build one minimal, prompt-scope, prompt-effort base query."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="prompt",
    )


def test_depth_field_is_registered() -> None:
    """The field this whole module proves exists, with the right shape."""
    registry = default_registry()
    spec = registry.get("depth")

    assert "depth" in registry.known_names()
    assert spec is not None
    assert spec.layer == "request"
    assert spec.kind == "enum"
    assert spec.enum_values == ("prompt", "targeted", "deep", "exhaustive")
    assert registry.get("effort") is spec


# ---------------------------------------------------------------------------
# The literal proven-check contract: SearchQuery.effort reflects depth:/
# effort:, with no other flags, through the TUI-facing entry point that
# returns an actual SearchQuery.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query_text", "expected_effort", "expected_scope"),
    [
        # "targeted" widens the base query's implicit prompts scope on its
        # own, mirroring --deep alone (no --scope) at the CLI.
        ("depth:targeted foo", "targeted", "all"),
        ("effort:targeted foo", "targeted", "all"),
        ("depth:deep foo", "targeted", "all"),
        # "exhaustive" does not auto-widen (ADR 0020: a bare --exhaustive
        # retains prompt scope).
        ("depth:exhaustive foo", "exhaustive", "prompts"),
        ("foo", "prompt", "prompts"),
    ],
)
def test_depth_field_resolves_search_query_effort(
    query_text: str,
    expected_effort: str,
    expected_scope: str,
) -> None:
    """``build_query_from_input`` resolves ``SearchQuery.effort``, no frontend involved."""
    result = build_query_from_input(query_text, _base_query(), default_registry())

    assert result.error is None
    assert result.query is not None
    assert result.query.effort == expected_effort
    assert result.query.scope == expected_scope


def test_conversation_limit_resets_when_effort_leaves_targeted() -> None:
    """A stale targeted-only ``conversation_limit`` has nothing to bound once effort moves on.

    A base query launched via ``/deep 50`` (or an earlier ``depth:targeted``
    edit) carries a real ``conversation_limit``. Typing ``depth:exhaustive``
    over that text must not silently carry the old bound into a query the
    engine would otherwise reject (``conversation_limit requires targeted
    effort``).
    """
    base = dataclasses.replace(_base_query(), scope="all", effort="targeted", conversation_limit=50)

    result = build_query_from_input("depth:exhaustive foo", base, default_registry())

    assert result.error is None
    assert result.query is not None
    assert result.query.effort == "exhaustive"
    assert result.query.conversation_limit is None


def test_conversation_limit_survives_a_repeated_targeted_directive() -> None:
    """A ``conversation_limit`` is only reset when effort actually leaves targeted."""
    base = dataclasses.replace(_base_query(), scope="all", effort="targeted", conversation_limit=50)

    result = build_query_from_input("depth:targeted foo", base, default_registry())

    assert result.error is None
    assert result.query is not None
    assert result.query.effort == "targeted"
    assert result.query.conversation_limit == 50


@pytest.mark.parametrize(
    ("query_text", "expected_effort", "expected_scope"),
    [
        # "targeted" widens an implicit prompts default on its own, mirroring
        # --deep alone (no --scope) — see resolve_request_modifiers' docstring.
        ("depth:targeted foo", "targeted", "all"),
        ("effort:targeted foo", "targeted", "all"),
        ("depth:deep foo", "targeted", "all"),
        # "exhaustive" does not auto-widen, matching a bare --exhaustive's
        # existing prompts-scope-retaining behavior (ADR 0020).
        ("depth:exhaustive foo", "exhaustive", "prompts"),
        ("foo", "prompt", "prompts"),
    ],
)
def test_resolve_request_modifiers_reads_the_inline_directive(
    query_text: str,
    expected_effort: str,
    expected_scope: str,
) -> None:
    """The shared resolver reads the inline directive directly, independent of any frontend."""
    registry = default_registry()
    ast = parse_query(query_text, registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
    )

    assert scope == expected_scope
    assert effort == expected_effort


def test_resolve_request_modifiers_respects_explicit_prompts_scope() -> None:
    """An explicitly-pinned prompts scope blocks the targeted auto-widen.

    ``base_scope_explicit=True`` mirrors an explicit ``--scope prompts``/
    ``scope="prompts"`` selection — the caller stated prompts scope on
    purpose, so ``depth:targeted`` must surface as a contradiction for the
    caller to handle (e.g. the CLI's ``targeted effort requires conversation
    or all scope`` error) instead of being silently overridden.
    """
    registry = default_registry()
    ast = parse_query("depth:targeted foo", registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
        base_scope_explicit=True,
    )

    assert (scope, effort) == ("prompts", "targeted")


def test_resolve_request_modifiers_respects_inline_explicit_prompts_scope() -> None:
    """An inline ``scope:prompts`` predicate blocks the targeted auto-widen too."""
    registry = default_registry()
    ast = parse_query("scope:prompts depth:targeted foo", registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
    )

    assert (scope, effort) == ("prompts", "targeted")


def test_resolve_request_modifiers_narrows_an_implicit_broad_scope_for_prompt() -> None:
    """``depth:prompt`` narrows an implicit broad ``base_scope`` back to prompts.

    Symmetric with the ``targeted`` auto-widen: a scope the caller never
    stated on purpose (``base_scope_explicit=False``, the default) is free
    to reconcile with a ``prompt`` directive rather than leave a
    ``(scope="all", effort="prompt")`` combination for a downstream caller
    to reject.
    """
    registry = default_registry()
    ast = parse_query("depth:prompt foo", registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="all",
        base_effort="targeted",
    )

    assert (scope, effort) == ("prompts", "prompt")


def test_resolve_request_modifiers_respects_explicit_broad_scope_for_prompt() -> None:
    """An explicitly-selected broad ``base_scope`` blocks the prompt auto-narrow."""
    registry = default_registry()
    ast = parse_query("depth:prompt foo", registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="all",
        base_effort="targeted",
        base_scope_explicit=True,
    )

    assert (scope, effort) == ("all", "prompt")


def test_resolve_request_modifiers_handles_no_ast() -> None:
    """A flag-only/bare-term request (``ast=None``) passes ``base_effort`` through."""
    registry = default_registry()

    scope, effort = resolve_request_modifiers(
        None,
        registry,
        base_scope="all",
        base_effort="targeted",
    )

    assert (scope, effort) == ("all", "targeted")


def test_depth_field_evaluates_as_vacuously_true() -> None:
    """A ``depth:``/``effort:`` predicate filters nothing at either engine layer.

    It is a request-wide directive, not a per-record/per-source fact, so the
    compiled predicates must not reject a record/source just because a
    depth: clause is ANDed alongside real filters.
    """
    registry = default_registry()
    ast = parse_query("depth:exhaustive agent:codex", registry)
    compiled = compile_query(ast, registry)

    assert compiled.source_predicate is not None
    assert compiled.record_predicate is not None

    matching_source = SourceHandle(
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/history.jsonl"),
        path_kind="history_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    other_source = SourceHandle(
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
        path=pathlib.Path("/tmp/other.jsonl"),
        path_kind="history_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    matching_record = SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/history.jsonl"),
        text="deploy the service",
    )

    assert compiled.source_predicate(matching_source) is True
    assert compiled.source_predicate(other_source) is False
    assert compiled.record_predicate(matching_record) is True


def test_any_request_layer_field_exists_predicate_is_vacuously_true() -> None:
    """A ``field:*`` existence check on a request-layer field never rejects a record.

    Generalizes beyond ``depth`` itself: a custom :class:`FieldRegistry` can
    declare its own ``layer="request"`` field, and ``_evaluate_record`` must
    still short-circuit ``FieldExistsNode`` for it by consulting the
    registry rather than a hard-coded field-name set that only knows
    ``depth`` — the same rule ``depth:*`` already gets, proven directly here
    since ``depth:*`` alone under NOT/OR is a compile error and can't
    otherwise reach evaluation as a bare positive predicate in one assert.
    """
    registry = FieldRegistry(
        specs=(
            *default_registry().specs,
            FieldSpec(name="mode", kind="enum", layer="request", enum_values=("a", "b")),
        ),
    )
    ast = parse_query("mode:* agent:codex", registry)
    compiled = compile_query(ast, registry)
    assert compiled.record_predicate is not None

    record = SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/history.jsonl"),
        text="deploy the service",
    )

    assert compiled.record_predicate(record) is True


def test_a_non_depth_request_layer_field_is_not_read_as_an_effort() -> None:
    """A custom request-layer field's value is not mistaken for a ``SearchEffort``.

    ``layer="request"`` is the general "extract instead of evaluate"
    category; ``depth`` is one specific field in it. Combining a custom
    ``mode:a`` predicate with ``depth:targeted`` must resolve the real
    directive cleanly, not raise "conflicting depth:/effort: directives"
    because ``_effort_directive`` mistook ``mode``'s value ``"a"`` for a
    second, disagreeing effort.
    """
    registry = FieldRegistry(
        specs=(
            *default_registry().specs,
            FieldSpec(name="mode", kind="enum", layer="request", enum_values=("a", "b")),
        ),
    )
    ast = parse_query("mode:a depth:targeted foo", registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
    )

    assert (scope, effort) == ("all", "targeted")


def test_a_non_depth_request_layer_field_alone_leaves_effort_at_its_default() -> None:
    """A custom request-layer field alone, with no ``depth:`` term, doesn't set effort."""
    registry = FieldRegistry(
        specs=(
            *default_registry().specs,
            FieldSpec(name="mode", kind="enum", layer="request", enum_values=("a", "b")),
        ),
    )
    ast = parse_query("mode:a foo", registry)

    scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
    )

    assert (scope, effort) == ("prompts", "prompt")


# ---------------------------------------------------------------------------
# NOT/OR rejection: negating or OR-ing a request-layer directive would
# silently flip an entire AND chain to always-false (NOT) or an entire OR
# chain to always-true (OR) — see resolve_request_modifiers's docstring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query_text",
    [
        "NOT depth:targeted foo",
        "-depth:targeted foo",
        "(depth:targeted OR agent:codex) foo",
        "NOT depth:*",
    ],
)
def test_depth_field_rejects_not_or_composition(query_text: str) -> None:
    """A request-layer directive under NOT/OR is a compile error, not silence."""
    registry = default_registry()
    ast = parse_query(query_text, registry)

    with pytest.raises(QueryCompileError, match="request-wide directive"):
        compile_query(ast, registry)


@pytest.mark.parametrize(
    "query_text",
    [
        "NOT depth:targeted foo",
        "(depth:targeted OR agent:codex) foo",
    ],
)
def test_resolve_request_modifiers_rejects_not_or_standalone(query_text: str) -> None:
    """``resolve_request_modifiers`` enforces NOT/OR rejection on its own.

    A caller that reaches this function without first calling
    ``compile_query`` (which would normally raise via ``_validate_ast``)
    still fails closed instead of silently ignoring the negated/OR'd
    directive — see its docstring's ``Raises`` section.
    """
    registry = default_registry()
    ast = parse_query(query_text, registry)

    with pytest.raises(QueryCompileError, match="request-wide directive"):
        resolve_request_modifiers(ast, registry, base_scope="all", base_effort="prompt")


def test_depth_field_rejects_conflicting_and_directives() -> None:
    """Two ANDed depth:/effort: clauses resolving to different values error.

    ``compile_query`` never touches effort at all (only
    ``resolve_request_modifiers`` extracts it), so the conflict only
    surfaces when a caller actually resolves the directive.
    """
    registry = default_registry()
    ast = parse_query("depth:targeted AND effort:exhaustive foo", registry)

    with pytest.raises(QueryCompileError, match="conflicting depth"):
        resolve_request_modifiers(ast, registry, base_scope="all", base_effort="prompt")


def test_depth_field_allows_matching_and_directives() -> None:
    """The same effort value repeated under AND (``deep`` and ``targeted``) is fine."""
    registry = default_registry()
    ast = parse_query("depth:targeted AND effort:deep foo", registry)

    _scope, effort = resolve_request_modifiers(
        ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
    )

    assert effort == "targeted"


# ---------------------------------------------------------------------------
# Cross-surface: CLI (parse_args -> SearchArgs/GrepArgs.effort).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["search", "grep"])
@pytest.mark.parametrize(
    ("query_text", "expected_scope", "expected_effort"),
    [
        ("scope:all depth:targeted foo", "all", "targeted"),
        ("scope:all depth:exhaustive foo", "all", "exhaustive"),
        ("depth:targeted foo", "all", "targeted"),
        ("foo", "prompts", "prompt"),
    ],
)
def test_depth_field_works_through_the_cli_path(
    command: str,
    query_text: str,
    expected_scope: str,
    expected_effort: str,
) -> None:
    """The CLI path (``parse_args`` -> ``SearchArgs``/``GrepArgs``) resolves the directive too.

    ``depth:targeted foo`` alone widens the implicit ``"prompts"`` default to
    ``"all"`` on its own — mirroring ``--deep`` alone (no ``--scope``) —
    exercised here alongside the explicit ``scope:all depth:targeted foo``
    spelling to prove both reach the same effective request. See
    ``test_depth_field_targeted_conflicts_with_explicit_prompts_scope`` below
    for the case where scope was pinned to prompts on purpose.
    """
    argv = [command, query_text]

    parsed = parse_args(argv)

    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.scope == expected_scope
    assert parsed.effort == expected_effort


@pytest.mark.parametrize(
    ("command", "argv_tail"),
    [
        ("search", ["--scope", "prompts", "depth:targeted foo"]),
        ("grep", ["--scope", "prompts", "depth:targeted foo"]),
        ("search", ["scope:prompts depth:targeted foo"]),
    ],
)
def test_depth_field_targeted_conflicts_with_explicit_prompts_scope(
    command: str,
    argv_tail: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``depth:targeted`` still hard-errors when prompts scope was pinned on purpose.

    The implicit-default case (bare ``depth:targeted foo``) auto-widens (see
    ``test_depth_field_works_through_the_cli_path``); an explicit ``--scope
    prompts`` flag or inline ``scope:prompts`` predicate is a real, stated
    contradiction with ``depth:targeted`` and must still fail clean, the same
    way ``--deep --scope prompts`` already does.
    """
    with pytest.raises(SystemExit):
        parse_args([command, *argv_tail])

    assert "targeted effort requires conversation or all scope" in capsys.readouterr().err


def test_depth_field_conflicts_with_explicit_deep_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--deep`` plus an inline ``depth:`` term is a pick-one-syntax error."""
    with pytest.raises(SystemExit):
        parse_args(["search", "--deep", "depth:exhaustive foo"])

    assert "cannot combine --deep flag with depth: field predicate" in capsys.readouterr().err


def test_depth_field_prompt_value_conflicts_with_broad_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``depth:prompt`` with a widened scope is symmetrically rejected."""
    with pytest.raises(SystemExit):
        parse_args(["search", "scope:all depth:prompt foo"])

    assert "prompt effort requires prompt scope" in capsys.readouterr().err


def test_depth_field_is_rejected_by_find(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``find`` has no read-policy concept; a depth:/effort: pattern hard-errors."""
    with pytest.raises(SystemExit):
        parse_args(["find", "depth:targeted"])

    assert "selects a read policy, which find does not apply" in capsys.readouterr().err


_INVALID_DEPTH_MESSAGE = (
    "invalid depth value 'promt'; valid choices: prompt, targeted, deep, exhaustive"
)


def test_depth_typo_errors_on_the_cli(capsys: pytest.CaptureFixture[str]) -> None:
    """A typo'd enum value exits non-zero with the compile message, like ``kind:``."""
    with pytest.raises(SystemExit):
        parse_args(["search", "depth:promt"])

    assert _INVALID_DEPTH_MESSAGE in capsys.readouterr().err


def test_depth_typo_errors_through_build_query_from_input() -> None:
    """Same ``QueryCompileError`` message, surfaced as ``result.error`` for the TUI."""
    result = build_query_from_input("depth:promt", _base_query(), default_registry())

    assert result.query is None
    assert result.error == _INVALID_DEPTH_MESSAGE


def test_depth_field_works_through_compose_query_ast() -> None:
    """The MCP-shaped path (``compose_query_ast`` + ``resolve_request_modifiers``) resolves it."""
    registry = default_registry()
    _ast, user_ast = compose_query_ast(["depth:exhaustive", "foo"], (), registry)

    assert user_ast is not None
    scope, effort = resolve_request_modifiers(
        user_ast,
        registry,
        base_scope="prompts",
        base_effort="prompt",
    )
    assert (scope, effort) == ("prompts", "exhaustive")


# ---------------------------------------------------------------------------
# Real CLI invocation: a subprocess call to `python -m agentgrep` (the same
# entry point the `agentgrep` console script wraps) against this repo's
# codex_transcript_home fixture, with no --deep/--exhaustive flag anywhere.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_cli_json_search_with_inline_depth_reads_transcripts(
    codex_transcript_home: pathlib.Path,
) -> None:
    """``depth:exhaustive`` in query text alone authorizes a transcript read.

    ``codex_transcript_home`` (tests/conftest.py) seeds a home whose only
    Codex evidence is a session *transcript* — no coalesced prompt-history
    file at all — carrying the text "deep-only prompt". At the default
    ``effort="prompt"`` read policy the engine never opens that transcript,
    so the term cannot match; only an ``exhaustive`` (or ``targeted``) read
    policy opens it. This drives the actual ``agentgrep`` entry point in a
    subprocess against that real fixture, proving the literal
    ``depth:exhaustive`` token — not a CLI flag — is what flips the read
    policy for a real search run. ``test_depth_field_works_through_the_cli_path``
    already proves the cheaper parse/resolve-only contract on the default
    lane; this adds the one thing that can't prove — that the engine
    actually reads more files — so it stays slow rather than dropped
    (tests/AGENTS.md: subprocess tests belong in the slow lane).
    """
    env = dict(os.environ)
    env["HOME"] = str(codex_transcript_home)
    env["AGENTGREP_WSL_USERS_ROOT"] = str(codex_transcript_home / "missing-wsl-root")

    def run_json(query: str, *, expected_returncode: int) -> dict[str, t.Any]:
        # agentgrep exits rg-style: 0 with matches, 1 with none — the fast
        # (prompt-effort) run below is expected to find nothing.
        completed = subprocess.run(
            [sys.executable, "-m", "agentgrep", "search", query, "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert completed.returncode == expected_returncode, completed.stderr
        return t.cast("dict[str, t.Any]", json.loads(completed.stdout))

    fast_payload = run_json("deep-only", expected_returncode=1)
    assert fast_payload["query"]["effort"] == "prompt"
    assert fast_payload["query"]["scope"] == "prompts"
    assert fast_payload["results"] == []

    exhaustive_payload = run_json("depth:exhaustive deep-only", expected_returncode=0)
    assert exhaustive_payload["query"]["effort"] == "exhaustive"
    assert exhaustive_payload["query"]["scope"] == "prompts"
    assert [r["text"] for r in exhaustive_payload["results"]] == ["deep-only prompt"]
    assert exhaustive_payload["summary"]["effort"]["completed"] == "exhaustive"
