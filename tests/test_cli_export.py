"""Functional tests for the dedicated headless export command."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import typing as t

import pytest

import agentgrep
import agentgrep.cli.render as cli_render


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


def test_export_stdout_is_deterministic_across_reruns(export_home: pathlib.Path) -> None:
    """Repeated reads of unchanged stores produce byte-identical stdout."""
    first = _run_export_cli(export_home, "bliss", "--agent", "codex")
    second = _run_export_cli(export_home, "bliss", "--agent", "codex")

    assert first.returncode == second.returncode == 0
    assert first.stdout.encode() == second.stdout.encode()
    assert first.stderr == second.stderr == ""


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
