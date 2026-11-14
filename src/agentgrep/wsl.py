"""Windows-host paths as seen from inside a WSL distro.

A WSL distro sees the Windows drives through DrvFs mounts, ``/mnt/c`` by
default. Agents installed on the Windows side keep their history under the
Windows profile, so from WSL a Claude desktop session lives at a path such as
``/mnt/c/Users/<name>/AppData/Roaming/Claude/...``. This module gives those
locations a type, finds the profiles to look in, and prints them without the
Windows account name.

:class:`WSLHostPath` is a :class:`pathlib.PosixPath`. Its ``str()`` and
:func:`os.fspath` are the real path, so it opens, stats, compares, and hashes
like any other path. Only :attr:`WSLHostPath.display` and ``repr()`` hide the
account name, by rewriting the profile prefix to a Windows variable.

Three role objects name the places agent stores live:

- :class:`WSLHost`: a mounted Windows drive, such as ``/mnt/c``.
- :class:`WSLHostUserProfile`: one ``%USERPROFILE%``.
- :class:`WSLHostAppData`: ``%APPDATA%`` (``AppData/Roaming``) or
  ``%LOCALAPPDATA%`` (``AppData/Local``).

They implement :class:`os.PathLike`, so they pass anywhere a path does, and
joining one with ``/`` returns a :class:`WSLHostPath`.

Examples
--------
Point the users root at a scratch tree, the way ``AGENTGREP_WSL_USERS_ROOT``
does on a real machine:

>>> import os, pathlib, tempfile
>>> from unittest import mock
>>> from agentgrep.wsl import USERS_ROOT_ENV, host_user_profiles
>>> scratch = pathlib.Path(tempfile.mkdtemp()) / "Users"
>>> _ = (scratch / "someone" / "AppData" / "Roaming").mkdir(parents=True)
>>> _ = (scratch / "Public").mkdir()
>>> with mock.patch.dict(os.environ, {USERS_ROOT_ENV: str(scratch)}):
...     profiles = host_user_profiles(require_wsl=False)
...     claude = profiles[0].appdata / "Claude"
...     print(profiles, repr(claude))
(WSLHostUserProfile('%USERPROFILE%'),) WSLHostPath('%APPDATA%/Claude')
>>> str(claude) == str(scratch / "someone" / "AppData" / "Roaming" / "Claude")
True
"""

from __future__ import annotations

import configparser
import dataclasses
import functools
import hashlib
import hmac
import os
import pathlib
import re

USERS_ROOT_ENV = "AGENTGREP_WSL_USERS_ROOT"
"""Environment variable that pins the Windows users directory seen from WSL.

Set it when drive ``C:`` is mounted somewhere other than the automount root,
or to point discovery at a test tree. When unset, the users directory is
``<automount root>/c/Users``.
"""

DEFAULT_MOUNT_ROOT = pathlib.PurePosixPath("/mnt")
"""Default ``[automount] root`` of ``/etc/wsl.conf``, without its trailing slash."""

USERPROFILE = "%USERPROFILE%"
"""Display token for a Windows user profile directory."""

APPDATA = "%APPDATA%"
"""Display token for ``%USERPROFILE%/AppData/Roaming``."""

LOCALAPPDATA = "%LOCALAPPDATA%"
"""Display token for ``%USERPROFILE%/AppData/Local``."""

USERNAME = "%USERNAME%"
"""Display token for the Windows account name inside an encoded folder name.

Agents name project folders after the working directory with separators
replaced by dashes, so a project under the profile becomes
``C--Users-<name>-work-demo`` (Claude Code) or ``C-Users-<name>-...``
(Cursor); the account name in such a segment is shown as this token.
"""

_SERVICE_PROFILES = frozenset({"all users", "default", "default user", "public"})
"""Profiles Windows creates for itself under ``C:\\Users``, compared casefolded."""

_PROC_VERSION = pathlib.Path("/proc/version")
_WSL_CONF = pathlib.Path("/etc/wsl.conf")


def is_wsl(proc_version: pathlib.Path = _PROC_VERSION) -> bool:
    """Return whether this process runs inside a WSL distro.

    WSL kernels report ``microsoft`` in ``/proc/version``. Any read error,
    including a missing file on macOS, means "not WSL".

    Parameters
    ----------
    proc_version : pathlib.Path
        Kernel version file to read.

    Returns
    -------
    bool
        ``True`` on a WSL kernel.

    Examples
    --------
    >>> import pathlib
    >>> from agentgrep.wsl import is_wsl
    >>> is_wsl(pathlib.Path("/nonexistent/proc/version"))
    False
    """
    try:
        text = proc_version.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "microsoft" in text.casefold()


