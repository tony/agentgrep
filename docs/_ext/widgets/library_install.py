"""Library install + quickstart picker widget.

Renders one row of install-method tabs and, for each method, a panel
pairing a code block with the matching Python quickstart.

Methods cover the three ways to consume agentgrep as a Python library:

- ``uv-script`` — a single-file script with PEP 723 inline metadata
  declaring ``agentgrep`` as a dependency. ``uv run example.py``
  resolves and runs in an ephemeral environment. The canonical
  "try it in one file" shape for uv-native users.
- ``uv-add`` — adds ``agentgrep`` to the active project's
  ``pyproject.toml`` so it installs into the project venv.
- ``pip`` — traditional ``pip install`` into the active environment.

``uvx`` and ``pipx run`` are deliberately omitted — they're tool
runners (one-shot CLI invocations), not library-install patterns. For
the CLI surface see :mod:`cli_install`.
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
    """One library-consumption method.

    ``doc_url`` is the upstream docs link for the tool driving the
    method. The Jinja template links to it in the panel preamble so
    users can dive into the tool's own documentation.
    """

    id: str
    label: str
    doc_url: str | None


@dataclass(frozen=True, slots=True)
class Panel:
    """Pre-built HTML-ready cell for one method, ready for Jinja.

    The two code blocks render with different Pygments lexers
    (``console`` for shell, ``python`` for code), so each panel carries
    the language alongside the body. ``a`` is the first block (the
    install command or the inline-metadata script), ``b`` is the
    second (the Python quickstart or the run command).
    """

    method: Method
    code_a_body: str
    code_a_lang: str
    code_b_body: str
    code_b_lang: str
    is_default: bool


METHODS: tuple[Method, ...] = (
    Method(
        id="uv-script",
        label="uv script",
        doc_url="https://docs.astral.sh/uv/guides/scripts/",
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


_QUICKSTART_BODY = """from pathlib import Path

import agentgrep

backends = agentgrep.select_backends()
query = agentgrep.SearchQuery(
    terms=("hello",),
    scope="all",
    any_term=False,
    regex=False,
    case_sensitive=False,
    agents=agentgrep.AGENT_CHOICES,
    limit=10,
)
for record in agentgrep.run_search_query(Path.home(), query, backends=backends):
    print(record.agent, record.title or record.path)
"""


_PEP723_SCRIPT = f"""# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "agentgrep",
# ]
# ///

{_QUICKSTART_BODY}"""


def build_panels() -> list[Panel]:
    """Pre-build one panel per install method, marking the first as default."""
    panels: list[Panel] = []
    is_default = True
    for method in METHODS:
        if method.id == "uv-script":
            code_a_body = _PEP723_SCRIPT
            code_a_lang = "python"
            code_b_body = "$ uv run example.py"
            code_b_lang = "console"
        elif method.id == "uv-add":
            code_a_body = "$ uv add agentgrep"
            code_a_lang = "console"
            code_b_body = _QUICKSTART_BODY
            code_b_lang = "python"
        else:  # pip
            code_a_body = "$ pip install --user --upgrade agentgrep"
            code_a_lang = "console"
            code_b_body = _QUICKSTART_BODY
            code_b_lang = "python"
        panels.append(
            Panel(
                method=method,
                code_a_body=code_a_body,
                code_a_lang=code_a_lang,
                code_b_body=code_b_body,
                code_b_lang=code_b_lang,
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
