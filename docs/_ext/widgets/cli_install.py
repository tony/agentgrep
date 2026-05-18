"""CLI install + usage picker widget.

Renders one row of install-method tabs (``uvx run`` / ``pipx run`` /
``uv add`` / ``pip install``) and, for each method, a two-block panel:
the install command and a runnable terminal usage snippet that drives
the CLI through one search and one ``--json`` invocation.

Shape mirrors :mod:`library_install`: one axis, four methods, no
cooldown/scope matrix. The package name and CLI binary are constant
across methods, so every panel shares the same ``usage_body`` — the
install command on the left differs by tool.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from docutils.parsers.rst import directives

from ._base import BaseWidget

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
    """Pre-built HTML-ready cell for one method, ready for Jinja.

    ``usage_commands`` is a tuple so each command renders as its own
    Pygments-highlighted code block — copy-paste works one line at a
    time, matching CLAUDE.md's one-command-per-block convention.
    """

    method: Method
    install_body: str
    usage_commands: tuple[str, ...]
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


def _install_command(method: Method) -> str:
    """Return the install (or run) command for ``method``."""
    if method.id == "uvx-run":
        return "uvx agentgrep --help"
    if method.id == "pipx-run":
        return "pipx run agentgrep --help"
    if method.id == "uv-add":
        return "uv add agentgrep"
    return "pip install --user --upgrade agentgrep"


def _usage_prefix(method: Method) -> str:
    """Return the invocation prefix for ``method``.

    Transient methods (``uvx``, ``pipx run``) keep the wrapper on every
    call because nothing lands on ``PATH``. ``uv add`` installs into the
    active project venv, so ``uv run`` is the explicit way to invoke
    without requiring venv activation. ``pip install --user`` puts the
    script on the user's ``PATH``, so the bare ``agentgrep`` works.
    """
    if method.id == "uvx-run":
        return "uvx agentgrep"
    if method.id == "pipx-run":
        return "pipx run agentgrep"
    if method.id == "uv-add":
        return "uv run agentgrep"
    return "agentgrep"


def build_panels() -> list[Panel]:
    """Pre-build one panel per install method, marking the first as default."""
    panels: list[Panel] = []
    is_default = True
    for method in METHODS:
        prefix = _usage_prefix(method)
        usage = tuple(f"$ {prefix} {suffix}" for suffix in _USAGE_SUFFIXES)
        panels.append(
            Panel(
                method=method,
                install_body=f"$ {_install_command(method)}",
                usage_commands=usage,
                is_default=is_default,
            )
        )
        is_default = False
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
        """Provide methods + pre-built panels to the Jinja template."""
        return {
            "methods": METHODS,
            "panels": build_panels(),
            "default_method": DEFAULT_METHOD,
        }
