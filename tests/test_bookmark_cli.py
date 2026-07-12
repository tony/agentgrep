# ruff: noqa: D103
"""Focused CLI contracts for durable bookmarks."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

import pytest

import agentgrep
from agentgrep.bookmarks import BookmarkStore

CONTENT_ID = "agc1:00000000000000000000000000"
OTHER_CONTENT_ID = "agc1:11111111111111111111111111"
RECORD_ID = "agr1:22222222222222222222222222"
THREAD_ID = "agt1:33333333333333333333333333"
CREATED_AT = "2026-07-12T12:00:00Z"


def test_bookmark_cli_contracts_are_public() -> None:
    from agentgrep.cli.parser import BookmarkArgs
    from agentgrep.cli.render import run_bookmark_command

    assert agentgrep.BookmarkArgs is BookmarkArgs
    assert agentgrep.run_bookmark_command is run_bookmark_command


def test_parser_bundle_preserves_positional_constructor() -> None:
    parsers = tuple(argparse.ArgumentParser() for _ in range(4))

    bundle = agentgrep.ParserBundle(*parsers)

    assert (
        bundle.parser,
        bundle.find_parser,
        bundle.grep_parser,
        bundle.search_parser,
    ) == parsers
    assert bundle.bookmark_parser is None


def test_parser_bundle_preserves_keyword_constructor() -> None:
    parsers = tuple(argparse.ArgumentParser() for _ in range(4))

    bundle = agentgrep.ParserBundle(
        parser=parsers[0],
        find_parser=parsers[1],
        grep_parser=parsers[2],
        search_parser=parsers[3],
    )

    assert (
        bundle.parser,
        bundle.find_parser,
        bundle.grep_parser,
        bundle.search_parser,
    ) == parsers
    assert bundle.bookmark_parser is None


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            (
                "bookmark",
                "add",
                RECORD_ID,
                "--content-id",
                CONTENT_ID,
                "--json",
            ),
            ("add", RECORD_ID, CONTENT_ID, True),
        ),
        (
            ("bookmark", "remove", THREAD_ID, "--json"),
            ("remove", THREAD_ID, None, True),
        ),
        (("bookmark", "list"), ("list", None, None, False)),
    ],
    ids=["add", "remove", "list"],
)
def test_parse_args_returns_typed_bookmark_args(
    argv: tuple[str, ...],
    expected: tuple[str, str | None, str | None, bool],
) -> None:
    parsed = agentgrep.parse_args(argv)

    assert isinstance(parsed, agentgrep.BookmarkArgs)
    assert (parsed.action, parsed.target_id, parsed.content_id, parsed.json) == expected


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("bookmark", "add", RECORD_ID), "--content-id"),
        (
            ("bookmark", "add", CONTENT_ID, "--content-id", OTHER_CONTENT_ID),
            "only valid for agr1:",
        ),
        (
            ("bookmark", "add", THREAD_ID, "--content-id", CONTENT_ID),
            "only valid for agr1:",
        ),
        (
            ("bookmark", "add", RECORD_ID, "--content-id", THREAD_ID),
            "agc1:",
        ),
        (("bookmark", "remove", "agr1:short"), "canonical"),
        (("bookmark", "add", "unknown:value"), "canonical"),
    ],
    ids=[
        "record-needs-content",
        "content-rejects-validation",
        "thread-rejects-validation",
        "record-needs-agc1",
        "short-id",
        "unknown-prefix",
    ],
)
def test_bookmark_parser_rejects_invalid_id_combinations(
    argv: tuple[str, ...],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        agentgrep.parse_args(argv)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_bookmark_cli_infers_scope_from_each_target(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert agentgrep.main(["bookmark", "add", CONTENT_ID]) == 0
    assert agentgrep.main(["bookmark", "add", THREAD_ID]) == 0
    assert (
        agentgrep.main(
            [
                "bookmark",
                "add",
                RECORD_ID,
                "--content-id",
                OTHER_CONTENT_ID,
            ]
        )
        == 0
    )
    capsys.readouterr()

    entries = BookmarkStore().list()
    assert [(entry.target_id, entry.scope, entry.content_id) for entry in entries] == [
        (CONTENT_ID, "content", None),
        (THREAD_ID, "thread", None),
        (RECORD_ID, "record", OTHER_CONTENT_ID),
    ]


def test_bookmark_cli_reports_idempotent_status(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert agentgrep.main(["bookmark", "add", CONTENT_ID]) == 0
    assert capsys.readouterr().out == f"added {CONTENT_ID}\n"
    assert agentgrep.main(["bookmark", "add", CONTENT_ID]) == 0
    assert capsys.readouterr().out == f"unchanged {CONTENT_ID}\n"
    assert agentgrep.main(["bookmark", "remove", CONTENT_ID]) == 0
    assert capsys.readouterr().out == f"removed {CONTENT_ID}\n"
    assert agentgrep.main(["bookmark", "remove", CONTENT_ID]) == 0
    assert capsys.readouterr().out == f"unchanged {CONTENT_ID}\n"


def test_bookmark_cli_json_output_is_stable(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = BookmarkStore()
    store.add(THREAD_ID, created_at=CREATED_AT)

    assert agentgrep.main(["bookmark", "add", THREAD_ID, "--json"]) == 0
    assert capsys.readouterr().out == (
        '{"action":"unchanged","entry":{"content_id":null,'
        '"created_at":"2026-07-12T12:00:00Z","scope":"thread",'
        '"target_id":"agt1:33333333333333333333333333"}}\n'
    )
    assert agentgrep.main(["bookmark", "list", "--json"]) == 0
    assert capsys.readouterr().out == (
        '{"bookmarks":[{"content_id":null,"created_at":"2026-07-12T12:00:00Z",'
        '"scope":"thread","target_id":"agt1:33333333333333333333333333"}]}\n'
    )


def test_bookmark_cli_human_list_output(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = BookmarkStore()
    store.add(THREAD_ID, created_at=CREATED_AT)
    store.add(RECORD_ID, content_id=CONTENT_ID, created_at=CREATED_AT)

    assert agentgrep.main(["bookmark", "list"]) == 0

    assert capsys.readouterr().out == (
        f"thread\t{THREAD_ID}\t-\t{CREATED_AT}\n"
        f"record\t{RECORD_ID}\t{CONTENT_ID}\t{CREATED_AT}\n"
    )


def test_bookmark_cli_empty_list_output(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert agentgrep.main(["bookmark", "list"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "No bookmarks.\n"
    assert captured.err == ""


def test_bookmark_cli_capacity_error_is_path_free(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_home = tmp_path / "private-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    path = data_home / "agentgrep" / "bookmarks.json"
    path.parent.mkdir(parents=True)
    entries = [
        {
            "content_id": None,
            "created_at": CREATED_AT,
            "scope": "content",
            "target_id": f"agc1:{index:026x}",
        }
        for index in range(200)
    ]
    path.write_text(
        json.dumps(
            {"entries": entries, "schema_version": 1},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    target_id = "agc1:vvvvvvvvvvvvvvvvvvvvvvvvvv"

    assert agentgrep.main(["bookmark", "add", target_id]) == 1

    captured = capsys.readouterr()
    assert "capacity" in captured.err
    assert str(path) not in captured.err
    assert "Traceback" not in captured.err


def test_bookmark_cli_corruption_error_leaks_no_path_or_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_home = tmp_path / "private-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    path = data_home / "agentgrep" / "bookmarks.json"
    path.parent.mkdir(parents=True)
    private_body = "private prompt body that must stay hidden"
    path.write_text(private_body, encoding="utf-8")

    assert agentgrep.main(["bookmark", "list"]) == 1

    captured = capsys.readouterr()
    assert "corrupt" in captured.err
    assert str(path) not in captured.err
    assert private_body not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.slow
def test_root_help_does_not_import_bookmark_persistence() -> None:
    code = (
        "import agentgrep, sys; "
        "agentgrep.parse_args([]); "
        "raise SystemExit('agentgrep.bookmarks' in sys.modules)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
