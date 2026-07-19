"""Functional tests for the dedicated headless export command."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import itertools
import json
import os
import pathlib
import subprocess
import sys
import typing as t

import pytest

import agentgrep
import agentgrep.cli.render as cli_render


def load_agentgrep_module() -> object:
    """Return the installed package for facade-compatibility assertions."""
    return agentgrep


class ExportParserCase(t.NamedTuple):
    """One export parser permutation."""

    export_format: str
    output_args: tuple[str, ...]
    expected_output: str
    include_bodies: bool
    scope: str


EXPORT_PARSER_CASES: tuple[ExportParserCase, ...] = tuple(
    ExportParserCase(
        export_format=export_format,
        output_args=output_args,
        expected_output=expected_output,
        include_bodies=include_bodies,
        scope=scope,
    )
    for export_format, (output_args, expected_output), include_bodies, scope in itertools.product(
        ("ndjson", "markdown"),
        (
            ((), "-"),
            (("-o", "-"), "-"),
            (("--output", "export.out"), "export.out"),
        ),
        (True, False),
        ("prompts", "conversations", "all"),
    )
)


@pytest.mark.parametrize(
    "case",
    EXPORT_PARSER_CASES,
    ids=(
        f"{case.export_format}-"
        f"{'default' if not case.output_args else case.output_args[0].lstrip('-') or 'stdout'}-"
        f"{'bodies' if case.include_bodies else 'no-bodies'}-{case.scope}"
        for case in EXPORT_PARSER_CASES
    ),
)
def test_parse_export_covers_format_sink_bodies_and_scope_matrix(
    case: ExportParserCase,
) -> None:
    """Every documented export parser permutation yields typed arguments."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    argv = [
        "export",
        "needle",
        "--format",
        case.export_format,
        *case.output_args,
        "--scope",
        case.scope,
    ]
    if not case.include_bodies:
        argv.append("--no-bodies")

    parsed = agentgrep.parse_args(argv)

    assert isinstance(parsed, agentgrep.ExportArgs)
    assert parsed.format == case.export_format
    assert parsed.output == case.expected_output
    assert parsed.include_bodies is case.include_bodies
    assert parsed.scope == case.scope


def test_parse_export_defaults_have_exact_typed_contract() -> None:
    """Export defaults to bounded body-inclusive NDJSON on stdout."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    parsed = agentgrep.parse_args(["export", "Needle"])

    assert isinstance(parsed, agentgrep.ExportArgs)
    assert dataclasses.asdict(parsed) == {
        "terms": ("Needle",),
        "agents": agentgrep.AGENT_CHOICES,
        "scope": "prompts",
        "case_sensitive": False,
        "limit": 100,
        "format": "ndjson",
        "output": "-",
        "force": False,
        "include_bodies": True,
        "compiled": None,
        "raw_query": "Needle",
    }


def test_parse_export_supports_agent_aliases_and_case_sensitive_search() -> None:
    """Export shares search's agent selection and case controls."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    all_agents = agentgrep.parse_args(["export", "needle", "--agent", "all"])
    selected_agents = agentgrep.parse_args(
        [
            "export",
            "needle",
            "--agent",
            "codex",
            "--agent",
            "claude",
            "--case-sensitive",
        ],
    )

    assert isinstance(all_agents, agentgrep.ExportArgs)
    assert all_agents.agents == agentgrep.AGENT_CHOICES
    assert isinstance(selected_agents, agentgrep.ExportArgs)
    assert selected_agents.agents == ("codex", "claude")
    assert selected_agents.case_sensitive is True


def test_parse_export_reuses_compiled_query_semantics() -> None:
    """Field predicates compile while residual terms retain search behavior."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    parsed = agentgrep.parse_args(["export", "scope:conversations", "bliss"])

    assert isinstance(parsed, agentgrep.ExportArgs)
    assert parsed.terms == ("bliss",)
    assert parsed.scope == "all"
    assert parsed.compiled is not None
    assert parsed.raw_query == "scope:conversations bliss"


@pytest.mark.parametrize("limit", ["-1", "0", "1001"])
def test_parse_export_rejects_limits_outside_closed_range(
    limit: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The export cap is constrained to 1 through 1000 inclusive."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    with pytest.raises(SystemExit) as exc_info:
        agentgrep.parse_args(["export", "needle", "--limit", limit])

    assert exc_info.value.code == 2
    assert "--limit must be between 1 and 1000" in capsys.readouterr().err


def test_parse_export_accepts_limit_range_endpoints() -> None:
    """Both documented export limit endpoints are accepted."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    low = agentgrep.parse_args(["export", "needle", "--limit", "1"])
    high = agentgrep.parse_args(["export", "needle", "--limit", "1000"])

    assert isinstance(low, agentgrep.ExportArgs)
    assert low.limit == 1
    assert isinstance(high, agentgrep.ExportArgs)
    assert high.limit == 1000


