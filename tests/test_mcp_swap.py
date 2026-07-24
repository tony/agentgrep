"""Tests for the ``doctor`` subcommand and ``use-local --env`` in mcp_swap.py.

The swap script lives outside the ``src/`` package, so we load it via the
module's file path and exercise the diagnostics/env-injection behavior against
temporary config fixtures that mirror each CLI's real layout.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import typing as t

import pytest

pytestmark = [pytest.mark.mcp, pytest.mark.setup]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "mcp_swap.py"

_spec = importlib.util.spec_from_file_location("mcp_swap", _SCRIPT)
assert _spec and _spec.loader
mcp_swap = importlib.util.module_from_spec(_spec)
sys.modules["mcp_swap"] = mcp_swap
_spec.loader.exec_module(mcp_swap)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect every config path the script touches into ``tmp_path``."""
    monkeypatch.setattr(
        mcp_swap,
        "CLIS",
        {
            "claude": mcp_swap.CLIInfo(
                name="claude",
                binary="claude",
                config_path=tmp_path / ".claude.json",
                fmt="json",
            ),
            "codex": mcp_swap.CLIInfo(
                name="codex",
                binary="codex",
                config_path=tmp_path / ".codex" / "config.toml",
                fmt="toml",
            ),
            "cursor": mcp_swap.CLIInfo(
                name="cursor",
                binary="cursor-agent",
                config_path=tmp_path / ".cursor" / "mcp.json",
                fmt="json",
            ),
            "gemini": mcp_swap.CLIInfo(
                name="gemini",
                binary="gemini",
                config_path=tmp_path / ".gemini" / "settings.json",
                fmt="json",
            ),
            "grok": mcp_swap.CLIInfo(
                name="grok",
                binary="grok",
                config_path=tmp_path / ".grok" / "config.toml",
                fmt="toml",
            ),
            "agy": mcp_swap.CLIInfo(
                name="agy",
                binary="agy",
                config_path=tmp_path / ".gemini" / "config" / "mcp_config.json",
                fmt="json",
            ),
        },
    )
    state_dir = tmp_path / "state"
    monkeypatch.setattr(mcp_swap, "STATE_DIR", state_dir)
    monkeypatch.setattr(mcp_swap, "STATE_FILE", state_dir / "state.json")
    return tmp_path


@pytest.fixture
def fake_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal pyproject.toml repo for meta resolution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "agentgrep-mcp"\n'
        "[project.scripts]\n"
        'agentgrep-mcp = "agentgrep.mcp:main"\n'
    )
    return repo


