"""Report which version of agentgrep is running, and from which commit.

The version is the ``project.version`` literal in ``pyproject.toml``, or the
installed distribution's metadata for a wheel. A checkout knows one thing the
literal cannot — which commit — so :func:`build_provenance` adds
``git describe``, falling back to what the installer recorded when there is no
working tree or no ``git``. Every failure path is silent: no ref, and nothing
on stderr.

:func:`build_provenance` spawns a subprocess, so the TUI must not call it
directly; the Textual UI runs on one thread and anything slow there freezes
keystrokes and the spinner. Resolve it in a worker, and read
:func:`cached_build_provenance` from the UI, which only reads a variable.

``tomllib``, ``shutil``, ``subprocess``, and ``importlib.metadata`` are
imported inside the functions that use them, so ``import agentgrep`` stays
cheap for callers that never ask for a version.
"""

from __future__ import annotations

import logging
import pathlib
import typing as t

logger = logging.getLogger(__name__)

__all__ = [
    "BuildProvenance",
    "build_provenance",
    "cached_build_provenance",
    "format_build_status",
    "format_version_line",
    "release_version",
]

#: Distribution name looked up in installed metadata when no checkout is found.
DISTRIBUTION_NAME = "agentgrep"

#: Reported when neither a checkout nor installed metadata names a version. A
#: valid PEP 440 local version, so a consumer parsing it still succeeds.
UNKNOWN_VERSION = "0+unknown"

#: Wall-clock cap on the ``git describe`` probe. A repository slow enough to
#: exceed it is reported as "no ref" rather than delaying the caller further.
GIT_DESCRIBE_TIMEOUT_SECONDS = 2.0


class BuildProvenance(t.NamedTuple):
    """The released version plus the git ref the running code came from.

    Attributes
    ----------
    release : str
        PEP 440 release version from the static ``pyproject.toml`` literal (or
        installed distribution metadata), such as ``"0.1.0a45"``.
    git_ref : str | None
        ``git describe --tags --dirty --always`` output for the checkout this
        code was imported from, such as ``"v0.1.0a45-6-gcab6f56b"``. ``None``
        when there is no checkout to describe, no ``git`` to describe it with,
        or the command failed.
    install_kind : str, default "unknown"
        What the installer recorded about where this distribution came from:
        ``"release"`` for a published artifact, ``"development"`` for an
        editable, local-source, or VCS install, ``"unknown"`` when nothing was
        recorded. Consulted only when :attr:`git_ref` is ``None``, so a
        checkout with no usable ``git`` is not mistaken for a release.
    """

    release: str
    git_ref: str | None
    install_kind: str = "unknown"

    @property
    def is_release_build(self) -> bool:
        """Report whether the running code is exactly the released version.

        A ref is a release build only when it names the release's own tag with
        no distance and no ``-dirty`` suffix. With no ref to judge, the
        installer's own record decides: a wheel has no repository to disagree
        with its metadata, but an editable or VCS install is a development
        build whether or not ``git`` was available to describe it.

        Examples
        --------
        >>> BuildProvenance("0.1.0a45", "v0.1.0a45").is_release_build
        True
        >>> BuildProvenance("0.1.0a45", "v0.1.0a45-dirty").is_release_build
        False
        >>> BuildProvenance("0.1.0a45", "v0.1.0a45-6-gcab6f56b").is_release_build
        False
        >>> BuildProvenance("0.1.0a45", None).is_release_build
        True
        >>> BuildProvenance("0.1.0a45", None, "development").is_release_build
        False
        """
        if self.git_ref is not None:
            return self.git_ref == f"v{self.release}"
        return self.install_kind != "development"

    @property
    def build_kind(self) -> str:
        """Return ``"release"`` or ``"development"`` for display.

        Examples
        --------
        >>> BuildProvenance("0.1.0a45", "v0.1.0a45").build_kind
        'release'
        >>> BuildProvenance("0.1.0a45", "v0.1.0a45-6-gcab6f56b").build_kind
        'development'
        """
        return "release" if self.is_release_build else "development"


