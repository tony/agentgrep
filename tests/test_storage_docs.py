"""Tests for the Sphinx storage catalogue extension."""

from __future__ import annotations

import io
import pathlib
import re
import textwrap
import typing as t

import pytest
from sphinx.application import Sphinx

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _build_sphinx(srcdir: pathlib.Path, outdir: pathlib.Path) -> tuple[Sphinx, str]:
    """Build a tiny Sphinx project and return the app plus warnings."""
    outdir.mkdir()
    doctreedir = outdir / ".doctrees"
    doctreedir.mkdir()
    warning = io.StringIO()
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(outdir / "html"),
        doctreedir=str(doctreedir),
        buildername="dirhtml",
        freshenv=True,
        warning=warning,
        status=io.StringIO(),
    )
    app.build()
    return app, warning.getvalue()


def _root_html(outdir: pathlib.Path) -> str:
    """Return the built root page HTML."""
    return (outdir / "html" / "index.html").read_text(encoding="utf-8")


def test_storage_badge_group_uses_coverage_and_type_badges() -> None:
    """Storage badge groups use the shared gp-sphinx badge primitives."""
    from sphinx_ux_badges import BadgeNode

    from docs._ext.storages._badges import build_store_badge_group

    group = build_store_badge_group("default_search")

    assert "gp-sphinx-badge-group" in group["classes"]
    badges = list(group.findall(BadgeNode))
    assert [badge.astext() for badge in badges] == ["default", "store"]
    assert "gp-sphinx-storage__coverage-default-search" in badges[0]["classes"]


def test_storage_badge_css_uses_shared_badge_color_variables() -> None:
    """Storage badge classes map onto gp-sphinx badge color tokens."""
    css = pathlib.Path("docs/_ext/storages/_static/css/storage.css").read_text(encoding="utf-8")

    assert ".gp-sphinx-storage__coverage-default-search" in css
    assert ".gp-sphinx-storage__type-store" in css
    assert "--gp-sphinx-badge-bg" in css
    assert "--gp-sphinx-badge-fg" in css
    assert "--gp-sphinx-badge-border" in css
    assert "--sab-color" not in css


def test_storage_domain_registers_and_resolves_store_targets(tmp_path: pathlib.Path) -> None:
    """Generated store targets resolve through the custom storage domain."""
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
            extensions = ["myst_parser", "docs._ext.storages"]
            myst_enable_extensions = ["colon_fence"]
            """
        ),
        encoding="utf-8",
    )
    (srcdir / "index.md").write_text(
        textwrap.dedent(
            """\
            # Storage docs

            Use {storage:store}`claude.history` beside {storage:storeref}`claude.projects.session`.

            ```{storage:agent} claude
            ```
            """
        ),
        encoding="utf-8",
    )

    app, warnings = _build_sphinx(srcdir, tmp_path / "build")
    html = _root_html(tmp_path / "build")

    domain = app.env.get_domain("storage")
    objects = list(domain.get_objects())
    assert any(obj[0] == "claude.history" for obj in objects)
    assert 'id="storage-store-claude-history"' in html
    assert 'href="#storage-store-claude-history"' in html
    assert "gp-sphinx-api-card-entry" in html
    assert "gp-sphinx-storage__store-index" in html
    assert "gp-sphinx-storage__store-index-card" in html
    assert "gp-sphinx-storage__key-value" in html
    assert "gp-sphinx-storage__coverage-default-search" in html
    assert '<table class="gp-sphinx-storage__table' not in html
    assert "undefined label" not in warnings


def test_storage_coverage_grid_summarizes_catalog(tmp_path: pathlib.Path) -> None:
    """The generated coverage grid exposes catalog coverage by backend."""
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
            extensions = ["myst_parser", "docs._ext.storages"]
            myst_enable_extensions = ["colon_fence"]
            """
        ),
        encoding="utf-8",
    )
    (srcdir / "index.md").write_text(
        textwrap.dedent(
            """\
            # Backend coverage

            ```{toctree}
            :maxdepth: 1

            claude
            codex
            cursor-cli
            cursor-ide
            gemini
            grok
            pi
            ```

            ```{storage:coverage-grid}
            ```
            """
        ),
        encoding="utf-8",
    )
    for agent in ("claude", "codex", "cursor-cli", "cursor-ide", "gemini", "grok", "pi"):
        (srcdir / f"{agent}.md").write_text(
            textwrap.dedent(
                f"""\
                # {agent.title()}

                ```{{storage:agent}} {agent}
                ```
                """
            ),
            encoding="utf-8",
        )

    _app, warnings = _build_sphinx(srcdir, tmp_path / "build")
    html = _root_html(tmp_path / "build")

    assert "gp-sphinx-storage__support-matrix" in html
    assert "gp-sphinx-storage__support-agent-card" in html
    assert "gp-sphinx-storage__store-link-list" in html
    assert "gp-sphinx-storage__store-link-item" in html
    assert "Codex" in html
    assert "Default search" in html
    assert "Runtime / cache / private" in html
    assert "codex.history" in html
    assert "claude.history" in html
    assert 'href="claude/#storage-store-claude-history"' in html
    assert 'href="codex/#storage-store-codex-history"' in html
    assert 'href="#storage-store-claude-history"' not in html
    assert 'href="#storage-store-codex-history"' not in html
    assert '<table class="gp-sphinx-storage__table' not in html
    assert "gp-sphinx-storage__coverage-grid" not in html
    assert "undefined label" not in warnings