def _write_json(path: pathlib.Path, data: dict[str, t.Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _local_entry(repo: pathlib.Path) -> dict[str, t.Any]:
    """Return a local ``uv --directory <repo> run`` JSON entry (use-local shape)."""
    return {
        "command": "uv",
        "args": ["--directory", str(repo.resolve()), "run", "agentgrep-mcp"],
    }


# ---------------------------------------------------------------------------
# use-local --env injection
# ---------------------------------------------------------------------------


def test_use_local_env_flag_injects_into_entry(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """``--env KEY=VALUE`` lands in the written server entry's ``env``.

    The isolation workflow needs to point the server at a scratch index dir
    without a manual post-edit; ``--env`` writes that env at swap time.
    """
    info = mcp_swap.CLIS["cursor"]
    _write_json(info.config_path, {"mcpServers": {}})

    args = mcp_swap.build_parser().parse_args(
        [
            "use-local",
            "--repo",
            str(fake_repo),
            "--cli",
            "cursor",
            "--env",
            "AGENTGREP_DATA_DIR=/scratch/index",
        ]
    )
    assert mcp_swap.cmd_use_local(args) == 0

    entry = json.loads(info.config_path.read_text())["mcpServers"]["agentgrep"]
    assert entry["env"] == {"AGENTGREP_DATA_DIR": "/scratch/index"}


def test_use_local_env_flag_wins_over_preserved_env(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """Explicit ``--env`` overrides a preserved key; other preserved keys survive."""
    info = mcp_swap.CLIS["cursor"]
    _write_json(
        info.config_path,
        {
            "mcpServers": {
                "agentgrep": {
                    "command": "uvx",
                    "args": ["agentgrep-mcp==0.1.0a2"],
                    "env": {"AGENTGREP_DATA_DIR": "/old/index", "KEEP": "me"},
                }
            }
        },
    )

    args = mcp_swap.build_parser().parse_args(
        [
            "use-local",
            "--repo",
            str(fake_repo),
            "--cli",
            "cursor",
            "--env",
            "AGENTGREP_DATA_DIR=/scratch/index",
        ]
    )
    assert mcp_swap.cmd_use_local(args) == 0

    entry = json.loads(info.config_path.read_text())["mcpServers"]["agentgrep"]
    assert entry["env"] == {"AGENTGREP_DATA_DIR": "/scratch/index", "KEEP": "me"}


def test_env_pair_rejects_malformed() -> None:
    """``--env`` without ``=`` is an argparse error, not a silent skip."""
    with pytest.raises(SystemExit):
        mcp_swap.build_parser().parse_args(["use-local", "--env", "NOEQUALS"])


# ---------------------------------------------------------------------------
# naming hint
# ---------------------------------------------------------------------------


def test_naming_hint_points_at_registered_alias(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """Hint names the real slug when the repo uses a non-default server name.

    A bare run would otherwise no-op on a missing entry, so the hint points
    at the name the CLIs were actually registered under.
    """
    _write_json(
        mcp_swap.CLIS["cursor"].config_path,
        {"mcpServers": {"grep": _local_entry(fake_repo)}},
    )
    hint = mcp_swap._naming_hint(fake_repo.resolve(), "agentgrep")
    assert hint is not None
    assert "--server grep" in hint


def test_naming_hint_none_when_derived_name_matches(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """No hint when the repo is already registered under the derived name."""
    _write_json(
        mcp_swap.CLIS["cursor"].config_path,
        {"mcpServers": {"agentgrep": _local_entry(fake_repo)}},
    )
    assert mcp_swap._naming_hint(fake_repo.resolve(), "agentgrep") is None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_name_mismatch_and_auth_env(
    fake_home: pathlib.Path,
    fake_repo: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor surfaces the server-name mismatch and auth-overriding env vars."""
    _write_json(
        mcp_swap.CLIS["cursor"].config_path,
        {"mcpServers": {"grep": _local_entry(fake_repo)}},
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    args = mcp_swap.build_parser().parse_args(["doctor", "--repo", str(fake_repo)])
    assert mcp_swap.cmd_doctor(args) == 0
    out = capsys.readouterr().out
    assert "server name mismatch" in out
    assert "--server grep" in out
    assert "OPENAI_API_KEY" in out and "codex" in out


def test_doctor_flags_missing_backup_and_orphans(
    fake_home: pathlib.Path,
    fake_repo: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Doctor flags a state entry whose backup vanished, and untracked backups."""
    info = mcp_swap.CLIS["cursor"]
    _write_json(info.config_path, {"mcpServers": {"agentgrep": _local_entry(fake_repo)}})
    # A recorded swap whose backup file does not exist -> revert would fail.
    mcp_swap.save_state(
        {
            ("cursor", "user"): mcp_swap.SwapEntry(
                config_path=str(info.config_path),
                backup_path=str(info.config_path) + ".bak.mcp-swap-20200101000000",
                server="agentgrep",
                action="replaced",
                swapped_at="20200101000000",
                seq_no=0,
            )
        }
    )
    # An orphaned backup on disk not referenced by state.
    orphan = info.config_path.parent / (info.config_path.name + ".bak.mcp-swap-20190101000000")
    orphan.write_text("stale")

    args = mcp_swap.build_parser().parse_args(["doctor", "--repo", str(fake_repo)])
    assert mcp_swap.cmd_doctor(args) == 0
    out = capsys.readouterr().out
    assert "BACKUP MISSING" in out
    assert "orphaned backups" in out


def test_orphaned_backups_matches_swap_pattern(
    fake_home: pathlib.Path,
) -> None:
    """``_orphaned_backups`` finds swap backups and ignores the live config."""
    info = mcp_swap.CLIS["cursor"]
    info.config_path.parent.mkdir(parents=True, exist_ok=True)
    info.config_path.write_text("{}\n")
    b1 = info.config_path.parent / (info.config_path.name + ".bak.mcp-swap-20260101000000")
    b1.write_text("x")
    found = mcp_swap._orphaned_backups(info.config_path)
    assert b1 in found
    assert info.config_path not in found


def test_use_local_env_written_on_already_local_entry(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """``--env`` still writes when the entry already points at this repo.

    Regression: the already-local short-circuit ``continue``d before the env
    merge, so ``--env`` was silently dropped whenever the config already
    pointed local. The guard now only short-circuits when the requested env is
    already satisfied.
    """
    info = mcp_swap.CLIS["cursor"]
    _write_json(info.config_path, {"mcpServers": {"agentgrep": _local_entry(fake_repo)}})

    args = mcp_swap.build_parser().parse_args(
        [
            "use-local",
            "--repo",
            str(fake_repo),
            "--cli",
            "cursor",
            "--env",
            "AGENTGREP_DATA_DIR=/scratch/index",
        ]
    )
    assert mcp_swap.cmd_use_local(args) == 0

    entry = json.loads(info.config_path.read_text())["mcpServers"]["agentgrep"]
    assert entry.get("env") == {"AGENTGREP_DATA_DIR": "/scratch/index"}


def test_use_local_already_local_still_noop_when_env_matches(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """The short-circuit still fires when the requested env is already present."""
    info = mcp_swap.CLIS["cursor"]
    spec = _local_entry(fake_repo)
    spec["env"] = {"AGENTGREP_DATA_DIR": "/scratch/index"}
    _write_json(info.config_path, {"mcpServers": {"agentgrep": spec}})
    before = info.config_path.read_bytes()

    args = mcp_swap.build_parser().parse_args(
        [
            "use-local",
            "--repo",
            str(fake_repo),
            "--cli",
            "cursor",
            "--env",
            "AGENTGREP_DATA_DIR=/scratch/index",
        ]
    )
    assert mcp_swap.cmd_use_local(args) == 0
    assert info.config_path.read_bytes() == before


def test_naming_hint_none_when_repo_also_registered_under_derived(
    fake_home: pathlib.Path, fake_repo: pathlib.Path
) -> None:
    """No hint when the derived name points here, even if another name does too."""
    _write_json(
        mcp_swap.CLIS["cursor"].config_path,
        {"mcpServers": {"agentgrep": _local_entry(fake_repo), "grep": _local_entry(fake_repo)}},
    )
    assert mcp_swap._naming_hint(fake_repo.resolve(), "agentgrep") is None
