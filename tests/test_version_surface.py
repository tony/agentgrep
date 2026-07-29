"""Contracts for the released version and its build provenance.

``agentgrep.__version__`` is the static ``pyproject.toml`` literal, and the git
probe that names the running commit is additive: every way it can fail has to
leave that plain release version behind, silently.
"""

from __future__ import annotations

import importlib.metadata
import pathlib
import shutil
import subprocess
import tomllib
import types
import typing as t

import packaging.version
import pytest

import agentgrep
from agentgrep import _version
from agentgrep.cli import parser as cli_parser

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pyproject_literal() -> str:
    """Return ``project.version`` read independently of the code under test."""
    parsed = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return t.cast("str", parsed["project"]["version"])


def _run_git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``repo`` with identity and signing pinned."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=agentgrep test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_dunder_version_is_the_static_release_literal() -> None:
    """The facade exports the pyproject literal, undecorated by any git ref."""
    assert agentgrep.__version__ == _pyproject_literal()
    assert "__version__" in agentgrep.__all__


def test_dunder_version_is_a_normalized_pep440_version() -> None:
    """A consumer can parse ``__version__`` without stripping provenance."""
    parsed = packaging.version.Version(agentgrep.__version__)

    assert str(parsed) == agentgrep.__version__


def test_installed_metadata_agrees_with_the_static_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wheel path — no checkout to read — reports the same released number.

    This is also the drift check between the static literal and the metadata
    hatchling built from it.
    """
    monkeypatch.setattr(_version, "_checkout_root", lambda: None)

    assert _version._read_release_version() == _pyproject_literal()


def test_a_foreign_manifest_falls_back_to_installed_metadata(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendored copy never reports the host project's version number."""
    foreign = tmp_path / "host-project"
    foreign.mkdir()
    _ = (foreign / "pyproject.toml").write_text(
        '[project]\nname = "host-project"\nversion = "7.7.7"\n',
        encoding="utf-8",
    )

    assert _version._pyproject_version(foreign) is None

    monkeypatch.setattr(_version, "_checkout_root", lambda: foreign)
    assert _version._read_release_version() != "7.7.7"


def test_probe_describes_the_checkout_this_package_was_imported_from() -> None:
    """The ref is this repository's own ``git describe``, not the cwd's."""
    root = _version._checkout_root()
    assert root is not None
    assert (root / "src" / "agentgrep" / "_version.py").is_file()

    expected = _run_git(root, "describe", "--tags", "--dirty", "--always").stdout.strip()

    assert _version._git_describe() == expected


@pytest.mark.slow
def test_probe_reports_a_tagged_ref_and_an_untagged_development_ref(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean tag reports the tag; a commit past it reports tag-distance-hash."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _ = _run_git(repo, "init", "--initial-branch=main")
    _ = _run_git(repo, "commit", "--allow-empty", "-m", "first")
    _ = _run_git(repo, "tag", "v9.9.9")
    monkeypatch.setattr(_version, "_checkout_root", lambda: repo)

    assert _version._git_describe() == "v9.9.9"

    _ = _run_git(repo, "commit", "--allow-empty", "-m", "second")
    described = _version._git_describe()

    assert described is not None
    assert described.startswith("v9.9.9-1-g")
    assert not _version.BuildProvenance("9.9.9", described).is_release_build
    assert (
        _version.format_version_line(
            _version.BuildProvenance("9.9.9", described),
        )
        == f"9.9.9 (dev: {described})"
    )


def test_probe_is_silent_without_a_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An installed wheel has no repository to describe."""
    monkeypatch.setattr(_version, "_checkout_root", lambda: None)

    assert _version._git_describe() is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_probe_is_silent_without_git_on_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No git binary degrades to no ref, not to an error."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert _version._git_describe() is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_probe_is_silent_when_the_command_cannot_be_spawned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A spawn failure is swallowed, not propagated to the caller."""

    def _explode(*_args: object, **_kwargs: object) -> object:
        msg = "no such file"
        raise OSError(msg)

    monkeypatch.setattr(subprocess, "run", _explode)

    assert _version._git_describe() is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_probe_is_silent_when_git_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Git's own stderr is captured, never relayed to the terminal."""

    def _fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository\n",
        )

    monkeypatch.setattr(subprocess, "run", _fail)

    assert _version._git_describe() is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_checkout_without_a_ref_is_still_a_development_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ref is not the same as a release: a working tree is never published.

    A developer whose machine has no ``git`` still gets an honest answer. The
    version line stays the bare release, because there is no ref to name.
    """
    monkeypatch.setattr(_version, "_cached_provenance", None)
    monkeypatch.setattr(_version, "_git_describe", lambda: None)

    provenance = _version.build_provenance()

    assert provenance.git_ref is None
    assert not provenance.is_release_build
    assert provenance.build_kind == "development"
    assert _version.format_version_line(provenance) == agentgrep.__version__


def test_an_installed_wheel_without_a_ref_reports_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off a checkout, an absent install record means a published artifact."""
    monkeypatch.setattr(_version, "_cached_provenance", None)
    monkeypatch.setattr(_version, "_checkout_root", lambda: None)
    monkeypatch.setattr(_version, "_git_describe", lambda: None)
    monkeypatch.setattr(_version, "_install_provenance", lambda: ("release", None))

    provenance = _version.build_provenance()

    assert provenance.is_release_build
    assert provenance.build_kind == "release"