def mount_root(wsl_conf: pathlib.Path = _WSL_CONF) -> pathlib.PurePosixPath:
    """Return the directory WSL mounts Windows drives under.

    Reads ``root`` from the ``[automount]`` section of ``/etc/wsl.conf``
    and falls back to ``/mnt`` when the file, section, or key is missing or
    unreadable. WSL applies the setting when the distro starts, so the file
    is read once per process.

    Parameters
    ----------
    wsl_conf : pathlib.Path
        Distro configuration file to read.

    Returns
    -------
    pathlib.PurePosixPath
        Mount root without a trailing slash, such as ``/mnt``.

    Examples
    --------
    >>> import pathlib
    >>> from agentgrep.wsl import mount_root
    >>> mount_root(pathlib.Path("/nonexistent/wsl.conf"))
    PurePosixPath('/mnt')
    """
    return _parse_mount_root(str(wsl_conf))


@functools.lru_cache(maxsize=8)
def _parse_mount_root(wsl_conf: str) -> pathlib.PurePosixPath:
    """Parse ``[automount] root`` once per file and process.

    WSL applies the automount root when the distro starts, so the value
    cannot change under a running process, and caching it keeps path display
    (which runs on the TUI's render path) free of file I/O.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(pathlib.Path(wsl_conf).read_text(encoding="utf-8", errors="ignore"))
    except OSError, configparser.Error:
        return DEFAULT_MOUNT_ROOT
    root = parser.get("automount", "root", fallback="").strip().strip("\"'")
    if not root.startswith("/"):
        return DEFAULT_MOUNT_ROOT
    return pathlib.PurePosixPath(root.rstrip("/") or "/")


def _users_root_text() -> str:
    """Return :func:`users_root` as a string without building a path object."""
    return os.environ.get(USERS_ROOT_ENV) or f"{mount_root()}/c/Users"


def users_root() -> WSLHostPath:
    """Return the Windows users directory as seen from WSL.

    ``AGENTGREP_WSL_USERS_ROOT`` wins when set; otherwise the directory is
    ``<automount root>/c/Users``.

    Returns
    -------
    WSLHostPath
        The users directory. It may not exist.
    """
    override = os.environ.get(USERS_ROOT_ENV)
    if override:
        return WSLHostPath(override)
    return WSLHostPath(mount_root(), "c", "Users")


def host_user_profiles(
    root: str | os.PathLike[str] | None = None,
    *,
    require_wsl: bool = True,
) -> tuple[WSLHostUserProfile, ...]:
    """Return the Windows user profiles reachable from this distro.

    Skips the profiles Windows creates for itself (``Public``, ``Default``,
    ``Default User``, ``All Users``) and anything that is not a directory.
    Returns an empty tuple off WSL, when the users directory is missing, or
    when listing it fails, so callers can probe unconditionally.

    Parameters
    ----------
    root : str | os.PathLike[str] | None
        Users directory to list. Defaults to :func:`users_root`.
    require_wsl : bool
        Return nothing unless :func:`is_wsl` is true. Tests and explicit
        callers pass ``False`` together with a ``root``.

    Returns
    -------
    tuple[WSLHostUserProfile, ...]
        Profiles sorted by directory name.

    Examples
    --------
    >>> from agentgrep.wsl import host_user_profiles
    >>> host_user_profiles("/nonexistent/Users", require_wsl=False)
    ()
    """
    if require_wsl and not is_wsl():
        return ()
    users = WSLHostPath(root) if root is not None else users_root()
    try:
        entries = sorted(users.iterdir())
    except OSError:
        return ()
    profiles: list[WSLHostUserProfile] = []
    for entry in entries:
        if entry.name.casefold() in _SERVICE_PROFILES:
            continue
        try:
            is_profile = entry.is_dir()
        except OSError:
            continue
        if is_profile:
            profiles.append(WSLHostUserProfile(entry))
    return tuple(profiles)


def _redact_account(segment: str, profile: str) -> str:
    """Replace ``Users-<profile>`` in an encoded folder name with ``Users-%USERNAME%``."""
    if profile.casefold() not in segment.casefold():
        return segment
    pattern = re.compile(rf"(?<=Users-){re.escape(profile)}(?=-|$)", re.IGNORECASE)
    return pattern.sub(USERNAME, segment)


def _restore_account(segment: str, profile: str) -> str:
    """Invert :func:`_redact_account` for one profile."""
    return segment.replace(USERNAME, profile)


def display_host_path(path: str | os.PathLike[str]) -> str | None:
    """Rewrite a path inside a Windows profile to its Windows-variable form.

    The profile prefix becomes ``%APPDATA%`` for ``AppData/Roaming``,
    ``%LOCALAPPDATA%`` for ``AppData/Local``, and ``%USERPROFILE%`` for the
    rest of the profile. The account name inside an encoded project folder
    (``C--Users-<name>-work``) becomes ``%USERNAME%``, so the Windows account
    name never reaches output.
    The match is a string prefix test against :func:`users_root`; it touches
    no files.

    Parameters
    ----------
    path : str | os.PathLike[str]
        Absolute path to rewrite.

    Returns
    -------
    str | None
        The rewritten path, or ``None`` when ``path`` is not inside a user
        profile (paths under ``Public`` and the other service profiles carry
        no account name and stay as they are).

    Examples
    --------
    >>> import os
    >>> from unittest import mock
    >>> from agentgrep.wsl import USERS_ROOT_ENV, display_host_path
    >>> env = mock.patch.dict(os.environ, {USERS_ROOT_ENV: "/mnt/c/Users"})
    >>> with env:
    ...     for path in (
    ...         "/mnt/c/Users/someone/AppData/Roaming/Code/User",
    ...         "/mnt/c/Users/someone/AppData/Local/Packages",
    ...         "/mnt/c/Users/someone/.claude/projects",
    ...         "/mnt/c/Users/someone/.claude/projects/C--Users-someone-work/s.jsonl",
    ...         "/mnt/c/Users/Public/Documents",
    ...         "/home/someone/.claude",
    ...     ):
    ...         print(display_host_path(path))
    %APPDATA%/Code/User
    %LOCALAPPDATA%/Packages
    %USERPROFILE%/.claude/projects
    %USERPROFILE%/.claude/projects/C--Users-%USERNAME%-work/s.jsonl
    None
    None
    """
    text = os.fspath(path)
    root = _users_root_text().rstrip("/")
    if not text.startswith(root + "/"):
        return None
    profile, _, tail = text[len(root) + 1 :].partition("/")
    if not profile or profile.casefold() in _SERVICE_PROFILES:
        return None
    parts = [_redact_account(part, profile) for part in tail.split("/")] if tail else []
    if len(parts) >= 2 and parts[0].casefold() == "appdata":
        area = parts[1].casefold()
        if area == "roaming":
            return "/".join((APPDATA, *parts[2:]))
        if area == "local":
            return "/".join((LOCALAPPDATA, *parts[2:]))
    return "/".join((USERPROFILE, *parts))


def is_host_path(path: str | os.PathLike[str]) -> bool:
    """Return whether ``path`` lies under the Windows users directory.

    A string prefix test against :func:`users_root`, with no file access.
    Callers use it to treat paths on the 9p Windows mount, where every
    ``stat`` is a round trip, differently from local paths.

    Examples
    --------
    >>> import os
    >>> from unittest import mock
    >>> from agentgrep.wsl import USERS_ROOT_ENV, is_host_path
    >>> with mock.patch.dict(os.environ, {USERS_ROOT_ENV: "/mnt/c/Users"}):
    ...     print(is_host_path("/mnt/c/Users/someone/.claude"), is_host_path("/home/someone"))
    True False
    """
    return os.fspath(path).startswith(_users_root_text().rstrip("/") + "/")


_DISPLAY_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (APPDATA, ("AppData", "Roaming")),
    (LOCALAPPDATA, ("AppData", "Local")),
    (USERPROFILE, ()),
)


_MACHINE_ID = pathlib.Path("/etc/machine-id")


@functools.lru_cache(maxsize=1)
def _profile_key_secret() -> bytes:
    """Return the per-machine key for :func:`host_profile_key`."""
    try:
        return _MACHINE_ID.read_bytes().strip() or b"agentgrep"
    except OSError:
        return b"agentgrep"


def host_profile_key(path: str | os.PathLike[str]) -> str | None:
    """Return an opaque key naming the Windows profile that holds ``path``.

    The display form drops the profile, so two profiles' copies of the same
    relative file print the same. This key tells them apart without naming
    the account: it is an HMAC of the casefolded profile name keyed by
    ``/etc/machine-id``, so it is stable on one machine and cannot be
    reversed by guessing account names elsewhere.

    Parameters
    ----------
    path : str | os.PathLike[str]
        Absolute path to classify.

    Returns
    -------
    str | None
        Sixteen hex characters, or ``None`` when ``path`` is not inside a
        user profile under :func:`users_root`.

    Examples
    --------
    >>> import os
    >>> from unittest import mock
    >>> from agentgrep.wsl import USERS_ROOT_ENV, host_profile_key
    >>> with mock.patch.dict(os.environ, {USERS_ROOT_ENV: "/mnt/c/Users"}):
    ...     key = host_profile_key("/mnt/c/Users/someone/.codex/history.jsonl")
    ...     same = host_profile_key("/mnt/c/Users/SOMEONE/AppData")
    ...     other = host_profile_key("/home/someone/.codex/history.jsonl")
    >>> len(key), key == same, other
    (16, True, None)
    """
    text = os.fspath(path)
    root = _users_root_text().rstrip("/")
    if not text.startswith(root + "/"):
        return None
    profile = text[len(root) + 1 :].partition("/")[0]
    if not profile or profile.casefold() in _SERVICE_PROFILES:
        return None
    digest = hmac.new(_profile_key_secret(), profile.casefold().encode(), hashlib.sha256)
    return digest.hexdigest()[:16]


def host_display_candidates(
    text: str,
    *,
    profile_key: str | None = None,
) -> tuple[WSLHostPath, ...]:
    """Return the real paths a Windows-variable display form can name.

    The inverse of :func:`display_host_path`: ``%APPDATA%/Claude`` becomes
    ``<profile>/AppData/Roaming/Claude`` for every Windows profile under
    :func:`users_root`, since the display form leaves out which profile it
    came from. Glob characters pass through untouched, so ``path:`` query
    patterns expand the same way.

    Parameters
    ----------
    text : str
        A display form starting with ``%USERPROFILE%``, ``%APPDATA%``, or
        ``%LOCALAPPDATA%``.
    profile_key : str | None
        Keep only the profile whose :func:`host_profile_key` matches, which
        resolves a display form back to the one file it was made from.

    Returns
    -------
    tuple[WSLHostPath, ...]
        One candidate per profile, in profile order; empty when ``text``
        has no such prefix or no profile is reachable.

    Examples
    --------
    >>> import os, pathlib, tempfile
    >>> from unittest import mock
    >>> from agentgrep.wsl import USERS_ROOT_ENV, host_display_candidates
    >>> users = pathlib.Path(tempfile.mkdtemp()) / "Users"
    >>> _ = (users / "someone").mkdir(parents=True)
    >>> with mock.patch.dict(os.environ, {USERS_ROOT_ENV: str(users)}):
    ...     (path,) = host_display_candidates("%APPDATA%/Claude")
    ...     print(path.relative_to(users).as_posix(), repr(path))
    someone/AppData/Roaming/Claude WSLHostPath('%APPDATA%/Claude')
    >>> host_display_candidates("~/notes")
    ()
    """
    for token, parts in _DISPLAY_TOKENS:
        if text == token or text.startswith(f"{token}/"):
            tail = [part for part in text[len(token) :].split("/") if part]
            return tuple(
                profile.path.joinpath(
                    *parts,
                    *(_restore_account(part, profile.path.name) for part in tail),
                )
                for profile in host_user_profiles(require_wsl=False)
                if profile_key is None or host_profile_key(profile) == profile_key
            )
    return ()


class WSLHostPath(pathlib.PosixPath):
    """A path on the Windows host, seen from inside WSL.

    Behaves exactly like :class:`pathlib.PosixPath`: ``str()``,
    :func:`os.fspath`, equality, and hashing all use the real path. The
    account name is hidden only by :attr:`display` and ``repr()``.

    Examples
    --------
    >>> import os, pathlib
    >>> from unittest import mock
    >>> from agentgrep.wsl import USERS_ROOT_ENV, WSLHostPath
    >>> path = WSLHostPath("/mnt/c/Users/someone/.codex/sessions")
    >>> with mock.patch.dict(os.environ, {USERS_ROOT_ENV: "/mnt/c/Users"}):
    ...     print(repr(path))
    WSLHostPath('%USERPROFILE%/.codex/sessions')
    >>> str(path)
    '/mnt/c/Users/someone/.codex/sessions'
    >>> path == pathlib.Path("/mnt/c/Users/someone/.codex/sessions")
    True
    >>> hash(path) == hash(pathlib.Path("/mnt/c/Users/someone/.codex/sessions"))
    True
    """

    __slots__ = ()

    @property
    def display(self) -> str:
        """Return the path with any profile prefix rewritten to a Windows variable."""
        return display_host_path(self) or str(self)

    def __repr__(self) -> str:
        """Return ``WSLHostPath('<display form>')``."""
        return f"{type(self).__name__}({self.display!r})"

    def as_windows(self, root: pathlib.PurePosixPath | None = None) -> pathlib.PureWindowsPath:
        r"""Return the Windows form of this path.

        Parameters
        ----------
        root : pathlib.PurePosixPath | None
            Automount root the drive letters sit under. Defaults to
            :func:`mount_root`.

        Returns
        -------
        pathlib.PureWindowsPath
            For example ``C:\Users\someone`` for ``/mnt/c/Users/someone``.

        Raises
        ------
        ValueError
            If the path is not under a drive mount.

        Examples
        --------
        >>> import pathlib
        >>> from agentgrep.wsl import WSLHostPath
        >>> WSLHostPath("/mnt/c/Users/someone").as_windows(pathlib.PurePosixPath("/mnt"))
        PureWindowsPath('C:/Users/someone')
        """
        base = root if root is not None else mount_root()
        try:
            relative = pathlib.PurePosixPath(self).relative_to(base)
        except ValueError:
            msg = f"{self.display} is not under the drive mount root {base}"
            raise ValueError(msg) from None
        drive, *rest = relative.parts or ("",)
        if len(drive) != 1 or not drive.isalpha():
            msg = f"{self.display} is not under a drive mount in {base}"
            raise ValueError(msg)
        return pathlib.PureWindowsPath(f"{drive.upper()}:/", *rest)

    @classmethod
    def from_windows(
        cls,
        path: str | pathlib.PureWindowsPath,
        root: pathlib.PurePosixPath | None = None,
    ) -> WSLHostPath:
        r"""Translate a Windows drive path to its WSL mount path.

        :class:`pathlib.PurePosixPath` would turn ``C:\Users`` into the
        string ``C:/Users``; this maps the drive letter to its DrvFs mount
        instead, without spawning ``wslpath``.

        Parameters
        ----------
        path : str | pathlib.PureWindowsPath
            Windows path with a drive letter, such as ``C:\Users\someone``.
        root : pathlib.PurePosixPath | None
            Automount root. Defaults to :func:`mount_root`.

        Returns
        -------
        WSLHostPath
            The mounted path, such as ``/mnt/c/Users/someone``.

        Raises
        ------
        ValueError
            If ``path`` has no drive letter (UNC and relative paths).

        Examples
        --------
        >>> import pathlib
        >>> from agentgrep.wsl import WSLHostPath
        >>> str(WSLHostPath.from_windows("C:\\Users\\someone", pathlib.PurePosixPath("/mnt")))
        '/mnt/c/Users/someone'
        """
        windows = pathlib.PureWindowsPath(path)
        drive = windows.drive
        if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
            msg = f"not a drive-letter path: {windows}"
            raise ValueError(msg)
        base = root if root is not None else mount_root()
        return cls(base, drive[0].lower(), *windows.parts[1:])


@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class _HostLocation(os.PathLike[str]):
    """A named place on the Windows host; path-like, joins to :class:`WSLHostPath`."""

    path: WSLHostPath

    def __fspath__(self) -> str:
        """Return the real path."""
        return os.fspath(self.path)

    def __truediv__(self, other: str | os.PathLike[str]) -> WSLHostPath:
        """Join a child path."""
        return self.path / other

    def __repr__(self) -> str:
        """Return ``<Role>('<display form>')``."""
        return f"{type(self).__name__}({self.display!r})"

    @property
    def display(self) -> str:
        """Return the location with the account name rewritten to a Windows variable."""
        return self.path.display

    def is_dir(self) -> bool:
        """Return whether the location exists as a directory."""
        return self.path.is_dir()


@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class WSLHost(_HostLocation):
    """A Windows drive mounted into WSL, such as ``/mnt/c``.

    Examples
    --------
    >>> import pathlib
    >>> from agentgrep.wsl import WSLHost
    >>> host = WSLHost.from_drive("C:", pathlib.PurePosixPath("/mnt"))
    >>> host.drive
    'C:'
    >>> str(host.users)
    '/mnt/c/Users'
    """

    @classmethod
    def from_drive(cls, drive: str, root: pathlib.PurePosixPath | None = None) -> WSLHost:
        """Return the mount for a drive letter such as ``"C:"`` or ``"d"``."""
        letter = drive.rstrip(":\\/")
        if len(letter) != 1 or not letter.isalpha():
            msg = f"not a drive letter: {drive!r}"
            raise ValueError(msg)
        base = root if root is not None else mount_root()
        return cls(WSLHostPath(base, letter.lower()))

    @property
    def drive(self) -> str:
        """Return the drive, such as ``"C:"``."""
        return f"{self.path.name.upper()}:"

    @property
    def users(self) -> WSLHostPath:
        """Return the drive's ``Users`` directory."""
        return self.path / "Users"

    def user_profiles(self) -> tuple[WSLHostUserProfile, ...]:
        """Return the user profiles on this drive; see :func:`host_user_profiles`."""
        return host_user_profiles(self.users, require_wsl=False)