def format_version_line(provenance: BuildProvenance) -> str:
    """Render one ``--version`` line: the release, and the ref when it differs.

    Parameters
    ----------
    provenance : BuildProvenance
        Resolved release version and git ref.

    Returns
    -------
    str
        The bare PEP 440 release version, or that version followed by a
        parenthesized development ref. A development build whose ref could not
        be read renders bare too — there is nothing to name, and ``--version``
        is the wrong surface to say so. :func:`format_build_status` reports the
        build kind explicitly.

    Examples
    --------
    >>> format_version_line(BuildProvenance("0.1.0a45", "v0.1.0a45"))
    '0.1.0a45'
    >>> format_version_line(BuildProvenance("0.1.0a45", None))
    '0.1.0a45'
    >>> format_version_line(BuildProvenance("0.1.0a45", None, "development"))
    '0.1.0a45'
    >>> format_version_line(BuildProvenance("0.1.0a45", "v0.1.0a45-6-gcab6f56b"))
    '0.1.0a45 (dev: v0.1.0a45-6-gcab6f56b)'
    """
    if provenance.git_ref is None or provenance.is_release_build:
        return provenance.release
    return f"{provenance.release} (dev: {provenance.git_ref})"


def format_build_status(provenance: BuildProvenance) -> str:
    """Render the multi-line build report the explorer's ``/status`` shows.

    Parameters
    ----------
    provenance : BuildProvenance
        Resolved release version and git ref.

    Returns
    -------
    str
        Three lines: the version, whether this is a release or development
        build, and the git ref (``unavailable`` when the probe found none).

    Examples
    --------
    >>> print(format_build_status(BuildProvenance("0.1.0a45", "v0.1.0a45-6-gcab6f56b")))
    agentgrep 0.1.0a45
    Build: development
    Git ref: v0.1.0a45-6-gcab6f56b
    >>> print(format_build_status(BuildProvenance("0.1.0a45", None)))
    agentgrep 0.1.0a45
    Build: release
    Git ref: unavailable
    """
    ref = provenance.git_ref if provenance.git_ref is not None else "unavailable"
    return "\n".join(
        (
            f"{DISTRIBUTION_NAME} {provenance.release}",
            f"Build: {provenance.build_kind}",
            f"Git ref: {ref}",
        ),
    )


_cached_release: str | None = None
_cached_provenance: BuildProvenance | None = None


def release_version() -> str:
    """Return the static released version, reading it at most once per process.

    Reads ``pyproject.toml`` or installed distribution metadata on the first
    call, so it blocks. Do not call it from the TUI thread.

    Returns
    -------
    str
        PEP 440 release version, or :data:`UNKNOWN_VERSION` when neither a
        checkout nor installed metadata names one.
    """
    global _cached_release
    if _cached_release is None:
        _cached_release = _read_release_version()
    return _cached_release


def build_provenance() -> BuildProvenance:
    """Resolve the release version and the git ref, caching the result.

    Spawns ``git describe`` on the first call in a checkout, so it blocks. Call
    it from a worker thread or the CLI's main thread; the TUI reads
    :func:`cached_build_provenance` instead.

    Returns
    -------
    BuildProvenance
        The release version, plus the git ref when one could be probed.
    """
    global _cached_provenance
    cached = _cached_provenance
    if cached is not None:
        return cached
    # A racing second resolve computes the same immutable answer, so the
    # assignment is left unlocked: waiting on a lock is itself blocking work,
    # and the loser of the race would only wait to learn what it computed.
    install_kind, install_ref = _install_provenance()
    if _checkout_root() is not None:
        # A working tree is a development build whether or not git could
        # describe it; without this a checkout on a machine with no git would
        # fall back to the install record and could claim to be a release.
        install_kind = "development"
    git_ref = _git_describe()
    if git_ref is None:
        git_ref = install_ref
    resolved = BuildProvenance(
        release=release_version(),
        git_ref=git_ref,
        install_kind=install_kind,
    )
    _cached_provenance = resolved
    return resolved


def cached_build_provenance() -> BuildProvenance | None:
    """Return the already-resolved provenance, or ``None`` when unresolved.

    Reads one variable and does no I/O, so the TUI thread may call it: it
    either reports what a worker has already resolved, or says nothing has.

    Returns
    -------
    BuildProvenance | None
        The cached provenance, or ``None`` when :func:`build_provenance` has
        not run in this process yet.
    """
    return _cached_provenance


def _checkout_root() -> pathlib.Path | None:
    """Return the source checkout this package was imported from, if any.

    The discriminator is the ``src`` layout: a checkout imports
    ``<root>/src/agentgrep/_version.py`` (directly or through an editable
    install), while an installed wheel imports
    ``<site-packages>/agentgrep/_version.py``. Anchoring on the package's own
    location — rather than the working directory — keeps an installed agentgrep
    run from inside an unrelated repository from reporting that repository's
    ref.

    Returns
    -------
    pathlib.Path | None
        Directory holding ``pyproject.toml`` and ``src/agentgrep``, or ``None``
        when this package is not being imported from a checkout.
    """
    package_dir = pathlib.Path(__file__).resolve().parent
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def _read_release_version() -> str:
    """Read the released version from the checkout, else installed metadata.

    The checkout's ``pyproject.toml`` wins because it is the literal a release
    bump edits; an editable install's metadata can lag it until the next sync.
    """
    root = _checkout_root()
    if root is not None:
        literal = _pyproject_version(root)
        if literal is not None:
            return literal
    return _installed_version()


