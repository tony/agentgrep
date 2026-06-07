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

    for widget in ("mcp-install", "library-install", "cli-install"):
        widget_dir = tmp_path / "_static" / "widgets" / widget
        assert (widget_dir / "widget.css").is_file()
        assert (widget_dir / "widget.js").is_file()


def test_cli_install_panel_matrix() -> None:
    """``build_panels`` emits one panel per (method, cooldown) cell."""
    import sys as _sys

    repo = pathlib.Path(__file__).resolve().parents[1]
    docs_root = str(repo)
    if docs_root not in _sys.path:
        _sys.path.insert(0, docs_root)

    from docs._ext.widgets import cli_install
    from docs._ext.widgets.mcp_install import COOLDOWNS

    panels = cli_install.build_panels()

    # 4 methods x 3 cooldowns = 12 panels.
    assert len(panels) == len(cli_install.METHODS) * len(COOLDOWNS)

    # Every (method, cooldown) pair appears exactly once.
    seen = {(panel.method.id, panel.cooldown.id) for panel in panels}
    assert len(seen) == len(panels)

    # The default panel is (uvx-run, off).
    default_panels = [panel for panel in panels if panel.is_default]
    assert len(default_panels) == 1
    assert default_panels[0].method.id == "uvx-run"
    assert default_panels[0].cooldown.id == "off"

    # Cooldown notes appear on bypass cells for pipx-run and pip, and on
    # days cells for pipx-run and pip (the pip backends have no
    # per-package exemption flag, unlike uv).
    notes = {(panel.method.id, panel.cooldown.id): panel.note for panel in panels}
    assert notes[("pipx-run", "bypass")] is not None
    assert notes[("pip", "bypass")] is not None
    assert notes[("pipx-run", "days")] is not None
    assert notes[("pip", "days")] is not None
    assert notes[("uvx-run", "bypass")] is None
    assert notes[("uv-add", "bypass")] is None
    assert notes[("uvx-run", "days")] is None
    assert notes[("uv-add", "days")] is None
    for method_id in ("uvx-run", "pipx-run", "uv-add", "pip"):
        assert notes[(method_id, "off")] is None

    # Only uv-flavored days panels embed the duration sentinel —
    # pipx-run / pip days panels fall back to the bare command because
    # pip has no per-package cooldown override (see _cooldown_note).
    for method_id in ("uvx-run", "uv-add"):
        panel = next(p for p in panels if p.method.id == method_id and p.cooldown.id == "days")
        assert "<COOLDOWN_DURATION>" in panel.install_body
        assert "--exclude-newer-package agentgrep=" in panel.install_body

    # pipx-run / pip never embed a cooldown sentinel: all three modes
    # (off / days / bypass) emit the same bare install command.
    for method_id in ("pipx-run", "pip"):
        bodies = {p.cooldown.id: p.install_body for p in panels if p.method.id == method_id}
        assert bodies["off"] == bodies["days"] == bodies["bypass"]
        assert "<COOLDOWN_DURATION>" not in bodies["days"]
        assert "<COOLDOWN_DATE>" not in bodies["days"]
        assert "--uploaded-prior-to" not in bodies["days"]
        assert "--pip-args" not in bodies["days"]


def test_backend_index_renders_backend_shortcut_grid(tmp_path: pathlib.Path) -> None:
    """The backend index links directly to each backend page near the top."""
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

    backend_index = (tmp_path / "backends" / "index.html").read_text(encoding="utf-8")
    assert "Backend pages" in backend_index
    assert backend_index.index("Backend pages") < backend_index.index("Coverage levels")
    for backend in (
        "codex",
        "claude",
        "cursor-cli",
        "cursor-ide",
        "gemini",
        "grok",
        "pi",
        "opencode",
    ):
        assert f'href="{backend}/"' in backend_index


def test_cli_docs_render_one_page_per_command_group(tmp_path: pathlib.Path) -> None:
    """CLI docs expose separate argparse pages for each command group."""
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

    cli_index = (tmp_path / "cli" / "index.html").read_text(encoding="utf-8")
    assert "db-insights" not in cli_index
    for href in (
        'href="search/"',
        'href="ui/"',
        'href="db/"',
        'href="insights/"',
        'href="suggestions/"',
    ):
        assert href in cli_index

    for page in (
        tmp_path / "cli" / "db" / "sync" / "index.html",
        tmp_path / "cli" / "db" / "status" / "index.html",
        tmp_path / "cli" / "db" / "explain" / "index.html",
        tmp_path / "cli" / "insights" / "analyze" / "index.html",
        tmp_path / "cli" / "insights" / "list" / "index.html",
        tmp_path / "cli" / "insights" / "explain" / "index.html",
        tmp_path / "cli" / "suggestions" / "list" / "index.html",
        tmp_path / "cli" / "suggestions" / "show" / "index.html",
        tmp_path / "cli" / "suggestions" / "render" / "index.html",
    ):
        assert page.is_file()
