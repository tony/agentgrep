"""Shared test helpers.

Currently exposes :func:`fixture_path`, which locates fixture files under
``tests/samples/<agent>/<store_id>/`` so adapter tests (current and future)
share one fixture layout.
"""

from __future__ import annotations

import pathlib

import pytest

SAMPLES_ROOT = pathlib.Path(__file__).parent / "samples"


@pytest.fixture(autouse=True)
def _isolate_vscode_wsl_bridge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the WSL cross-host probes off the developer's real ``/mnt/c``.

    ``discover_vscode_sources`` and ``discover_cursor_ide_sources`` auto-probe
    the Windows-host data under ``/mnt/c/Users`` when they detect WSL. That path
    is independent of ``$HOME``, so on a WSL dev box it would leak real chat
    history into hermetic ``find --agent all`` tests. Point the users-mount root
    at a nonexistent path by default; tests that exercise a bridge override
    ``AGENTGREP_WSL_USERS_ROOT`` explicitly.
    """
    monkeypatch.setenv("AGENTGREP_WSL_USERS_ROOT", str(tmp_path / "no-windows-mount"))


def fixture_path(store_id: str, name: str) -> pathlib.Path:
    """Return the path to a fixture file for one ``store_id``.

    Parameters
    ----------
    store_id : str
        The dotted store identifier from
        :class:`agentgrep.stores.StoreDescriptor`.
    name : str
        The basename of the fixture file under
        ``tests/samples/<agent>/<store_id>/``.

    Returns
    -------
    pathlib.Path
        Absolute path to the fixture.

    Raises
    ------
    FileNotFoundError
        If the fixture file is missing — keeps catalog edits honest.
    """
    agent = store_id.split(".", 1)[0]
    path = SAMPLES_ROOT / agent / store_id / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
