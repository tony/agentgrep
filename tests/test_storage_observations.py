"""Contracts for the storage docs' observation-manifest reader."""

from __future__ import annotations

import typing as t

import pytest

from docs._ext.storages._observations import (
    MANIFEST_VERSION,
    AgentObservation,
    ObservationIndex,
    detect_version_drift,
    load_observation_index,
)

if t.TYPE_CHECKING:
    import pathlib

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
