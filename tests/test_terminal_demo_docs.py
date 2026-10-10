"""Tests for the Sphinx terminal demo directive."""

from __future__ import annotations

import io
import pathlib
import shutil
import textwrap
import typing as t

import pytest
from sphinx.application import Sphinx

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_POSTER_FIXTURE = _REPO_ROOT / "docs/_static/demos/posters/agentgrep-search.png"


def _write_project(
    srcdir: pathlib.Path,
    *,
    source: str = "../_static/demo.mp4",
    poster: str | None = "../_static/poster.png",
    alt: str | None = "Search agent history from the terminal",
) -> None:
    """Write a minimal MyST project containing one terminal demo."""
    srcdir.mkdir()
    (srcdir / "_static").mkdir()
    (srcdir / "guide").mkdir()
    (srcdir / "_static" / "demo.mp4").write_bytes(b"terminal demo")
    shutil.copyfile(_POSTER_FIXTURE, srcdir / "_static" / "poster.png")
    (srcdir / "conf.py").write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            extensions = ["myst_parser", "docs._ext.terminal_demo"]
            html_static_path = ["_static"]
            """
        ),
        encoding="utf-8",
    )
    (srcdir / "index.md").write_text(
        textwrap.dedent(
            """\
            # Demo docs

            ```{toctree}
            guide/index
            ```
            """
        ),
        encoding="utf-8",
    )
    options = ""
    if poster is not None:
        options += f":poster: {poster}\n"
    if alt is not None:
        options += f":alt: {alt}\n"
    (srcdir / "guide" / "index.md").write_text(
        f"# Terminal demo\n\n```{{terminal-demo}} {source}\n{options}```\n",
        encoding="utf-8",
    )


def _build_sphinx(
    srcdir: pathlib.Path,
    outdir: pathlib.Path,
    *,
    builder: str = "dirhtml",
) -> tuple[Sphinx, str]:
    """Build a tiny Sphinx project and return the app plus warnings."""
    outdir.mkdir()
    warning = io.StringIO()
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(outdir / builder),
        doctreedir=str(outdir / ".doctrees"),
        buildername=builder,
        freshenv=True,
        warning=warning,
        status=io.StringIO(),
    )
    app.build()
    return app, warning.getvalue()


def _without_registration_warnings(warnings: str) -> str:
    """Remove Docutils' process-global re-registration noise between test apps."""
    return "\n".join(
        line
        for line in warnings.splitlines()
        if "already registered" not in line and "will not be overridden" not in line
    )


def test_terminal_demo_renders_intrinsic_responsive_video(
    tmp_path: pathlib.Path,
) -> None:
    """HTML reserves poster geometry and keeps a usable fallback link."""
    srcdir = tmp_path / "src"
    _write_project(srcdir)

    app, warnings = _build_sphinx(srcdir, tmp_path / "build")
    html = (tmp_path / "build/dirhtml/guide/index.html").read_text(encoding="utf-8")

    assert _without_registration_warnings(warnings) == ""
    assert '<video class="agentgrep-terminal-demo__video"' in html
    assert " controls" in html
    assert " playsinline" in html
    assert ' preload="none"' in html
    assert ' width="1200"' in html
    assert ' height="420"' in html
    assert ' poster="../_static/poster.png"' in html
    assert ' aria-label="Search agent history from the terminal"' in html
    assert '<source src="../_static/demo.mp4" type="video/mp4">' in html
    assert 'href="../_static/demo.mp4"' in html
    assert "Search agent history from the terminal" in html
    assert (tmp_path / "build/dirhtml/_static/demo.mp4").is_file()
    assert (tmp_path / "build/dirhtml/_static/poster.png").is_file()
    assert (tmp_path / "build/dirhtml/_static/css/terminal-demo.css").is_file()

    dependencies = {pathlib.Path(path).name for path in app.env.dependencies["guide/index"]}
    assert {"demo.mp4", "poster.png"} <= dependencies


def test_terminal_demo_has_text_builder_fallback(tmp_path: pathlib.Path) -> None:
    """Non-HTML output identifies the demo and preserves its video path."""
    srcdir = tmp_path / "src"
    _write_project(srcdir)

    _app, warnings = _build_sphinx(
        srcdir,
        tmp_path / "build",
        builder="text",
    )
    text = (tmp_path / "build/text/guide/index.txt").read_text(encoding="utf-8")

    assert _without_registration_warnings(warnings) == ""
    assert "Terminal demo: Search agent history from the terminal" in text
    assert "_static/demo.mp4" in text


class InvalidAssetCase(t.NamedTuple):
    """One invalid terminal-demo asset configuration."""

    test_id: str
    source: str
    poster: str | None
    alt: str | None
    warning: str


INVALID_ASSET_CASES = (
    InvalidAssetCase(
        "remote-video",
        "https://example.com/demo.mp4",
        "../_static/poster.png",
        "Search agent history from the terminal",
        "terminal-demo source must be a local path",
    ),
    InvalidAssetCase(
        "outside-static",
        "../../outside.mp4",
        "../_static/poster.png",
        "Search agent history from the terminal",
        "terminal-demo source must stay under _static",
    ),
    InvalidAssetCase(
        "missing-video",
        "../_static/missing.mp4",
        "../_static/poster.png",
        "Search agent history from the terminal",
        "terminal-demo source does not exist",
    ),
    InvalidAssetCase(
        "missing-poster-option",
        "../_static/demo.mp4",
        None,
        "Search agent history from the terminal",
        "terminal-demo requires :poster:",
    ),
    InvalidAssetCase(
        "missing-alt-option",
        "../_static/demo.mp4",
        "../_static/poster.png",
        None,
        "terminal-demo requires :alt:",
    ),
)


@pytest.mark.parametrize(
    "case",
    INVALID_ASSET_CASES,
    ids=[case.test_id for case in INVALID_ASSET_CASES],
)
def test_terminal_demo_rejects_invalid_assets(
    case: InvalidAssetCase,
    tmp_path: pathlib.Path,
) -> None:
    """Invalid or incomplete demo declarations produce actionable diagnostics."""
    srcdir = tmp_path / "src"
    _write_project(
        srcdir,
        source=case.source,
        poster=case.poster,
        alt=case.alt,
    )

    _app, warnings = _build_sphinx(srcdir, tmp_path / "build")

    assert case.warning in warnings


def test_terminal_demo_rejects_unsized_poster(tmp_path: pathlib.Path) -> None:
    """A corrupt poster cannot provide the intrinsic video geometry."""
    srcdir = tmp_path / "src"
    _write_project(srcdir)
    (srcdir / "_static/poster.png").write_bytes(b"not a png")

    _app, warnings = _build_sphinx(srcdir, tmp_path / "build")

    assert "terminal-demo could not determine poster dimensions" in warnings