def test_a_vcs_install_supplies_the_ref_when_git_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pip install git+URL`` has no working tree but records its commit."""
    monkeypatch.setattr(_version, "_cached_provenance", None)
    monkeypatch.setattr(_version, "_checkout_root", lambda: None)
    monkeypatch.setattr(_version, "_git_describe", lambda: None)
    monkeypatch.setattr(
        _version,
        "_install_provenance",
        lambda: ("development", "cab6f56b9f6d"),
    )

    provenance = _version.build_provenance()

    assert provenance.git_ref == "cab6f56b9f6d"
    assert not provenance.is_release_build


class _StubDistribution:
    """Stand-in for the object ``importlib.metadata.distribution`` returns."""

    def __init__(self, origin: object) -> None:
        self.origin = origin


@pytest.mark.parametrize(
    ("test_id", "origin", "expected"),
    [
        ("wheel", None, ("release", None)),
        (
            "editable",
            types.SimpleNamespace(dir_info=types.SimpleNamespace(editable=True)),
            ("development", None),
        ),
        (
            "local_source_tree",
            types.SimpleNamespace(dir_info=types.SimpleNamespace(editable=False)),
            ("development", None),
        ),
        (
            "vcs",
            types.SimpleNamespace(vcs_info=types.SimpleNamespace(commit_id="deadbeef")),
            ("development", "deadbeef"),
        ),
        (
            "direct_archive",
            types.SimpleNamespace(archive_info=types.SimpleNamespace()),
            ("release", None),
        ),
        ("unrecognized", types.SimpleNamespace(), ("unknown", None)),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_install_records_classify_by_pep610_origin(
    test_id: str,
    origin: object,
    expected: tuple[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ``direct_url.json`` shape maps to one build kind and ref."""
    del test_id
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda _name: _StubDistribution(origin),
    )

    assert _version._install_provenance() == expected


@pytest.mark.parametrize(
    "raised",
    [importlib.metadata.PackageNotFoundError(), ValueError("truncated direct_url.json")],
    ids=["not_installed", "unreadable_record"],
)
def test_an_unusable_install_record_reports_unknown(
    raised: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or corrupt record admits ignorance instead of claiming release."""

    def _raise(_name: str) -> object:
        raise raised

    monkeypatch.setattr(importlib.metadata, "distribution", _raise)

    assert _version._install_provenance() == ("unknown", None)


def test_provenance_resolves_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second caller reads the cache instead of spawning git again."""
    monkeypatch.setattr(_version, "_cached_provenance", None)
    probes = 0

    def _probe() -> str:
        nonlocal probes
        probes += 1
        return "v9.9.9-2-gdeadbee"

    monkeypatch.setattr(_version, "_git_describe", _probe)

    assert _version.cached_build_provenance() is None
    first = _version.build_provenance()
    second = _version.build_provenance()

    assert probes == 1
    assert first is second
    assert _version.cached_build_provenance() is first


def test_cli_version_flag_prints_the_version_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentgrep --version`` exits zero with ``prog version`` on stdout."""
    with pytest.raises(SystemExit) as excinfo:
        _ = cli_parser.parse_args(["--version"])

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    prog, _, reported = captured.out.strip().partition(" ")
    assert prog == "agentgrep"
    assert reported.startswith(agentgrep.__version__)


def test_cli_version_flag_names_the_ref_on_a_development_build(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An untagged checkout reports the ref instead of a bare release number."""
    monkeypatch.setattr(
        _version,
        "_cached_provenance",
        _version.BuildProvenance(agentgrep.__version__, "v9.9.9-6-gcab6f56b"),
    )

    with pytest.raises(SystemExit):
        _ = cli_parser.parse_args(["--version"])

    captured = capsys.readouterr()
    assert captured.out.strip() == (f"agentgrep {agentgrep.__version__} (dev: v9.9.9-6-gcab6f56b)")
