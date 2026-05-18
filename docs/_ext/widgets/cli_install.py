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
_DATE_SENTINEL = "<COOLDOWN_DATE>"


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
        if cooldown.id == "days":
            return f"pipx run --pip-args=--uploaded-prior-to={_DATE_SENTINEL} agentgrep --help"
        # off + bypass — pipx default backend (pip) has no global cooldown
        return "pipx run agentgrep --help"
    if method.id == "uv-add":
        if cooldown.id == "days":
            return f"uv add --exclude-newer {_DURATION_SENTINEL} {_UV_AGENTGREP_EXEMPT} agentgrep"
        if cooldown.id == "bypass":
            return "uv add --no-config agentgrep"
        return "uv add agentgrep"
    # pip
    if cooldown.id == "days":
        return f"pip install --user --upgrade --uploaded-prior-to {_DURATION_SENTINEL} agentgrep"
    # off + bypass — pip has no global cooldown to skip
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
        if cooldown.id == "days":
            return f"pipx run --pip-args=--uploaded-prior-to={_DATE_SENTINEL} agentgrep"
        return "pipx run agentgrep"
    if method.id == "uv-add":
        return "uv run agentgrep"
    return "agentgrep"


def _cooldown_note(method: Method, cooldown: Cooldown) -> str | None:
    """Return a one-line caveat for cells where the snippet has caveats."""
    if cooldown.id == "days" and method.id in {"pipx-run", "pip"}:
        # pip's --uploaded-prior-to is a global cutoff with no
        # per-package override, so a cooldown shorter than agentgrep's
        # most-recent-release age makes the install unresolvable. uv
        # handles this via --exclude-newer-package on the uvx-run /
        # uv-add panels (see _install_command).
        return (
            "pip's `--uploaded-prior-to` is a global cutoff with no"
            " per-package override. If the cooldown filters out a recent"
            " release of agentgrep itself, switch to the `uv add` or"
            " `uvx run` snippets — they exempt agentgrep via"
            " `--exclude-newer-package`."
        )
    if cooldown.id == "bypass":
        if method.id == "pipx-run":
            return (
                "pipx's default backend (pip) has no global cooldown."
                " For uv-style cooldown control, install `pipx[uv]` and set"
                " `UV_NO_CONFIG=1` in your shell."
            )
        if method.id == "pip":
            return (
                "pip has no global cooldown, so bypass is a no-op. Use this"
                " when pairing the snippet with a uv-backed parent command."
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
