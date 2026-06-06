"""Shared test helpers.

Currently exposes :func:`fixture_path`, which locates fixture files under
``tests/samples/<agent>/<store_id>/`` so adapter tests (current and future)
share one fixture layout.
"""

from __future__ import annotations

import pathlib

import pytest

SAMPLES_ROOT = pathlib.Path(__file__).parent / "samples"


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


@pytest.fixture(autouse=True)
def _isolated_agentgrep_db(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the DB cache at a per-test path.

    The cache-aware search path consults ``default_db_path()`` when no
    explicit db path is given, so without this guard the suite would
    read — and schema rebuilds would erase — the developer's real cache
    under ``$XDG_CACHE_HOME``.
    """
    monkeypatch.setenv("AGENTGREP_DB", str(tmp_path / "agentgrep-test.sqlite"))
