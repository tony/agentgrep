"""Contracts for the storage observation manifests: the docs reader and the observer."""

from __future__ import annotations

import collections
import importlib.util
import json
import os
import pathlib
import sys
import typing as t

import pytest

from agentgrep.store_catalog import CATALOG
from docs._ext.storages._observations import (
    MANIFEST_VERSION,
    AgentObservation,
    ObservationIndex,
    detect_version_drift,
    load_observation_index,
)

if t.TYPE_CHECKING:
    import types

_MANIFEST = """\
manifest_version = {version}

[agent]
id = "grok"
app_version = "{app_version}"

[observation]
observed_at = {observed_at}

[[store]]
id = "grok.sessions"
discriminator = "type"

[store.record_keys]
user = ["content", "type"]
"""


def _write(root: pathlib.Path, name: str, **fields: object) -> pathlib.Path:
    """Write one manifest, defaulting every field to a valid value."""
    payload = {"version": MANIFEST_VERSION, "app_version": "1.0.0", "observed_at": "2026-08-08"}
    payload.update(fields)
    path = root / "grok" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MANIFEST.format(**payload), encoding="utf-8")
    return path


def _load_observer() -> types.ModuleType:
    """Import ``scripts/observe_stores.py``, a standalone script outside the package."""
    spec = importlib.util.spec_from_file_location("_observe", "scripts/observe_stores.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    sys.modules["_observe"] = script
    spec.loader.exec_module(script)
    return script


def test_missing_root_is_silence(tmp_path: pathlib.Path) -> None:
    """An absent tree yields an empty index and no problems."""
    index = load_observation_index(tmp_path / "nope")
    assert index.agents == {}
    assert index.problems == ()


def test_newest_manifest_wins_by_date(tmp_path: pathlib.Path) -> None:
    """Selection is by observed date, not by filename order."""
    _write(tmp_path, "9.0.0.toml", app_version="9.0.0", observed_at="2026-01-01")
    _write(tmp_path, "1.0.0.toml", app_version="1.0.0", observed_at="2026-08-08")
    assert load_observation_index(tmp_path).agents["grok"].app_version == "1.0.0"


def test_future_manifest_version_is_refused(tmp_path: pathlib.Path) -> None:
    """A newer schema is skipped rather than parsed under old assumptions."""
    _write(tmp_path, "1.0.0.toml", version=MANIFEST_VERSION + 1)
    index = load_observation_index(tmp_path)
    assert index.agents == {}
    assert any("manifest_version" in problem for problem in index.problems)


@pytest.mark.parametrize(
    ("name", "body"),
    [("bad.toml", "this is not toml ["), ("empty.toml", "")],
    ids=["malformed", "empty"],
)
def test_unreadable_manifest_is_reported(tmp_path: pathlib.Path, name: str, body: str) -> None:
    """A damaged manifest produces a problem message, never a silent gap."""
    path = tmp_path / "grok" / name
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    assert load_observation_index(tmp_path).problems


def test_paths_lists_every_manifest(tmp_path: pathlib.Path) -> None:
    """All manifests are tracked, not just selected ones, so pages can depend on them."""
    _write(tmp_path, "1.0.0.toml")
    _write(tmp_path, "0.9.0.toml", app_version="0.9.0", observed_at="2026-01-01")
    assert len(load_observation_index(tmp_path).paths) == 2


def test_unknown_app_version_is_not_drift() -> None:
    """Three backends expose no version; warning about them is unfixable noise."""
    index = ObservationIndex({"x": AgentObservation("x", "unknown", "2026-08-08", {})}, ())
    assert detect_version_drift(index, {"x": ("Whatever 1.2.3",)}) == ()


def test_drift_message_names_both_repairs() -> None:
    """Either side may be stale, so the warning cannot prescribe only one fix."""
    index = ObservationIndex({"grok": AgentObservation("grok", "1.1.0", "2026-08-08", {})}, ())
    (drift,) = detect_version_drift(index, {"grok": ("grok 1.0.0",)})
    assert "observe --agent grok" in drift.message
    assert "store_catalog/grok.py" in drift.message


def test_script_and_extension_pick_the_same_manifest(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observer and the docs reader must agree on which manifest is newest.

    Two implementations of one selection rule drifted once already: the script
    globbed unsorted, so a same-day tie fell to filesystem order.
    """
    script = _load_observer()
    monkeypatch.setattr(script, "OBSERVATIONS_ROOT", tmp_path)

    for name, version in (
        ("1.0.0.toml", "1.0.0"),
        ("9.0.0.toml", "9.0.0"),
        ("2.0.0.toml", "2.0.0"),
    ):
        _write(tmp_path, name, app_version=version)

    picked = script.newest_manifest("grok")
    assert picked is not None
    observed = load_observation_index(tmp_path).agents["grok"]
    assert picked.stem == observed.app_version


class UnclaimedCase(t.NamedTuple):
    """One path under a synthetic agent home and whether the observer lists it."""

    test_id: str
    path: str
    listed: bool


UNCLAIMED_CASES: tuple[UnclaimedCase, ...] = (
    UnclaimedCase("unclaimed-directory", ".agent/plugins", listed=True),
    UnclaimedCase("inside-unclaimed-directory", ".agent/plugins/someone-1a2b3c4d", listed=False),
    UnclaimedCase("inside-claimed-directory", ".agent/sessions/new.log", listed=True),
)


@pytest.mark.parametrize(
    list(UnclaimedCase._fields),
    UNCLAIMED_CASES,
    ids=[case.test_id for case in UNCLAIMED_CASES],
)
def test_unclaimed_listing_stops_at_an_unclaimed_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    path: str,
    listed: bool,
) -> None:
    """Names inside an unclaimed directory identify instances, not coverage gaps."""
    (tmp_path / ".agent" / "plugins" / "someone-1a2b3c4d").mkdir(parents=True)
    (tmp_path / ".agent" / "sessions").mkdir()
    (tmp_path / ".agent" / "sessions" / "new.log").touch()
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    script = _load_observer()
    probe = script.AgentProbe("agent", homes=(".agent",))
    entries = script._unclaimed_entries(probe, ["${HOME}/.agent/sessions.json"])
    reported = {entry["path_pattern"] for entry in entries}
    assert (f"${{HOME}}/{path}" in reported) is listed


class EncodedHomeCase(t.NamedTuple):
    """One way an agent encodes a project path under the home into a directory name."""

    test_id: str
    template: str


ENCODED_HOME_CASES: tuple[EncodedHomeCase, ...] = (
    EncodedHomeCase("dashed", "{dashed}-work-repo"),
    EncodedHomeCase("leading-dash", "-{dashed}-work-repo"),
    EncodedHomeCase("url-encoded", "{url}%2Fwork%2Frepo"),
)


@pytest.mark.parametrize(
    list(EncodedHomeCase._fields),
    ENCODED_HOME_CASES,
    ids=[case.test_id for case in ENCODED_HOME_CASES],
)
def test_unclaimed_listing_hides_encoded_home_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    template: str,
) -> None:
    """A project directory named after its encoded path must not name the user.

    A row that mentions ``projects`` by name makes the walk descend into it, and
    the project directories there encode the home path, username included.
    """
    name = template.format(
        dashed=str(tmp_path).strip("/").replace("/", "-"),
        url="%2F".join(str(tmp_path).split("/")),
    )
    (tmp_path / ".agent" / "projects" / name).mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    script = _load_observer()
    probe = script.AgentProbe("agent", homes=(".agent",))
    entries = script._unclaimed_entries(probe, ["${HOME}/.agent/projects/index.json"])
    reported = {str(entry["path_pattern"]) for entry in entries}
    assert "${HOME}/.agent/projects/<encoded_project>" in reported
    assert not any(name in pattern for pattern in reported)


def test_version_file_glob_reads_the_newest_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IDE keeps one remote-server build per commit; the newest one is installed."""
    for build, version, mtime in (("aaa", "2.0.0", 2_000_000_000), ("bbb", "1.0.0", 1_000_000_000)):
        product = tmp_path / ".ide-server" / "bin" / build / "product.json"
        product.parent.mkdir(parents=True)
        product.write_text(json.dumps({"version": version}), encoding="utf-8")
        os.utime(product, (mtime, mtime))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    script = _load_observer()
    probe = script.AgentProbe("ide", version_file=".ide-server/bin/*/product.json")
    assert script._read_installed_version(probe) == ("2.0.0", "version_file")


def test_antigravity_ide_reads_its_own_version(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Antigravity's ``version`` is the VS Code release it forks; its own is ``ideVersion``."""
    product = tmp_path / ".antigravity-server" / "bin" / "abc" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.104.0", "ideVersion": "1.13.3"}), encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    script = _load_observer()
    (probe,) = (probe for probe in script.AGENT_PROBES if probe.agent == "antigravity-ide")
    assert script._read_installed_version(probe) == ("1.13.3", "version_file")


def test_windows_launcher_is_read_not_run(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under WSL, ``code --version`` downloads a server into the home before reading its flags."""
    install = tmp_path / "mnt" / "c" / "VS Code"
    launcher = install / "bin" / "code"
    launcher.parent.mkdir(parents=True)
    ran = tmp_path / "launcher-ran"
    launcher.write_text(f"#!/bin/sh\ntouch {ran}\necho 9.9.9\n", encoding="utf-8")
    launcher.chmod(0o755)
    product = install / "abc1234567" / "resources" / "app" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.143.0"}), encoding="utf-8")
    monkeypatch.setenv("PATH", str(launcher.parent))
    script = _load_observer()
    monkeypatch.setattr(script, "WINDOWS_MOUNT", tmp_path / "mnt", raising=False)
    probe = script.AgentProbe("vscode", ("code", "--version"))
    assert script._read_installed_version(probe) == ("1.143.0", "version_file")
    assert not ran.exists()


def test_check_reports_unclaimed_path_changes() -> None:
    """A client can add a directory without a version bump; ``check`` must still say so."""
    script = _load_observer()
    previous = {
        "agent": {"app_version": "1.0.0"},
        "unclaimed": [{"path_pattern": "${HOME}/.agent/old", "kind": "dir"}],
    }
    current = {
        "agent": {"app_version": "1.0.0"},
        "unclaimed": [{"path_pattern": "${HOME}/.agent/new", "kind": "dir"}],
    }
    assert script.diff_manifest(previous, current) == [
        "${HOME}/.agent/new: newly unclaimed",
        "${HOME}/.agent/old: no longer unclaimed",
    ]


class SourceDropCase(t.NamedTuple):
    """A store's source count at two observations and whether ``check`` reports it."""

    test_id: str
    previous: int
    current: int
    reported: bool


SOURCE_DROP_CASES: tuple[SourceDropCase, ...] = (
    SourceDropCase("most-sources-gone", 2782, 400, reported=True),
    SourceDropCase("old-sessions-pruned", 3470, 3373, reported=False),
)


@pytest.mark.parametrize(
    list(SourceDropCase._fields),
    SOURCE_DROP_CASES,
    ids=[case.test_id for case in SOURCE_DROP_CASES],
)
def test_check_reports_a_store_that_lost_most_of_its_sources(
    test_id: str,
    previous: int,
    current: int,
    reported: bool,
) -> None:
    """Files that move or are recompressed drop out of search with no schema change.

    Codex began compressing older rollouts to ``.jsonl.zst``, which discovery
    does not glob, and only the source count showed it.
    """
    script = _load_observer()

    def manifest(count: int) -> dict[str, object]:
        return {"store": [{"id": "agent.sessions", "source_count": count}]}

    lines = script.diff_manifest(manifest(previous), manifest(current))
    assert any(line.startswith("agent.sessions: source_count") for line in lines) is reported


def test_catalogue_rows_share_their_agent_observation_date() -> None:
    """A row with its own ``observed_at`` claims a check its module never made.

    Restamping moves one constant per agent; a row that pins a literal date
    keeps it, and then claims the module's version on a day it was not checked.
    """
    dates: dict[str, set[object]] = collections.defaultdict(set)
    for store in CATALOG.stores:
        dates[str(store.agent)].add(store.observed_at)
    assert {agent: found for agent, found in dates.items() if len(found) > 1} == {}
