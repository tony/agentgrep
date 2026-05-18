"""Smoke tests for Sphinx widget rendering."""

from __future__ import annotations

import pathlib
import subprocess
import sys


def test_widgets_render_in_built_docs(tmp_path: pathlib.Path) -> None:
    """The mcp-install and library-install widgets render with the right class hooks."""
    repo = pathlib.Path(__file__).resolve().parents[1]
    docs = repo / "docs"
    _ = subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "dirhtml",
            "-q",
            str(docs),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # Pygments wraps whitespace in <span class="w"> ... </span>, so look for
    # token substrings that survive that wrapping rather than full phrases.
    mcp_index = (tmp_path / "mcp" / "index.html").read_text(encoding="utf-8")
    assert "ag-mcp-install" in mcp_index
    assert "claude" in mcp_index
    assert "agentgrep-mcp" in mcp_index

    library_index = (tmp_path / "library" / "index.html").read_text(encoding="utf-8")
    assert "ag-library-install" in library_index
    assert "SearchQuery" in library_index


def test_widget_assets_copied(tmp_path: pathlib.Path) -> None:
    """Widget CSS and JS land in ``_static/widgets/<name>/`` after a build."""
    repo = pathlib.Path(__file__).resolve().parents[1]
    docs = repo / "docs"
    _ = subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "dirhtml",
            "-q",
            str(docs),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    for widget in ("mcp-install", "library-install"):
        widget_dir = tmp_path / "_static" / "widgets" / widget
        assert (widget_dir / "widget.css").is_file()
        assert (widget_dir / "widget.js").is_file()