@pytest.mark.parametrize("output_args", [(), ("-o", "-")])
def test_parse_export_rejects_force_for_stdout(
    output_args: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force is meaningful only for an explicit file destination."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    with pytest.raises(SystemExit) as exc_info:
        agentgrep.parse_args(["export", "needle", *output_args, "--force"])

    assert exc_info.value.code == 2
    assert "--force requires a file output" in capsys.readouterr().err


def test_parse_export_allows_force_for_file_output() -> None:
    """Explicit file output may opt into replacement."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    parsed = agentgrep.parse_args(["export", "needle", "-o", "export.ndjson", "--force"])

    assert isinstance(parsed, agentgrep.ExportArgs)
    assert parsed.output == "export.ndjson"
    assert parsed.force is True


def test_bare_export_prints_subcommand_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare export explains the command instead of scanning every store."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    parsed = agentgrep.parse_args(["export"])

    assert parsed is None
    output = capsys.readouterr().out
    assert "usage: agentgrep export" in output
    assert "--no-bodies" in output


@pytest.mark.slow
def test_export_help_keeps_persistence_module_off_cold_path() -> None:
    """Root and export help do not import renderer or TUI persistence modules."""
    runner = """
import agentgrep
import contextlib
import io
import sys

root_output = io.StringIO()
with contextlib.redirect_stdout(root_output):
    assert agentgrep.main([]) == 0
assert "export" in root_output.getvalue()
assert "agentgrep.record_export" not in sys.modules
assert "agentgrep.ui._export_preferences" not in sys.modules

export_output = io.StringIO()
with contextlib.redirect_stdout(export_output):
    try:
        agentgrep.main(["export", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
assert "usage: agentgrep export" in export_output.getvalue()
assert "agentgrep.record_export" not in sys.modules
assert "agentgrep.ui._export_preferences" not in sys.modules
"""

    completed = subprocess.run(
        [sys.executable, "-c", runner],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_main_dispatches_export_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility facade routes typed export args to the thin command."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    args = agentgrep.ExportArgs(
        terms=("bliss",),
        agents=("codex",),
        scope="prompts",
        case_sensitive=False,
        limit=100,
        format="ndjson",
        output="-",
        force=False,
        include_bodies=True,
        compiled=None,
        raw_query="bliss",
    )
    calls: list[object] = []

    def parse_args(argv: cabc.Sequence[str] | None = None) -> object:
        assert argv == ["export", "bliss"]
        return args

    def run_export_command(received: object) -> int:
        calls.append(received)
        return 7

    monkeypatch.setattr(agentgrep, "parse_args", parse_args)
    monkeypatch.setattr(agentgrep, "run_export_command", run_export_command)

    assert agentgrep.main(["export", "bliss"]) == 7
    assert calls == [args]


def _write_jsonl(path: pathlib.Path, rows: list[object]) -> None:
    """Write one fixture store as newline-delimited JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )


@pytest.fixture
def export_home(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a home directory containing deterministic Codex prompt rows."""
    home = tmp_path / "home"
    _write_jsonl(
        home / ".codex" / "history.jsonl",
        [
            {
                "session_id": "session-b",
                "ts": 1_700_000_002,
                "text": "bliss second prompt",
            },
            {
                "session_id": "session-a",
                "ts": 1_700_000_001,
                "text": "bliss first prompt",
            },
            {
                "session_id": "session-c",
                "ts": 1_700_000_003,
                "text": "unrelated prompt",
            },
        ],
    )
    return home


def _run_export_cli(
    home: pathlib.Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run the installed export command against one isolated fixture home."""
    return subprocess.run(
        [sys.executable, "-m", "agentgrep", "export", *args],
        capture_output=True,
        text=True,
        check=False,
        env=_export_env(home),
    )


def _export_env(home: pathlib.Path) -> dict[str, str]:
    """Return an isolated subprocess environment for one fixture home."""
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "NO_COLOR": "1",
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        },
    )
    return env


@pytest.mark.parametrize("output_args", [(), ("-o", "-")])
@pytest.mark.slow
def test_export_ndjson_writes_default_and_explicit_stdout(
    export_home: pathlib.Path,
    output_args: tuple[str, ...],
) -> None:
    """NDJSON stdout emits one canonical body-inclusive row per match."""
    completed = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        *output_args,
    )

    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [row["text"] for row in rows] == [
        "bliss first prompt",
        "bliss second prompt",
    ]
    assert {row["agent"] for row in rows} == {"codex"}
    assert completed.stderr == ""


