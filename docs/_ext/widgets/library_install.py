"""Library install + quickstart picker widget.

Renders one row of install-method tabs (``uvx run`` / ``pipx run`` /
``uv add`` / ``pip install``) and, for each method, a two-block panel:
the install command and a runnable Python quickstart that drives the
library through one search.

The cooldown / scope axes used by :mod:`mcp_install` don't apply here.
Library users are pulling the package into their own project; their
cooldown policy is whatever uv / pipx / pip is configured with, and
there is no ``mcpServers.<slug>`` registration target.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

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
    """Pre-built HTML-ready cell for one method, ready for Jinja."""

    method: Method
    install_body: str
    quickstart_body: str
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


_QUICKSTART = """from pathlib import Path

import agentgrep

backends = agentgrep.select_backends()
query = agentgrep.SearchQuery(
    terms=("hello",),
    search_type="all",
    any_term=False,
    regex=False,
    case_sensitive=False,
    agents=agentgrep.AGENT_CHOICES,
    limit=10,
)
for record in agentgrep.run_search_query(Path.home(), query, backends=backends):
    print(record.agent, record.title or record.path)
"""


def _install_command(method: Method) -> str:
    """Return the install command for ``method``."""
    if method.id == "uvx-run":
        return "uvx agentgrep --help"
    if method.id == "pipx-run":
        return "pipx run agentgrep --help"
    if method.id == "uv-add":
        return "uv add agentgrep"
    return "pip install --user --upgrade agentgrep"


def build_panels() -> list[Panel]:
    """Pre-build one panel per install method, marking the first as default."""
    panels: list[Panel] = []
    is_default = True
    for method in METHODS:
        panels.append(
            Panel(
                method=method,
                install_body=f"$ {_install_command(method)}",
                quickstart_body=_QUICKSTART,
                is_default=is_default,
            )
        )
        is_default = False
    return panels


DEFAULT_METHOD: str = METHODS[0].id


class LibraryInstallWidget(BaseWidget):
    """The ``{library-install}`` Sphinx directive."""

    name = "library-install"
    option_spec: t.ClassVar[cabc.Mapping[str, t.Callable[[str], t.Any]]] = {}
    default_options: t.ClassVar[cabc.Mapping[str, t.Any]] = {}

    @classmethod
    def context(cls, env: BuildEnvironment) -> cabc.Mapping[str, t.Any]:
        """Provide methods + pre-built panels to the Jinja template."""
        return {
            "methods": METHODS,
            "panels": build_panels(),
            "default_method": DEFAULT_METHOD,
        }