def test_storage_catalog_summary_uses_nested_key_value_cards(
    tmp_path: pathlib.Path,
) -> None:
    """The generated catalog summary uses nested key/value cards."""
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
            extensions = ["myst_parser", "docs._ext.storages"]
            myst_enable_extensions = ["colon_fence"]
            """
        ),
        encoding="utf-8",
    )
    (srcdir / "index.md").write_text(
        textwrap.dedent(
            """\
            # Catalog summary

            ```{storage:catalog-summary}
            ```
            """
        ),
        encoding="utf-8",
    )

    _app, warnings = _build_sphinx(srcdir, tmp_path / "build")
    html = _root_html(tmp_path / "build")

    assert "gp-sphinx-storage__catalog-summary" in html
    assert "gp-sphinx-storage__catalog-summary-card" in html
    assert "gp-sphinx-storage__key-value" in html
    assert "By coverage" in html
    assert "default_search" in html
    assert '<table class="gp-sphinx-storage__table' not in html
    assert "undefined label" not in warnings


class DescriptionMarkupCase(t.NamedTuple):
    """Parametrized case for store-card description markup rendering."""

    test_id: str
    token: str


DESCRIPTION_MARKUP_CASES: tuple[DescriptionMarkupCase, ...] = (
    DescriptionMarkupCase(test_id="schema-notes-inline-code", token="RolloutItem"),
    DescriptionMarkupCase(test_id="search-notes-inline-code", token="codex.history"),
)


@pytest.mark.parametrize(
    "case",
    DESCRIPTION_MARKUP_CASES,
    ids=[case.test_id for case in DESCRIPTION_MARKUP_CASES],
)
def test_store_card_description_renders_markdown(
    case: DescriptionMarkupCase, tmp_path: pathlib.Path
) -> None:
    """Single-backtick Markdown in a store description renders as inline code."""
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
            extensions = ["myst_parser", "docs._ext.storages"]
            myst_enable_extensions = ["colon_fence"]
            """
        ),
        encoding="utf-8",
    )
    (srcdir / "index.md").write_text(
        textwrap.dedent(
            """\
            # Codex sessions

            ```{storage:store} codex.sessions
            ```
            """
        ),
        encoding="utf-8",
    )

    _app, warnings = _build_sphinx(srcdir, tmp_path / "build")
    html = _root_html(tmp_path / "build")

    token = re.escape(case.token)
    assert re.search(rf"<code[^>]*>(?:<span[^>]*>)?{token}", html), html
    assert f"`{case.token}`" not in html
    assert "undefined label" not in warnings
