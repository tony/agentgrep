"""CLI install + usage picker widget.

Renders one row of install-method tabs (``uvx run`` / ``pipx run`` /
``uv add`` / ``pip install``) and, for each method, a panel pairing
the install (or transient-run) command with three runnable CLI usage
snippets. Mirrors the ``{mcp-install}`` widget's cooldown matrix —
every method exists in three cooldown variants (``off`` / ``days`` /
``bypass``) that swap based on the user's saved ``Configure
cooldowns`` selection.

Cooldown flags by (method, cooldown):

* ``uvx-run`` + days  : ``uvx --exclude-newer <COOLDOWN_DURATION>`` prefix
* ``uvx-run`` + bypass: ``uvx --no-config`` prefix
* ``pipx-run`` + days : ``pipx run --pip-args=--uploaded-prior-to=<COOLDOWN_DATE>`` prefix
* ``pipx-run`` + bypass: no-op (note explains pipx's default backend has no
  cooldown control)
* ``uv-add`` + days   : install gains ``--exclude-newer <COOLDOWN_DURATION>``,
  ``uv run agentgrep …`` usage unchanged (deps pinned at install time)
* ``uv-add`` + bypass : install gains ``--no-config``, usage unchanged
* ``pip`` + days      : install gains ``--uploaded-prior-to <COOLDOWN_DURATION>``,
  ``agentgrep …`` usage unchanged
* ``pip`` + bypass    : no-op (note explains pip has no global cooldown)

The ``<COOLDOWN_DURATION>`` / ``<COOLDOWN_DATE>`` sentinels round-trip
through Pygments unchanged and are swapped for JS-mutable
``<span class="ag-cooldown-days">`` by the shared
``cooldown_days_slot`` Jinja filter in :mod:`._base`. ``widget.js``
mutates ``.textContent`` on every cooldown-days input change.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from docutils.parsers.rst import directives

from ._base import BaseWidget
from .mcp_install import (
    COOLDOWNS,
    DEFAULT_COOLDOWN_DAYS,
    DEFAULT_COOLDOWN_ENABLED,
    DEFAULT_COOLDOWN_TYPE,
    Cooldown,
)

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from sphinx.environment import BuildEnvironment


@dataclass(frozen=True, slots=True)
class Method:
    """One install method (uvx run / pipx run / uv add / pip install)."""

    id: str
    label: str
    doc_url: str | None


@dataclass(frozen=True, slots=True)
class Panel:
    """Pre-built HTML-ready cell for one (method, cooldown) cell.

    ``usage_commands`` is a tuple so each command renders as its own
    Pygments-highlighted code block — copy-paste works one line at a
    time, matching CLAUDE.md's one-command-per-block convention.
    """

    method: Method
    cooldown: Cooldown
    install_body: str
    usage_commands: tuple[str, ...]
    note: str | None
    is_default: bool


METHODS: tuple[Method, ...] = (
    Method(
        id="uvx-run",
        label="uvx run",
        doc_url="https://docs.astral.sh/uv/guides/tools/",
    ),
    Method(
        id="pipx-run",
        label="pipx run",
        doc_url="https://pipx.pypa.io/",
    ),
    Method(
        id="uv-add",
        label="uv add",
        doc_url="https://docs.astral.sh/uv/guides/projects/",
    ),
    Method(
        id="pip",
        label="pip install",
        doc_url=None,
    ),
)


_USAGE_SUFFIXES: tuple[str, ...] = (
    'search "deploy"',
    'search --agent claude --json "deploy"',
    "find --json",
)


_DURATION_SENTINEL = "<COOLDOWN_DURATION>"
# Note: the ``<COOLDOWN_DATE>`` sentinel that mcp_install uses for pipx
# days panels is intentionally absent here. pipx and pip can't apply
# per-package cooldown overrides, so their cli-install panels fall back
# to the bare command across all cooldown modes (see ``_install_command``
# and ``_cooldown_note``). The uvx and uv add panels use the duration
# sentinel only.


# uv's ``--exclude-newer`` cutoff also filters the target package, so
# a security-conscious cooldown on the install command knocks agentgrep
# itself out of the resolver when agentgrep's most-recent release is
# newer than the cutoff (the resolver emits ``no versions of agentgrep``).
# uv's ``--exclude-newer-package <pkg>=<date>`` overrides the cutoff per
# package; setting agentgrep to a far-future date keeps the cooldown on
# transitive deps without filtering agentgrep itself.
#
# pip's ``--uploaded-prior-to`` has no per-package override — pipx-run
# and pip days panels surface that limitation in a cooldown note.
_UV_AGENTGREP_EXEMPT = "--exclude-newer-package agentgrep=2099-01-01"


def _install_command(method: Method, cooldown: Cooldown) -> str:
    """Return the install (or transient-run) command for ``(method, cooldown)``.

    Transient runners (``uvx``, ``pipx run``) carry the cooldown flag on
    every invocation. ``uv add`` and ``pip install`` carry it only on the
    install step — the resulting binary is pinned and needs no further
    flag at runtime.
    """
    if method.id == "uvx-run":
        if cooldown.id == "days":
            return (
                f"uvx --exclude-newer {_DURATION_SENTINEL} {_UV_AGENTGREP_EXEMPT} agentgrep --help"
            )
        if cooldown.id == "bypass":
            return "uvx --no-config agentgrep --help"
        return "uvx agentgrep --help"
    if method.id == "pipx-run":
        # pipx's pip backend (pip 26.0.1 in pipx 1.8.0) accepts
        # ``--uploaded-prior-to`` but has NO per-package override flag —
        # a cooldown shorter than agentgrep's most-recent-release age
        # makes the install unresolvable. pipx's ``--backend uv`` path
        # also doesn't translate ``--uploaded-prior-to`` (see pipx's
        # ``commands/run_uv.py::_UV_TRANSLATABLE_VALUE_FLAGS``). The
        # honest answer is: pipx can't do per-package cooldowns, so all
        # three modes emit the bare command. The per-cell cooldown note
        # redirects users to the uvx snippet for true cooldown support.
        return "pipx run agentgrep --help"
    if method.id == "uv-add":
        if cooldown.id == "days":
            return f"uv add --exclude-newer {_DURATION_SENTINEL} {_UV_AGENTGREP_EXEMPT} agentgrep"
        if cooldown.id == "bypass":
            return "uv add --no-config agentgrep"
        return "uv add agentgrep"
    # pip: same per-package-override limitation as pipx. All three
    # modes emit the bare install line; the note redirects to uvx.
    return "pip install --user --upgrade agentgrep"


def _usage_prefix(method: Method, cooldown: Cooldown) -> str:
    """Return the invocation prefix for ``(method, cooldown)``.

    For transient methods (``uvx``, ``pipx run``) the cooldown flag must
    sit on every usage invocation because nothing lands on ``PATH``. For
    ``uv add`` the dep is already pinned in the project venv, so usage
    runs through plain ``uv run agentgrep``. For ``pip install --user``
    the script is on the user's ``PATH``, so usage is bare ``agentgrep``.
    """
    if method.id == "uvx-run":
        if cooldown.id == "days":
            return f"uvx --exclude-newer {_DURATION_SENTINEL} {_UV_AGENTGREP_EXEMPT} agentgrep"
        if cooldown.id == "bypass":
            return "uvx --no-config agentgrep"
        return "uvx agentgrep"
    if method.id == "pipx-run":
        # pipx can't apply per-package cooldowns (see ``_install_command``).
        # Usage runs through bare ``pipx run`` regardless of cooldown mode.
        return "pipx run agentgrep"
    if method.id == "uv-add":
        return "uv run agentgrep"
    return "agentgrep"


def _cooldown_note(method: Method, cooldown: Cooldown) -> str | None:
    """Return a one-line caveat for cells where the snippet has caveats."""
    if method.id in {"pipx-run", "pip"} and cooldown.id in {"days", "bypass"}:
        # pip's ``--uploaded-prior-to`` has no per-package override (so
        # ``days`` mode would filter the target package out of the
        # resolver for fresh releases) and there's no global cooldown
        # for pip to bypass either. Both modes fall back to the bare
        # command; the note redirects users to the uvx snippet, which
        # carries ``--exclude-newer-package`` for true per-package
        # cooldown enforcement.
        return (
            "pip has no per-package cooldown override, so this snippet"
            " runs without cooldown enforcement. Switch to the `uvx run`"
            " or `uv add` tab — they apply the cooldown to transitive"
            " deps via `--exclude-newer` while exempting agentgrep"
            " itself via `--exclude-newer-package`."
        )
    return None


def build_panels() -> list[Panel]:
    """Pre-build one panel per (method, cooldown) cell.

    The first panel — ``(uvx-run, off)`` — is the default. Total panel
    count is ``len(METHODS) * len(COOLDOWNS)`` = ``4 * 3`` = 12.
    """
    panels: list[Panel] = []
    for method_index, method in enumerate(METHODS):
        for cooldown_index, cooldown in enumerate(COOLDOWNS):
            prefix = _usage_prefix(method, cooldown)
            usage = tuple(f"$ {prefix} {suffix}" for suffix in _USAGE_SUFFIXES)
            panels.append(
                Panel(
                    method=method,
                    cooldown=cooldown,
                    install_body=f"$ {_install_command(method, cooldown)}",
                    usage_commands=usage,
                    note=_cooldown_note(method, cooldown),
                    is_default=(method_index == 0 and cooldown_index == 0),
                )
            )
    return panels


DEFAULT_METHOD: str = METHODS[0].id


class CliInstallWidget(BaseWidget):
    """The ``{cli-install}`` Sphinx directive."""

    name = "cli-install"
    option_spec: t.ClassVar[cabc.Mapping[str, t.Callable[[str], t.Any]]] = {
        "variant": lambda arg: directives.choice(arg, ("full", "compact")),
    }
    default_options: t.ClassVar[cabc.Mapping[str, t.Any]] = {
        "variant": "full",
    }

    @classmethod
    def context(cls, env: BuildEnvironment) -> cabc.Mapping[str, t.Any]:
        """Provide methods, cooldowns, panels, and defaults to Jinja."""
        return {
            "methods": METHODS,
            "cooldowns": COOLDOWNS,
            "panels": build_panels(),
            "default_method": DEFAULT_METHOD,
            "default_cooldown_enabled": DEFAULT_COOLDOWN_ENABLED,
            "default_cooldown_type": DEFAULT_COOLDOWN_TYPE,
            "default_cooldown_days": DEFAULT_COOLDOWN_DAYS,
        }