@pytest.mark.slow
def test_export_ndjson_no_bodies_omits_text_field(export_home: pathlib.Path) -> None:
    """The body opt-out removes text without changing selected records."""
    completed = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "--no-bodies",
    )

    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(rows) == 2
    assert all("text" not in row for row in rows)
    assert completed.stderr == ""


@pytest.mark.slow
def test_export_markdown_writes_stdout(export_home: pathlib.Path) -> None:
    """Markdown stdout uses the approved deterministic records renderer."""
    completed = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "--format",
        "markdown",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("# agentgrep record export\n")
    assert "- Record count: 2" in completed.stdout
    assert "bliss first prompt" in completed.stdout
    assert "bliss second prompt" in completed.stdout
    assert completed.stderr == ""


@pytest.mark.slow
def test_export_writes_explicit_file_without_stdout(
    export_home: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Explicit output delegates the exact artifact to the safe writer."""
    destination = tmp_path / "records.ndjson"

    completed = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "-o",
        str(destination),
    )

    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert [row["text"] for row in rows] == [
        "bliss first prompt",
        "bliss second prompt",
    ]
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.mark.slow
def test_export_file_refusal_and_explicit_force(
    export_home: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Existing file output is preserved unless replacement is explicit."""
    destination = tmp_path / "records.ndjson"
    _ = destination.write_text("keep me\n", encoding="utf-8")

    refused = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "-o",
        str(destination),
    )

    assert refused.returncode == 2
    assert destination.read_text(encoding="utf-8") == "keep me\n"
    assert refused.stdout == ""
    assert "already exists" in refused.stderr
    assert str(destination) not in refused.stderr
    assert str(export_home) not in refused.stderr

    replaced = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "-o",
        str(destination),
        "--force",
    )

    assert replaced.returncode == 0, replaced.stderr
    assert "bliss first prompt" in destination.read_text(encoding="utf-8")
    assert replaced.stdout == ""
    assert replaced.stderr == ""


@pytest.mark.slow
def test_export_protects_every_selected_record_source_path(
    export_home: pathlib.Path,
) -> None:
    """A later selected source cannot be replaced even with ``--force``."""
    source = export_home / ".codex" / "sessions" / "rollout-2025-04-21-selected-source.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {
            "session": {
                "id": "selected-source-session",
                "timestamp": "2025-04-21T00:00:00Z",
            },
            "items": [
                {
                    "id": "selected-source-item",
                    "role": "user",
                    "type": "message",
                    "content": "bliss selected source prompt",
                },
            ],
        },
    )
    _ = source.write_text(original, encoding="utf-8")

    completed = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "--scope",
        "all",
        "-o",
        str(source),
        "--force",
    )

    assert completed.returncode == 2
    assert source.read_text(encoding="utf-8") == original
    assert completed.stdout == ""
    assert "protected source" in completed.stderr
    assert str(source) not in completed.stderr
    assert str(export_home) not in completed.stderr
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "original"),
    [
        (
            pathlib.Path(
                ".codex/sessions/rollout-2025-04-21-unmatched-source.json",
            ),
            json.dumps(
                {
                    "session": {
                        "id": "unmatched-source-session",
                        "timestamp": "2025-04-21T00:00:00Z",
                    },
                    "items": [
                        {
                            "id": "unmatched-source-item",
                            "role": "user",
                            "type": "message",
                            "content": "unrelated source prompt",
                        },
                    ],
                },
            ),
        ),
        (
            pathlib.Path(".claude/settings.json"),
            '{"theme":"dark"}\n',
        ),
    ],
    ids=("selected-agent", "outside-agent-non-default"),
)
@pytest.mark.slow
def test_export_force_protects_unmatched_discovered_source(
    export_home: pathlib.Path,
    relative_path: pathlib.Path,
    original: str,
) -> None:
    """Force cannot replace unmatched inventory inside or outside selection."""
    matched_source = export_home / ".codex" / "history.jsonl"
    matched_original = matched_source.read_bytes()
    unmatched_source = export_home / relative_path
    unmatched_source.parent.mkdir(parents=True, exist_ok=True)
    _ = unmatched_source.write_text(original, encoding="utf-8")

    completed = _run_export_cli(
        export_home,
        "bliss",
        "--agent",
        "codex",
        "-o",
        str(unmatched_source),
        "--force",
    )

    assert completed.returncode == 2
    assert matched_source.read_bytes() == matched_original
    assert unmatched_source.read_text(encoding="utf-8") == original
    assert completed.stdout == ""
    assert "protected source" in completed.stderr
    assert str(unmatched_source) not in completed.stderr
    assert str(export_home) not in completed.stderr
    assert "Traceback" not in completed.stderr


