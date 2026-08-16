"""Render local terminal recordings as responsive native HTML video."""

from __future__ import annotations

import html
import pathlib
import typing as t
import urllib.parse

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.util.docutils import SphinxDirective
from sphinx.util.images import get_image_size
from sphinx.util.osutil import relative_uri

if t.TYPE_CHECKING:
    from sphinx.application import Sphinx
    from sphinx.util.typing import ExtensionMetadata
    from sphinx.writers.html5 import HTML5Translator
    from sphinx.writers.latex import LaTeXTranslator
    from sphinx.writers.manpage import ManualPageTranslator
    from sphinx.writers.texinfo import TexinfoTranslator
    from sphinx.writers.text import TextTranslator

_EXTENSION_VERSION = "1"
_STATIC_DIR = pathlib.Path(__file__).parent / "_static"


class TerminalDemoNode(nodes.General, nodes.Element):
    """A terminal recording with its poster geometry and text fallback."""


class TerminalDemoDirective(SphinxDirective):
    """Describe one local MP4 recording and its required poster image."""

    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = False
    option_spec: t.ClassVar[dict[str, t.Callable[[str], str]]] = {
        "poster": directives.unchanged_required,
        "alt": directives.unchanged_required,
    }

    def run(self) -> list[nodes.Node]:
        """Validate the assets and return one terminal demo node."""
        source_value = self.arguments[0]
        poster_value = self.options.get("poster")
        alt = self.options.get("alt")
        if poster_value is None:
            message = "terminal-demo requires :poster:"
            raise self.error(message)
        if alt is None:
            message = "terminal-demo requires :alt:"
            raise self.error(message)

        source = self._resolve_asset(source_value, option="source", suffix=".mp4")
        poster = self._resolve_asset(poster_value, option="poster", suffix=".png")
        dimensions = get_image_size(poster)
        if dimensions is None:
            message = "terminal-demo could not determine poster dimensions"
            raise self.error(message)

        self.env.note_dependency(str(source))
        self.env.note_dependency(str(poster))
        node = TerminalDemoNode(
            source_uri=self._static_uri(source),
            poster_uri=self._static_uri(poster),
            alt=alt,
            width=dimensions[0],
            height=dimensions[1],
        )
        self.set_source_info(node)
        return [node]

    def _resolve_asset(
        self,
        value: str,
        *,
        option: str,
        suffix: str,
    ) -> pathlib.Path:
        """Resolve and validate a directive asset path."""
        parsed = urllib.parse.urlsplit(value)
        candidate = pathlib.Path(value)
        if parsed.scheme or parsed.netloc or candidate.is_absolute():
            message = f"terminal-demo {option} must be a local path"
            raise self.error(message)
        if parsed.query or parsed.fragment:
            message = f"terminal-demo {option} must be a local path"
            raise self.error(message)
        if candidate.suffix.lower() != suffix:
            message = f"terminal-demo {option} must be a {suffix} file"
            raise self.error(message)

        srcdir = pathlib.Path(self.env.srcdir).resolve()
        static_dir = (srcdir / "_static").resolve()
        document = pathlib.Path(self.env.doc2path(self.env.docname)).resolve()
        resolved = (document.parent / candidate).resolve()
        if not resolved.is_relative_to(static_dir):
            message = f"terminal-demo {option} must stay under _static"
            raise self.error(message)
        if not resolved.is_file():
            message = f"terminal-demo {option} does not exist: {value}"
            raise self.error(message)
        return resolved

    def _static_uri(self, asset: pathlib.Path) -> str:
        """Return an output-root-relative URI for an asset under ``_static``."""
        srcdir = pathlib.Path(self.env.srcdir).resolve()
        return asset.relative_to(srcdir).as_posix()


def _asset_uri(translator: HTML5Translator, uri: str) -> str:
    """Return an asset URI relative to the page being written."""
    target = translator.builder.get_target_uri(translator.builder.current_docname)
    return relative_uri(target, urllib.parse.quote(uri))


def visit_terminal_demo_html(
    translator: HTML5Translator,
    node: TerminalDemoNode,
) -> None:
    """Render a terminal demo as a native, no-JavaScript video player."""
    source = html.escape(_asset_uri(translator, node["source_uri"]), quote=True)
    poster = html.escape(_asset_uri(translator, node["poster_uri"]), quote=True)
    alt = html.escape(node["alt"], quote=True)
    translator.body.append(
        '<figure class="agentgrep-terminal-demo">'
        '<video class="agentgrep-terminal-demo__video" controls playsinline '
        f'preload="none" width="{node["width"]}" height="{node["height"]}" '
        f'poster="{poster}" aria-label="{alt}">'
        f'<source src="{source}" type="video/mp4">'
        f'<a href="{source}">{alt}</a>'
        "</video>"
        "</figure>"
    )
    raise nodes.SkipNode


def _terminal_demo_fallback(node: TerminalDemoNode) -> str:
    """Return the readable fallback used by non-HTML builders."""
    return f"Terminal demo: {node['alt']} (video: {node['source_uri']})"


def visit_terminal_demo_text(
    translator: TextTranslator,
    node: TerminalDemoNode,
) -> None:
    """Render a terminal demo as descriptive text."""
    translator.add_text(_terminal_demo_fallback(node))
    raise nodes.SkipNode


def visit_terminal_demo_man(
    translator: ManualPageTranslator,
    node: TerminalDemoNode,
) -> None:
    """Render a terminal demo in a manual page."""
    translator.body.append(_terminal_demo_fallback(node))
    raise nodes.SkipNode


def visit_terminal_demo_latex(
    translator: LaTeXTranslator,
    node: TerminalDemoNode,
) -> None:
    """Render a terminal demo in LaTeX output."""
    translator.body.append(translator.encode(_terminal_demo_fallback(node)))
    raise nodes.SkipNode


def visit_terminal_demo_texinfo(
    translator: TexinfoTranslator,
    node: TerminalDemoNode,
) -> None:
    """Render a terminal demo in Texinfo output."""
    translator.body.append(translator.escape(_terminal_demo_fallback(node)))
    raise nodes.SkipNode


def _add_static_path(app: Sphinx) -> None:
    """Publish the extension stylesheet with the documentation assets."""
    static_dir = str(_STATIC_DIR)
    if static_dir not in app.config.html_static_path:
        app.config.html_static_path.append(static_dir)


def setup(app: Sphinx) -> ExtensionMetadata:
    """Register the terminal demo directive, node, and stylesheet."""
    app.add_node(
        TerminalDemoNode,
        html=(visit_terminal_demo_html, None),
        text=(visit_terminal_demo_text, None),
        man=(visit_terminal_demo_man, None),
        latex=(visit_terminal_demo_latex, None),
        texinfo=(visit_terminal_demo_texinfo, None),
    )
    app.add_directive("terminal-demo", TerminalDemoDirective)
    app.connect("builder-inited", _add_static_path)
    app.add_css_file("css/terminal-demo.css")
    return {
        "version": _EXTENSION_VERSION,
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