def _pyproject_version(root: pathlib.Path) -> str | None:
    """Return agentgrep's ``project.version`` from ``root/pyproject.toml``.

    The manifest has to name this distribution before its version is trusted:
    ``src/agentgrep`` vendored inside some other project's source tree would
    otherwise report that project's version number.

    Parameters
    ----------
    root : pathlib.Path
        Checkout root containing ``pyproject.toml``.

    Returns
    -------
    str | None
        The static version literal, or ``None`` when the file is unreadable,
        malformed, declares no static version, or belongs to another project.
    """
    import tomllib

    try:
        parsed = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = parsed["project"]
        name = project["name"]
        version = project["version"]
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError:
        logger.debug("pyproject version read failed", exc_info=True)
        return None
    if name != DISTRIBUTION_NAME:
        logger.debug("pyproject names another distribution; using installed metadata")
        return None
    return version if isinstance(version, str) else None


def _installed_version() -> str:
    """Return the installed distribution's version, or :data:`UNKNOWN_VERSION`."""
    import importlib.metadata

    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        logger.debug("installed distribution metadata not found")
        return UNKNOWN_VERSION


def _install_provenance() -> tuple[str, str | None]:
    """Classify how this distribution was installed, from its PEP 610 record.

    ``git describe`` answers "which commit" only where there is a checkout and
    a ``git`` to run. The installer's own ``direct_url.json`` answers the
    coarser "was this published or not" everywhere, including a flat-layout
    editable install the ``src``-layout probe cannot recognize, and a
    ``pip install git+URL`` that has no working tree at all.

    The record's ``url`` is deliberately never read: for the common editable
    case it is a local filesystem path.

    Returns
    -------
    tuple[str, str | None]
        Build kind — ``"release"``, ``"development"``, or ``"unknown"`` — and
        the commit id when the installer recorded a VCS origin.
    """
    import importlib.metadata

    try:
        origin = importlib.metadata.distribution(DISTRIBUTION_NAME).origin
    except importlib.metadata.PackageNotFoundError:
        # Never installed — a source tree run straight off sys.path.
        return "unknown", None
    except ValueError:
        # A truncated direct_url.json from an interrupted install raises out of
        # json.loads. Provenance is a reporting nicety and must not be able to
        # break a caller, and claiming a published wheel on a record that says
        # nothing of the sort would be worse than admitting ignorance.
        logger.debug("install record unreadable", exc_info=True)
        return "unknown", None
    if origin is None:
        return "release", None
    vcs_info = getattr(origin, "vcs_info", None)
    if vcs_info is not None:
        ref = getattr(vcs_info, "commit_id", "") or getattr(vcs_info, "requested_revision", "")
        return "development", str(ref) or None
    if getattr(origin, "dir_info", None) is not None:
        return "development", None
    if getattr(origin, "archive_info", None) is not None:
        return "release", None
    return "unknown", None


def _git_describe() -> str | None:
    """Describe the checkout's current ref, or return ``None`` silently.

    Every degradation path returns ``None`` rather than raising or writing to
    stderr: no checkout, no ``.git``, no ``git`` on ``PATH``, a spawn failure, a
    timeout, or a non-zero exit. ``git describe`` is read-only — it never
    mutates the repository it is pointed at.

    Returns
    -------
    str | None
        ``git describe --tags --dirty --always`` output, or ``None``.
    """
    import shutil
    import subprocess

    root = _checkout_root()
    if root is None or not (root / ".git").exists():
        return None
    git = shutil.which("git")
    if git is None:
        logger.debug("git not found on PATH; reporting the release version")
        return None
    try:
        completed = subprocess.run(
            [git, "-C", str(root), "describe", "--tags", "--dirty", "--always"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_DESCRIBE_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.SubprocessError:
        logger.debug("git describe failed", exc_info=True)
        return None
    if completed.returncode != 0:
        logger.debug(
            "git describe exited non-zero",
            extra={"agentgrep_git_describe_returncode": completed.returncode},
        )
        return None
    return completed.stdout.strip() or None