@pytest.mark.slow
def test_export_zero_matches_uses_search_exit_status(export_home: pathlib.Path) -> None:
    """An empty NDJSON selection emits no rows and exits with no-match status."""
    completed = _run_export_cli(
        export_home,
        "absent-export-query",
        "--agent",
        "codex",
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.mark.slow
def test_export_stdout_is_deterministic_across_reruns(export_home: pathlib.Path) -> None:
    """Repeated reads of unchanged stores produce byte-identical stdout."""
    first = _run_export_cli(export_home, "bliss", "--agent", "codex")
    second = _run_export_cli(export_home, "bliss", "--agent", "codex")

    assert first.returncode == second.returncode == 0
    assert first.stdout.encode() == second.stdout.encode()
    assert first.stderr == second.stderr == ""


@pytest.mark.slow
def test_export_invalid_markdown_text_is_path_free(
    tmp_path: pathlib.Path,
) -> None:
    """Unencodable Markdown content reports only the typed export failure."""
    home = tmp_path / "private-home"
    _write_jsonl(
        home / ".codex" / "history.jsonl",
        [
            {
                "session_id": "invalid-markdown",
                "ts": 1_700_000_001,
                "text": "bliss invalid \ud800 markdown",
            },
        ],
    )

    completed = _run_export_cli(
        home,
        "bliss",
        "--agent",
        "codex",
        "--format",
        "markdown",
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "not valid UTF-8" in completed.stderr
    assert str(home) not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_export_search_io_failure_is_path_free(
    export_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Store I/O errors are sanitized when permissions cannot be tested portably."""
    private_path = export_home / ".codex" / "history.jsonl"

    def fail_search(*_args: object, **_kwargs: object) -> t.NoReturn:
        message = f"could not read {private_path}"
        raise OSError(message)

    monkeypatch.setattr(cli_render, "run_search_query", fail_search)

    result = agentgrep.run_export_command(_parsed_export_args())

    assert result == 2
    error = capsys.readouterr().err
    assert "export source could not be read" in error
    assert str(private_path) not in error
    assert str(export_home) not in error
    assert "Traceback" not in error


def test_export_protection_discovery_io_failure_is_path_free(
    export_home: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inventory discovery errors cannot disclose source or destination paths."""
    monkeypatch.setenv("HOME", str(export_home))
    monkeypatch.setenv("CODEX_HOME", str(export_home / ".codex"))
    private_path = export_home / ".claude" / "private-store.json"
    destination = tmp_path / "records.ndjson"

    def fail_discovery(*_args: object, **_kwargs: object) -> t.NoReturn:
        message = f"could not inspect {private_path}"
        raise OSError(message)

    monkeypatch.setattr(cli_render, "discover_sources", fail_discovery, raising=False)
    parsed = agentgrep.parse_args(
        [
            "export",
            "bliss",
            "--agent",
            "codex",
            "-o",
            str(destination),
        ],
    )
    assert isinstance(parsed, agentgrep.ExportArgs)

    result = agentgrep.run_export_command(parsed)

    assert result == 2
    assert not destination.exists()
    error = capsys.readouterr().err
    assert "export source could not be read" in error
    assert str(private_path) not in error
    assert str(destination) not in error
    assert str(export_home) not in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("phase", "expected_error"),
    [
        ("search", "export source could not be read"),
        ("discovery", "export source could not be read"),
        ("render", "export artifact could not be rendered"),
        ("output", "export output could not be written"),
    ],
)
def test_export_unexpected_failures_are_path_and_body_free(
    export_home: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    expected_error: str,
) -> None:
    """Unexpected phase failures expose neither store paths nor record bodies."""
    import agentgrep.record_export as record_export

    monkeypatch.setenv("HOME", str(export_home))
    monkeypatch.setenv("CODEX_HOME", str(export_home / ".codex"))
    private_path = export_home / ".codex" / "private-store.jsonl"
    private_body = "private prompt body"
    destination = tmp_path / "private-destination.ndjson"

    def fail(*_args: object, **_kwargs: object) -> t.NoReturn:
        message = f"failed near {private_path}: {private_body}"
        raise RuntimeError(message)

    if phase == "search":
        monkeypatch.setattr(cli_render, "run_search_query", fail)
    elif phase == "discovery":
        monkeypatch.setattr(cli_render, "discover_sources", fail)
    elif phase == "render":
        monkeypatch.setattr(record_export, "render_export", fail)
    else:
        monkeypatch.setattr(record_export, "write_export", fail)

    argv = ["export", "bliss", "--agent", "codex"]
    if phase in {"discovery", "output"}:
        argv.extend(("-o", str(destination)))
    parsed = agentgrep.parse_args(argv)
    assert isinstance(parsed, agentgrep.ExportArgs)

    result = agentgrep.run_export_command(parsed)

    assert result == 2
    error = capsys.readouterr().err
    assert expected_error in error
    assert str(private_path) not in error
    assert str(destination) not in error
    assert private_body not in error
    assert "Traceback" not in error


def test_export_stdout_skips_protection_discovery(
    export_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdout keeps the search-only discovery cost of the existing path."""
    monkeypatch.setenv("HOME", str(export_home))
    monkeypatch.setenv("CODEX_HOME", str(export_home / ".codex"))

    def unexpected_discovery(*_args: object, **_kwargs: object) -> t.NoReturn:
        pytest.fail("stdout export performed protection discovery")

    monkeypatch.setattr(
        cli_render,
        "discover_sources",
        unexpected_discovery,
        raising=False,
    )

    result = agentgrep.run_export_command(_parsed_export_args())

    assert result == 0


class _ShortWriteBuffer:
    """Binary stream that accepts only a bounded prefix per write."""

    def __init__(self) -> None:
        self.payload = bytearray()
        self.calls = 0

    def write(self, payload: bytes) -> int:
        """Accept at most seven bytes from one write request."""
        size = min(7, len(payload))
        self.payload.extend(payload[:size])
        self.calls += 1
        return size


class _BrokenWriteBuffer:
    """Binary stream that fails before accepting output."""

    def write(self, _payload: bytes) -> int:
        """Raise the pipe failure surfaced by a closed downstream reader."""
        raise BrokenPipeError


class _BinaryStdout:
    """Minimal text facade exposing a binary stdout buffer."""

    def __init__(self, buffer: _ShortWriteBuffer | _BrokenWriteBuffer) -> None:
        self.buffer = buffer
        self.flush_calls = 0

    def flush(self) -> None:
        """Record a command-level stdout flush."""
        self.flush_calls += 1


def _parsed_export_args() -> agentgrep.ExportArgs:
    """Build real typed arguments for direct command execution tests."""
    parsed = agentgrep.parse_args(["export", "bliss", "--agent", "codex"])
    assert isinstance(parsed, agentgrep.ExportArgs)
    return parsed


def test_export_stdout_retries_positive_short_writes_and_flushes(
    export_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdout receives every artifact byte before one final flush."""
    monkeypatch.setenv("HOME", str(export_home))
    monkeypatch.setenv("CODEX_HOME", str(export_home / ".codex"))
    buffer = _ShortWriteBuffer()
    stdout = _BinaryStdout(buffer)
    monkeypatch.setattr(sys, "stdout", t.cast("t.Any", stdout))

    result = agentgrep.run_export_command(_parsed_export_args())

    rows = [json.loads(line) for line in buffer.payload.decode().splitlines()]
    assert result == 0
    assert [row["text"] for row in rows] == [
        "bliss first prompt",
        "bliss second prompt",
    ]
    assert buffer.calls > 1
    assert stdout.flush_calls == 1


def test_export_broken_pipe_is_path_free(
    export_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A closed stdout pipe returns a clean diagnostic without traceback."""
    monkeypatch.setenv("HOME", str(export_home))
    monkeypatch.setenv("CODEX_HOME", str(export_home / ".codex"))
    stdout = _BinaryStdout(_BrokenWriteBuffer())
    monkeypatch.setattr(sys, "stdout", t.cast("t.Any", stdout))

    result = agentgrep.run_export_command(_parsed_export_args())

    assert result == 2
    error = capsys.readouterr().err
    assert "export output could not be written" in error
    assert str(export_home) not in error
    assert "Traceback" not in error


@pytest.mark.slow
def test_export_real_broken_pipe_exits_without_shutdown_traceback(
    export_home: pathlib.Path,
) -> None:
    """A closed OS pipe stays handled through interpreter shutdown."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentgrep",
            "export",
            "bliss",
            "--agent",
            "codex",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_export_env(export_home),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdout.close()
    error = process.stderr.read()
    returncode = process.wait()

    assert returncode == 2
    assert "export output could not be written" in error
    assert str(export_home) not in error
    assert "Traceback" not in error
    assert "Exception ignored" not in error