@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class WSLHostAppData(_HostLocation):
    """``%APPDATA%`` or ``%LOCALAPPDATA%`` of one Windows profile."""


@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class WSLHostUserProfile(_HostLocation):
    """One Windows user profile, ``%USERPROFILE%``.

    Examples
    --------
    >>> import os
    >>> from unittest import mock
    >>> from agentgrep.wsl import USERS_ROOT_ENV, WSLHostPath, WSLHostUserProfile
    >>> profile = WSLHostUserProfile(WSLHostPath("/mnt/c/Users/someone"))
    >>> with mock.patch.dict(os.environ, {USERS_ROOT_ENV: "/mnt/c/Users"}):
    ...     print(repr(profile.appdata))
    ...     print(repr(profile.package_appdata("Vendor.App_abc123") / "App"))
    WSLHostAppData('%APPDATA%')
    WSLHostPath('%LOCALAPPDATA%/Packages/Vendor.App_abc123/LocalCache/Roaming/App')
    """

    @property
    def appdata(self) -> WSLHostAppData:
        """Return ``%APPDATA%`` (``AppData/Roaming``)."""
        return WSLHostAppData(self.path / "AppData" / "Roaming")

    @property
    def localappdata(self) -> WSLHostAppData:
        """Return ``%LOCALAPPDATA%`` (``AppData/Local``)."""
        return WSLHostAppData(self.path / "AppData" / "Local")

    def package_appdata(self, family: str) -> WSLHostAppData:
        """Return the ``%APPDATA%`` an MSIX package writes to.

        Windows redirects new ``AppData`` files of a virtualized packaged app
        to ``%LOCALAPPDATA%/Packages/<family>/LocalCache/Roaming`` and merges
        them back only for processes inside Windows. From WSL there is no
        merge, so the files are only here.

        Parameters
        ----------
        family : str
            Package family name, such as ``OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0``.
        """
        return WSLHostAppData(self.localappdata / "Packages" / family / "LocalCache" / "Roaming")

    def app_dirs(self, name: str, *, package: str | None = None) -> tuple[WSLHostPath, ...]:
        """Return the existing ``%APPDATA%/<name>`` directories of one app.

        Checks the real ``%APPDATA%`` first, then the package's redirected
        copy when ``package`` names an MSIX family, because packaged apps
        differ: some write to the real directory and some only to the
        redirected one.

        Parameters
        ----------
        name : str
            Directory name under ``%APPDATA%``, such as ``Claude``.
        package : str | None
            MSIX package family name to also check.

        Returns
        -------
        tuple[WSLHostPath, ...]
            Existing directories, real location first.
        """
        candidates = [self.appdata / name]
        if package:
            candidates.append(self.package_appdata(package) / name)
        found: list[WSLHostPath] = []
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    found.append(candidate)
            except OSError:
                continue
        return tuple(found)


__all__ = (
    "APPDATA",
    "DEFAULT_MOUNT_ROOT",
    "LOCALAPPDATA",
    "USERNAME",
    "USERPROFILE",
    "USERS_ROOT_ENV",
    "WSLHost",
    "WSLHostAppData",
    "WSLHostPath",
    "WSLHostUserProfile",
    "display_host_path",
    "host_display_candidates",
    "host_profile_key",
    "host_user_profiles",
    "is_host_path",
    "is_wsl",
    "mount_root",
    "users_root",
)
