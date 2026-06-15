"""Tests for the agentgrep query AST compiler.

Covers commit 4 of the query-language project — the compiler in
:mod:`agentgrep.query.compile`. Verifies that:

- pure-text queries short-circuit to the legacy fast path
  (`CompiledQuery.is_pure_text=True`, both predicates ``None``)
- source-level predicates correctly prune sources before file I/O
- record-level predicates correctly filter parsed records
- mixed predicates compose with the right three-valued semantics
  (an OR-mixed query lets the source through but filters records)
- comparison and range predicates against the `timestamp` field
  produce the right matches against record timestamps
"""

from __future__ import annotations

import datetime as dt
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.query import (
    AndNode,
    CompiledQuery,
    FieldCmpNode,
    FieldEqNode,
    FieldRangeNode,
    NotNode,
    OrNode,
    QueryNode,
    TermNode,
    build_query_from_input,
    compile_query,
    default_registry,
    parse_query,
)
from agentgrep.query.compile import QueryCompileError
from agentgrep.query.dates import set_now_override


@pytest.fixture(autouse=True)
def _frozen_now() -> t.Iterator[None]:
    """Pin "now" so relative-date tests are deterministic."""
    set_now_override(
        lambda: dt.datetime(2026, 5, 22, 14, 0, 0, tzinfo=dt.UTC),
    )
    try:
        yield
    finally:
        set_now_override(None)


