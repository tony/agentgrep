"""Tests for DB cache CLI controls."""

from __future__ import annotations

import json
import typing as t

import pytest

import agentgrep
import agentgrep.cli.render as render
from agentgrep.db import SyncResult


class CacheFlagCase(t.NamedTuple):
    """Named case for search-shaped cache flag parsing."""

    test_id: str
    argv: tuple[str, ...]
    expected_cache_mode: agentgrep.CacheMode


class StructuredOutputCase(t.NamedTuple):
    """Named case for small structured CLI payload rendering."""

    test_id: str
    payload: object
    output_mode: t.Literal["json", "ndjson"]
    expected_documents: tuple[object, ...]


class CommandGroupHelpCase(t.NamedTuple):
    """Named case for command-group directory help behavior."""

    test_id: str
    argv: tuple[str, ...]
    expected_usage: str
    expected_examples_heading: str


CACHE_FLAG_CASES: tuple[CacheFlagCase, ...] = (
    CacheFlagCase("search-default-auto", ("search", "ruff"), "auto"),
    CacheFlagCase("search-no-cache-off", ("search", "--no-cache", "ruff"), "off"),
    CacheFlagCase("search-require", ("search", "--cache", "require", "ruff"), "require"),
    CacheFlagCase("grep-default-auto", ("grep", "ruff"), "auto"),
    CacheFlagCase("grep-no-cache-off", ("grep", "--no-cache", "ruff"), "off"),
    CacheFlagCase("grep-cache-off", ("grep", "--cache", "off", "ruff"), "off"),
)


COMMAND_GROUP_HELP_CASES: tuple[CommandGroupHelpCase, ...] = (
    CommandGroupHelpCase(
        test_id="db",
        argv=("db",),
        expected_usage="usage: agentgrep db",
        expected_examples_heading="db examples:",
    ),
)


STRUCTURED_OUTPUT_CASES: tuple[StructuredOutputCase, ...] = (
    StructuredOutputCase(
        test_id="dataclass-json",
        payload=SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
        output_mode="json",
        expected_documents=({"sources_synced": 1, "records_indexed": 2, "records_removed": 0},),
    ),
    StructuredOutputCase(
        test_id="dataclass-ndjson",
        payload=SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
        output_mode="ndjson",
        expected_documents=({"sources_synced": 1, "records_indexed": 2, "records_removed": 0},),
    ),
    StructuredOutputCase(
        test_id="list-ndjson",
        payload=[
            SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
            SyncResult(sources_synced=3, records_indexed=4, records_removed=1),
        ],
        output_mode="ndjson",
        expected_documents=(
            {"sources_synced": 1, "records_indexed": 2, "records_removed": 0},
            {"sources_synced": 3, "records_indexed": 4, "records_removed": 1},
        ),
    ),
    StructuredOutputCase(
        test_id="mapping-ndjson",
        payload={
            "results": [
                SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
            ],
        },
        output_mode="ndjson",
        expected_documents=(
            {
                "results": [
                    {"sources_synced": 1, "records_indexed": 2, "records_removed": 0},
                ],
            },
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    CACHE_FLAG_CASES,
    ids=[case.test_id for case in CACHE_FLAG_CASES],
)
def test_search_shaped_commands_parse_cache_mode(case: CacheFlagCase) -> None:
    """Search and grep expose cache controls for fresh/cold benchmark runs."""
    parsed = agentgrep.parse_args(case.argv)

    assert isinstance(parsed, (agentgrep.SearchArgs, agentgrep.GrepArgs))
    assert parsed.cache_mode == case.expected_cache_mode


@pytest.mark.parametrize(
    "case",
    COMMAND_GROUP_HELP_CASES,
    ids=[case.test_id for case in COMMAND_GROUP_HELP_CASES],
)
def test_command_groups_without_actions_print_help_directory(
    case: CommandGroupHelpCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB command groups act as help directories."""
    parsed = agentgrep.parse_args(case.argv)

    captured = capsys.readouterr()
    assert parsed is None
    assert captured.err == ""
    assert case.expected_usage in captured.out
    assert case.expected_examples_heading in captured.out


@pytest.mark.parametrize(
    "case",
    COMMAND_GROUP_HELP_CASES,
    ids=[case.test_id for case in COMMAND_GROUP_HELP_CASES],
)
def test_command_group_main_invocation_returns_success(
    case: CommandGroupHelpCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI entry point treats directory help as a successful command."""
    exit_code = agentgrep.main(case.argv)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert case.expected_usage in captured.out


@pytest.mark.parametrize(
    "case",
    STRUCTURED_OUTPUT_CASES,
    ids=[case.test_id for case in STRUCTURED_OUTPUT_CASES],
)
def test_small_structured_commands_emit_machine_readable_output(
    case: StructuredOutputCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB helpers honor JSON and NDJSON."""
    render._print_json_or_text(case.payload, output_mode=case.output_mode)

    captured = capsys.readouterr()
    if case.output_mode == "json":
        assert (json.loads(captured.out),) == case.expected_documents
        return

    documents = tuple(json.loads(line) for line in captured.out.splitlines() if line.strip())
    assert documents == case.expected_documents


def test_db_status_command_parses_db_path() -> None:
    """DB commands have typed parser output separate from search args."""
    parsed = agentgrep.parse_args(
        ("db", "status", "--db", "/tmp/agentgrep.sqlite", "--json"),
    )

    assert isinstance(parsed, agentgrep.DbArgs)
    assert parsed.action == "status"
    assert parsed.db_path == "/tmp/agentgrep.sqlite"
    assert parsed.output_mode == "json"


def test_collection_is_not_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``collection`` is rejected as an unknown command."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("collection",))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid choice: 'collection'" in captured.err
    assert "db" in captured.err


def test_command_group_actions_keep_default_behavior() -> None:
    """No-arg directory help does not remove existing action defaults."""
    db = agentgrep.parse_args(("db", "sync"))

    assert isinstance(db, agentgrep.DbArgs)
    assert db.action == "sync"


def test_grep_cache_require_unsupported_query_exits_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cache-required grep reports unsupported regex queries as CLI errors."""
    from agentgrep.db import DbQueryUnsupported

    class UnsupportedDb:
        """DB stub that rejects the query shape."""

        def search_records(
            self,
            _query: agentgrep.SearchQuery,
        ) -> list[agentgrep.SearchRecord]:
            msg = "query requires live scanner"
            raise DbQueryUnsupported(msg)

    runtime = agentgrep.SearchRuntime(
        db=t.cast("t.Any", UnsupportedDb()),
        cache_mode="require",
    )
    monkeypatch.setattr(render, "_db_runtime_for_cli", lambda _mode: runtime)
    args = agentgrep.GrepArgs(
        patterns=("ruff",),
        agents=("codex",),
        scope="prompts",
        case_mode="smart",
        pattern_mode="regex",
        invert_match=False,
        count_only=False,
        files_with_matches=False,
        only_matching=False,
        no_dedupe=False,
        line_number=None,
        heading=None,
        max_count=None,
        vimgrep=False,
        column=False,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
        cache_mode="require",
    )

    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.run_grep_command(args)

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--cache require" in captured.err
    assert "Traceback" not in captured.err
