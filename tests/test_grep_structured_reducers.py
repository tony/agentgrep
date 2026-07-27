"""Structured grep output and terminal-reducer contracts."""

from __future__ import annotations

import typing as t

import pytest

from agentgrep import GrepArgs, parse_args


class StructuredReducerCase(t.NamedTuple):
    """One invalid structured-output and terminal-reducer combination.

    Attributes
    ----------
    test_id : str
        Stable pytest id.
    output_flag : str
        Structured output flag selected by the caller.
    reducer_flags : tuple[str, ...]
        Terminal reducer flags enabled by the caller.
    expected_reducers : tuple[str, ...]
        Canonical reducer names expected in the diagnostic.
    """

    test_id: str
    output_flag: str
    reducer_flags: tuple[str, ...]
    expected_reducers: tuple[str, ...]


STRUCTURED_REDUCER_CASES = (
    StructuredReducerCase("json-count", "--json", ("-c",), ("--count",)),
    StructuredReducerCase(
        "json-files",
        "--json",
        ("-l",),
        ("--files-with-matches",),
    ),
    StructuredReducerCase(
        "json-invert-count",
        "--json",
        ("-v", "-c"),
        ("--invert-match", "--count"),
    ),
    StructuredReducerCase("ndjson-count", "--ndjson", ("-c",), ("--count",)),
    StructuredReducerCase(
        "ndjson-files",
        "--ndjson",
        ("-l",),
        ("--files-with-matches",),
    ),
    StructuredReducerCase(
        "ndjson-invert-count",
        "--ndjson",
        ("-v", "-c"),
        ("--invert-match", "--count"),
    ),
)


@pytest.mark.parametrize(
    "case",
    STRUCTURED_REDUCER_CASES,
    ids=lambda case: case.test_id,
)
def test_structured_output_rejects_terminal_reducers(
    case: StructuredReducerCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject reducers before they can corrupt or bypass a structured stream."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["grep", case.output_flag, *case.reducer_flags, "needle"])

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert case.output_flag in stderr
    assert "terminal reducers" in stderr
    for reducer in case.expected_reducers:
        assert reducer in stderr


def test_plain_structured_output_stays_available() -> None:
    """Keep structured event streams available when no reducer is selected."""
    json_args = parse_args(["grep", "--json", "needle"])
    ndjson_args = parse_args(["grep", "--ndjson", "needle"])

    assert isinstance(json_args, GrepArgs)
    assert isinstance(ndjson_args, GrepArgs)
    assert json_args.output_mode == "json"
    assert ndjson_args.output_mode == "ndjson"


def test_text_terminal_reducers_stay_available() -> None:
    """Keep each terminal reducer available for ordinary text output."""
    count_args = parse_args(["grep", "-c", "needle"])
    files_args = parse_args(["grep", "-l", "needle"])
    inverted_count_args = parse_args(["grep", "-v", "-c", "needle"])

    assert isinstance(count_args, GrepArgs)
    assert isinstance(files_args, GrepArgs)
    assert isinstance(inverted_count_args, GrepArgs)
    assert count_args.output_mode == "text" and count_args.count_only
    assert files_args.output_mode == "text" and files_args.files_with_matches
    assert inverted_count_args.output_mode == "text"
    assert inverted_count_args.invert_match and inverted_count_args.count_only