def _make_source(
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path: str = "/tmp/codex/sessions/abc.jsonl",
    mtime_ns: int = 0,
) -> agentgrep.SourceHandle:
    """Build a synthetic SourceHandle for compiler tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=mtime_ns,
    )


def _make_record(
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path: str = "/tmp/codex/sessions/abc.jsonl",
    text: str = "bliss prompt content",
    title: str | None = None,
    role: str | None = "user",
    timestamp: str | None = None,
    model: str | None = None,
    kind: t.Literal["prompt", "history"] = "prompt",
) -> agentgrep.SearchRecord:
    """Build a synthetic SearchRecord for compiler tests."""
    return agentgrep.SearchRecord(
        kind=kind,
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        text=text,
        title=title,
        role=role,
        timestamp=timestamp,
        model=model,
        session_id=None,
        conversation_id=None,
        metadata={},
    )


# ----- pure-text fast path -------------------------------------------------


class PureTextFastPathCase(t.NamedTuple):
    """Parametrized case for the legacy fast-path detection."""

    test_id: str
    query: str
    expected_pure: bool
    expected_terms: tuple[str, ...]


PURE_TEXT_CASES: tuple[PureTextFastPathCase, ...] = (
    PureTextFastPathCase(
        test_id="single-term-is-pure",
        query="bliss",
        expected_pure=True,
        expected_terms=("bliss",),
    ),
    PureTextFastPathCase(
        test_id="implicit-and-terms-pure",
        query="bliss codex deploy",
        expected_pure=True,
        expected_terms=("bliss", "codex", "deploy"),
    ),
    PureTextFastPathCase(
        test_id="field-predicate-not-pure",
        query="agent:codex bliss",
        expected_pure=False,
        expected_terms=("bliss",),
    ),
    PureTextFastPathCase(
        test_id="negation-not-pure",
        query="bliss NOT codex",
        expected_pure=False,
        expected_terms=("bliss", "codex"),
    ),
    PureTextFastPathCase(
        test_id="or-not-pure",
        query="bliss OR codex",
        expected_pure=False,
        expected_terms=("bliss", "codex"),
    ),
    PureTextFastPathCase(
        test_id="phrase-only-is-pure",
        query='"deploy v1"',
        expected_pure=True,
        expected_terms=("deploy v1",),
    ),
)


@pytest.mark.parametrize(
    "case",
    PURE_TEXT_CASES,
    ids=[c.test_id for c in PURE_TEXT_CASES],
)
def test_compile_query_detects_pure_text_fast_path(
    case: PureTextFastPathCase,
) -> None:
    """Pure-text queries route to the legacy fast path with no predicates."""
    ast = parse_query(case.query, default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.is_pure_text is case.expected_pure
    assert compiled.text_terms == case.expected_terms
    if case.expected_pure:
        assert compiled.source_predicate is None
        assert compiled.record_predicate is None
    else:
        assert compiled.source_predicate is not None
        assert compiled.record_predicate is not None


# ----- source-side conservative evaluation ---------------------------------


class SourcePredicateCase(t.NamedTuple):
    """Parametrized case for source-side conservative pruning."""

    test_id: str
    query: str
    source_kwargs: dict[str, t.Any]
    expected_passes: bool


SOURCE_PREDICATE_CASES: tuple[SourcePredicateCase, ...] = (
    SourcePredicateCase(
        test_id="agent-match-passes",
        query="agent:codex",
        source_kwargs={"agent": "codex"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="agent-mismatch-prunes",
        query="agent:codex",
        source_kwargs={"agent": "claude"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="negated-agent-mismatch-passes",
        query="-agent:claude",
        source_kwargs={"agent": "codex"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="negated-agent-match-prunes",
        query="-agent:claude",
        source_kwargs={"agent": "claude"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="and-source-record-passes-when-source-ok",
        query="agent:codex bliss",
        source_kwargs={"agent": "codex"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="and-source-record-prunes-when-source-fails",
        query="agent:codex bliss",
        source_kwargs={"agent": "claude"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="or-mixed-record-passes-conservatively",
        query="agent:codex OR bliss",
        source_kwargs={"agent": "claude"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="or-source-source-prunes-when-both-fail",
        query="agent:codex OR agent:cursor-cli",
        source_kwargs={"agent": "claude"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="or-source-source-passes-when-one-matches",
        query="agent:codex OR agent:cursor-cli",
        source_kwargs={"agent": "cursor-cli"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="path-substring-passes",
        query="path:codex",
        source_kwargs={"path": "/tmp/codex/sessions/abc.jsonl"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="path-substring-mismatch-prunes",
        query="path:claude",
        source_kwargs={"path": "/tmp/codex/sessions/abc.jsonl"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="path-glob-prunes",
        query="path:*.txt",
        source_kwargs={"path": "/tmp/codex/sessions/abc.jsonl"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="unknown-mtime-passes-through-as-U",
        query="mtime:>2026-01-01",
        source_kwargs={"mtime_ns": 0},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="agent-exists-always-passes",
        query="agent:*",
        source_kwargs={"agent": "codex"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="negated-store-exists-prunes",
        query="-store:*",
        source_kwargs={"store": "sessions"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="negated-unknown-mtime-exists-passes",
        query="-mtime:*",
        source_kwargs={"mtime_ns": 0},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="store-wildcard-passes",
        query="store:codex*",
        source_kwargs={"store": "codex.sessions"},
        expected_passes=True,
    ),
    SourcePredicateCase(
        test_id="store-wildcard-prunes",
        query="store:claude*",
        source_kwargs={"store": "codex.sessions"},
        expected_passes=False,
    ),
    SourcePredicateCase(
        test_id="store-literal-substring-passes",
        query="store:sessions",
        source_kwargs={"store": "codex.sessions"},
        expected_passes=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    SOURCE_PREDICATE_CASES,
    ids=[c.test_id for c in SOURCE_PREDICATE_CASES],
)
def test_compile_query_source_predicate_prunes(
    case: SourcePredicateCase,
) -> None:
    """The compiled source predicate prunes the right sources."""
    ast = parse_query(case.query, default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.source_predicate is not None
    source = _make_source(**case.source_kwargs)
    assert compiled.source_predicate(source) is case.expected_passes


def test_path_query_expands_current_user_home_for_source_predicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``path:~`` matches source paths under the current user's home."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    ast = parse_query("path:~/.codex", default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.source_predicate is not None

    matching = _make_source(path=str(home / ".codex" / "history.jsonl"))
    sibling = _make_source(path=str(tmp_path / "home-other" / ".codex" / "history.jsonl"))

    assert compiled.source_predicate(matching) is True
    assert compiled.source_predicate(sibling) is False


def test_path_query_home_root_does_not_match_sibling_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``path:~`` matches the home tree without leaking into sibling prefixes."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    ast = parse_query("path:~", default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.source_predicate is not None

    child = _make_source(path=str(home / ".codex" / "history.jsonl"))
    sibling = _make_source(path=str(tmp_path / "home-other" / ".codex" / "history.jsonl"))

    assert compiled.source_predicate(child) is True
    assert compiled.source_predicate(sibling) is False


def test_path_query_home_glob_expands_for_source_predicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``path:~`` expansion preserves glob matching semantics."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    ast = parse_query("path:~/.codex/*", default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.source_predicate is not None

    source = _make_source(path=str(home / ".codex" / "history.jsonl"))

    assert compiled.source_predicate(source) is True


# ----- record-side exact evaluation ----------------------------------------


class RecordPredicateCase(t.NamedTuple):
    """Parametrized case for record-side exact evaluation."""

    test_id: str
    query: str
    record_kwargs: dict[str, t.Any]
    expected_matches: bool


RECORD_PREDICATE_CASES: tuple[RecordPredicateCase, ...] = (
    RecordPredicateCase(
        test_id="agent-record-match",
        query="agent:codex",
        record_kwargs={"agent": "codex"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="agent-record-mismatch",
        query="agent:codex",
        record_kwargs={"agent": "claude"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="model-substring-match",
        query="model:claude",
        record_kwargs={"model": "claude-3-sonnet"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="model-substring-mismatch",
        query="model:gpt",
        record_kwargs={"model": "claude-3-sonnet"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="role-match",
        query="role:assistant",
        record_kwargs={"role": "assistant"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="scope-prompts-on-prompt-history",
        query="scope:prompts",
        record_kwargs={
            "kind": "prompt",
            "store": "codex.history",
            "adapter_id": "codex.history_jsonl.v1",
        },
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="scope-prompts-on-chat-prompt-record-layer",
        query="scope:prompts",
        record_kwargs={
            "kind": "prompt",
            "store": "codex.sessions",
            "adapter_id": "codex.sessions_jsonl.v1",
        },
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="scope-prompts-includes-transcript-only-prompt",
        query="scope:prompts",
        record_kwargs={
            "kind": "prompt",
            "agent": "pi",
            "store": "pi.sessions",
            "adapter_id": "pi.sessions_jsonl.v1",
        },
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="scope-conversations-on-chat-record",
        query="scope:conversations",
        record_kwargs={
            "kind": "history",
            "store": "codex.sessions",
            "adapter_id": "codex.sessions_jsonl.v1",
        },
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="scope-conversations-excludes-prompt-history",
        query="scope:conversations",
        record_kwargs={
            "kind": "prompt",
            "store": "codex.history",
            "adapter_id": "codex.history_jsonl.v1",
        },
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="text-substring-match",
        query="text:bliss",
        record_kwargs={"text": "alpha bliss line"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="text-substring-mismatch",
        query="text:missing",
        record_kwargs={"text": "alpha bliss line"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="and-composition-match",
        query="agent:codex bliss",
        record_kwargs={"agent": "codex", "text": "alpha bliss line"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="and-composition-fails",
        query="agent:codex bliss",
        record_kwargs={"agent": "claude", "text": "alpha bliss line"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="or-composition-one-side-match",
        query="agent:claude OR bliss",
        record_kwargs={"agent": "codex", "text": "alpha bliss line"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="negated-agent-records-pass",
        query="-agent:claude",
        record_kwargs={"agent": "codex"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="negated-agent-records-fail",
        query="-agent:claude",
        record_kwargs={"agent": "claude"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="timestamp-comparison-after",
        query="timestamp:>2026-01-01",
        record_kwargs={"timestamp": "2026-03-15T10:00:00Z"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="timestamp-comparison-before-fails",
        query="timestamp:>2026-06-01",
        record_kwargs={"timestamp": "2026-03-15T10:00:00Z"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="timestamp-range-inclusive",
        query="timestamp:[2026-01-01 TO 2026-06-01]",
        record_kwargs={"timestamp": "2026-03-15T10:00:00Z"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="timestamp-range-outside",
        query="timestamp:[2026-01-01 TO 2026-02-01]",
        record_kwargs={"timestamp": "2026-03-15T10:00:00Z"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="timestamp-no-timestamp-on-record-fails",
        query="timestamp:>2026-01-01",
        record_kwargs={"timestamp": None},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="phrase-substring-in-order-matches",
        query='agent:codex "bliss prompt"',
        record_kwargs={"agent": "codex", "text": "the bliss prompt content"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="phrase-words-out-of-order-misses",
        query='agent:codex "prompt bliss"',
        record_kwargs={"agent": "codex", "text": "the bliss prompt content"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="field-exists-model-present-matches",
        query="model:* bliss",
        record_kwargs={"model": "gpt-4", "text": "bliss"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="field-exists-model-null-misses",
        query="model:* bliss",
        record_kwargs={"model": None, "text": "bliss"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="negated-field-exists-model-null-matches",
        query="-model:* bliss",
        record_kwargs={"model": None, "text": "bliss"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="field-exists-empty-role-counts-as-absent",
        query="role:* bliss",
        record_kwargs={"role": "", "text": "bliss"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="model-wildcard-prefix-matches",
        query="model:gpt* bliss",
        record_kwargs={"model": "gpt-4", "text": "bliss"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="model-wildcard-no-match",
        query="model:gpt* bliss",
        record_kwargs={"model": "claude-3-sonnet", "text": "bliss"},
        expected_matches=False,
    ),
    RecordPredicateCase(
        test_id="model-wildcard-is-case-insensitive",
        query="model:GPT* bliss",
        record_kwargs={"model": "gpt-4", "text": "bliss"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="role-wildcard-question-mark",
        query="role:assist?nt bliss",
        record_kwargs={"role": "assistant", "text": "bliss"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="model-literal-substring-still-works",
        query="model:gpt bliss",
        record_kwargs={"model": "gpt-4", "text": "bliss"},
        expected_matches=True,
    ),
    RecordPredicateCase(
        test_id="text-field-wildcard-prefix-anchored",
        query="text:bliss* agent:codex",
        record_kwargs={"text": "bliss here", "agent": "codex"},
        expected_matches=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    RECORD_PREDICATE_CASES,
    ids=[c.test_id for c in RECORD_PREDICATE_CASES],
)
def test_compile_query_record_predicate_filters(
    case: RecordPredicateCase,
) -> None:
    """The compiled record predicate accepts/rejects records exactly."""
    ast = parse_query(case.query, default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.record_predicate is not None
    record = _make_record(**case.record_kwargs)
    assert compiled.record_predicate(record) is case.expected_matches


def test_path_query_expands_current_user_home_for_record_predicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``path:~`` matches record paths under the current user's home."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    ast = parse_query("path:~/.codex", default_registry())
    compiled = compile_query(ast, default_registry())
    assert compiled.record_predicate is not None

    matching = _make_record(path=str(home / ".codex" / "history.jsonl"))
    sibling = _make_record(path=str(tmp_path / "home-other" / ".codex" / "history.jsonl"))

    assert compiled.record_predicate(matching) is True
    assert compiled.record_predicate(sibling) is False


# ----- compile-time semantic errors ---------------------------------------


def test_comparison_against_string_field_errors_at_compile_time() -> None:
    """Comparison operators on a string field raise at compile time, not eval."""
    ast = FieldCmpNode(field="agent", op="gt", value="codex")
    with pytest.raises(QueryCompileError, match="does not support comparison"):
        _ = compile_query(ast, default_registry())


def test_unknown_enum_value_errors_at_compile_time() -> None:
    """An unknown enum value (e.g. agent:gpt4) raises at compile time."""
    ast = FieldEqNode(field="agent", value="gpt4")
    with pytest.raises(QueryCompileError, match="invalid agent value 'gpt4'"):
        _ = compile_query(ast, default_registry())


# ----- compile-time semantic validation -----------------------------------


class EnumValidationCase(t.NamedTuple):
    """Parametrized case for enum-membership validation at compile time."""

    test_id: str
    query: str
    expected_fragment: str


ENUM_VALIDATION_CASES: tuple[EnumValidationCase, ...] = (
    EnumValidationCase(
        test_id="agent-unknown-model",
        query="agent:gpt4 bliss",
        expected_fragment="invalid agent value 'gpt4'",
    ),
    EnumValidationCase(
        test_id="agent-typo",
        query="agent:clauded bliss",
        expected_fragment="invalid agent value 'clauded'",
    ),
    EnumValidationCase(
        test_id="scope-unknown-value",
        query="scope:bogus bliss",
        expected_fragment="invalid scope value 'bogus'",
    ),
    EnumValidationCase(
        test_id="scope-near-miss",
        query="scope:prompt bliss",
        expected_fragment="invalid scope value 'prompt'",
    ),
    EnumValidationCase(
        test_id="enum-wildcard-not-supported",
        query="agent:co* bliss",
        expected_fragment="invalid agent value 'co*'",
    ),
)


@pytest.mark.parametrize(
    "case",
    ENUM_VALIDATION_CASES,
    ids=[c.test_id for c in ENUM_VALIDATION_CASES],
)
def test_enum_value_validated_at_compile_time(case: EnumValidationCase) -> None:
    """Unknown enum values raise QueryCompileError before predicates are built."""
    ast = parse_query(case.query, default_registry())
    with pytest.raises(QueryCompileError) as exc_info:
        _ = compile_query(ast, default_registry())
    assert case.expected_fragment in str(exc_info.value)


class DateValidationCase(t.NamedTuple):
    """Parametrized case for date-literal validation at compile time."""

    test_id: str
    query: str
    expected_fragment: str


DATE_VALIDATION_CASES: tuple[DateValidationCase, ...] = (
    DateValidationCase(
        test_id="timestamp-comparison-bad-date",
        query="timestamp:>bogus bliss",
        expected_fragment="invalid date in timestamp",
    ),
    DateValidationCase(
        test_id="timestamp-range-bad-lo",
        query="timestamp:[bogus TO 2026] bliss",
        expected_fragment="invalid date in timestamp range",
    ),
    DateValidationCase(
        test_id="timestamp-range-bad-hi",
        query="timestamp:{2025 TO bogus} bliss",
        expected_fragment="invalid date in timestamp range",
    ),
    DateValidationCase(
        test_id="mtime-bad-comparison",
        query="mtime:>not-a-date bliss",
        expected_fragment="invalid date in mtime",
    ),
    DateValidationCase(
        test_id="timestamp-eq-bad-date",
        query="timestamp:nonsense bliss",
        expected_fragment="invalid date in timestamp",
    ),
)


@pytest.mark.parametrize(
    "case",
    DATE_VALIDATION_CASES,
    ids=[c.test_id for c in DATE_VALIDATION_CASES],
)
def test_date_literal_validated_at_compile_time(case: DateValidationCase) -> None:
    """Unparseable date literals raise QueryCompileError before predicates are built."""
    ast = parse_query(case.query, default_registry())
    with pytest.raises(QueryCompileError) as exc_info:
        _ = compile_query(ast, default_registry())
    assert case.expected_fragment in str(exc_info.value)


class ComparisonOnStringFieldCase(t.NamedTuple):
    """Parametrized case for comparison ops against non-comparable fields."""

    test_id: str
    query: str
    expected_fragment: str


COMPARISON_ON_STRING_CASES: tuple[ComparisonOnStringFieldCase, ...] = (
    ComparisonOnStringFieldCase(
        test_id="comparison-against-agent",
        query="agent:>codex bliss",
        expected_fragment="'agent' does not support comparison",
    ),
    ComparisonOnStringFieldCase(
        test_id="range-against-scope",
        query="scope:[prompts TO conversations] bliss",
        expected_fragment="'scope' does not support range",
    ),
    ComparisonOnStringFieldCase(
        test_id="comparison-against-path",
        query="path:<somewhere bliss",
        expected_fragment="'path' does not support comparison",
    ),
)


@pytest.mark.parametrize(
    "case",
    COMPARISON_ON_STRING_CASES,
    ids=[c.test_id for c in COMPARISON_ON_STRING_CASES],
)
def test_comparison_or_range_on_non_supported_field(
    case: ComparisonOnStringFieldCase,
) -> None:
    """Comparison / range operators against non-comparable fields raise at compile time."""
    ast = parse_query(case.query, default_registry())
    with pytest.raises(QueryCompileError) as exc_info:
        _ = compile_query(ast, default_registry())
    assert case.expected_fragment in str(exc_info.value)


def test_validation_walks_into_or_and_not_branches() -> None:
    """Validation finds bad predicates nested under OR / NOT / parens."""
    ast = parse_query("(agent:codex OR agent:gpt4) bliss", default_registry())
    with pytest.raises(QueryCompileError, match="invalid agent value 'gpt4'"):
        _ = compile_query(ast, default_registry())


def test_compiled_query_is_immutable_dataclass() -> None:
    """CompiledQuery is frozen so consumers can pass it across boundaries."""
    import dataclasses

    ast = parse_query("bliss", default_registry())
    compiled = compile_query(ast, default_registry())
    assert dataclasses.is_dataclass(compiled)
    params = t.cast("t.Any", type(compiled)).__dataclass_params__
    assert params.frozen is True


def test_compile_query_returns_compiled_query_type() -> None:
    """Sanity check on the return type so library consumers can rely on it."""
    ast: QueryNode = TermNode(value="bliss")
    compiled = compile_query(ast, default_registry())
    assert isinstance(compiled, CompiledQuery)


def test_collect_terms_descends_through_or_and_not() -> None:
    """Text terms nested under OR / NOT still show up in text_terms."""
    ast = parse_query("bliss OR (deploy NOT codex)", default_registry())
    compiled = compile_query(ast, default_registry())
    assert set(compiled.text_terms) == {"bliss", "deploy", "codex"}


def test_text_field_terms_show_up_in_text_terms() -> None:
    """`text:foo` contributes 'foo' to text_terms just like a bare term."""
    ast = parse_query("text:bliss agent:codex", default_registry())
    compiled = compile_query(ast, default_registry())
    assert "bliss" in compiled.text_terms


_ = (AndNode, FieldRangeNode, NotNode, OrNode)  # used in case data; keep imports live


class ScopeWidenCase(t.NamedTuple):
    """One build-query input and the discovery scope it should resolve to."""

    test_id: str
    query: str
    base_scope: agentgrep.SearchScope
    expected_scope: agentgrep.SearchScope


SCOPE_WIDEN_CASES: tuple[ScopeWidenCase, ...] = (
    ScopeWidenCase(
        test_id="scope-conversations-widens-discovery",
        query="scope:conversations",
        base_scope="prompts",
        expected_scope="all",
    ),
    ScopeWidenCase(
        test_id="scope-all-widens-discovery",
        query="scope:all",
        base_scope="prompts",
        expected_scope="all",
    ),
    ScopeWidenCase(
        test_id="scope-prompts-widens-from-conversations",
        query="scope:prompts",
        base_scope="conversations",
        expected_scope="all",
    ),
    ScopeWidenCase(
        test_id="negated-scope-widens",
        query="-scope:prompts",
        base_scope="prompts",
        expected_scope="all",
    ),
    ScopeWidenCase(
        test_id="no-scope-predicate-keeps-base",
        query="bliss",
        base_scope="prompts",
        expected_scope="prompts",
    ),
    ScopeWidenCase(
        test_id="other-field-keeps-base-scope",
        query="agent:codex bliss",
        base_scope="conversations",
        expected_scope="conversations",
    ),
)


@pytest.mark.parametrize(
    "case",
    SCOPE_WIDEN_CASES,
    ids=[c.test_id for c in SCOPE_WIDEN_CASES],
)
def test_build_query_from_input_widens_discovery_scope_for_scope_predicate(
    case: ScopeWidenCase,
) -> None:
    """A ``scope:`` predicate widens the coarse discovery scope to ``all``.

    The scope predicate filters records, but discovery decides which stores
    are opened; without widening, ``scope:conversations`` against a
    prompts-scoped box would open no conversation stores and match nothing.
    """
    base = agentgrep.SearchQuery(
        terms=(),
        scope=case.base_scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
    )
    result = build_query_from_input(case.query, base, default_registry())
    assert result.query is not None
    assert result.query.scope == case.expected_scope
