"""Sphinx configuration for agentgrep."""

from __future__ import annotations

import pathlib
import sys
import tomllib
import typing as t

from gp_sphinx.config import merge_sphinx_config

if t.TYPE_CHECKING:
    from sphinx.application import Sphinx

cwd = pathlib.Path(__file__).parent
project_root = cwd.parent
project_src = project_root / "src"

sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_src))
sys.path.insert(0, str(cwd / "_ext"))

project_metadata = tomllib.loads((project_root / "pyproject.toml").read_text())["project"]

conf = merge_sphinx_config(
    project=project_metadata["name"],
    version=project_metadata["version"],
    copyright="2026, Tony Narlock",
    source_repository="https://github.com/tony/agentgrep/",
    docs_url="https://agentgrep.org/",
    source_branch="master",
    light_logo="img/icons/logo.svg",
    dark_logo="img/icons/logo-dark.svg",
    html_favicon="_static/favicon.ico",
    html_extra_path=["manifest.json"],
    extra_extensions=[
        "sphinx.ext.doctest",
        "sphinx_autodoc_api_style",
        "sphinx_autodoc_argparse",
        "sphinx_autodoc_fastmcp",
        "sphinx_gp_mermaid",
        "sphinx_gp_highlighting",
        "docs._ext.storages",
        "docs._ext.widgets",
        "docs._ext.lexers",
    ],
    gp_highlighting_inline_literals="safe",
    gp_highlighting_inline_commands=["agentgrep"],
    myst_fence_as_directive=["mermaid"],
    intersphinx_mapping={
        "python": ("https://docs.python.org/3/", None),
        "pydantic": ("https://docs.pydantic.dev/latest/", None),
    },
    rediraffe_redirects="redirects.txt",
    copybutton_selector="div.highlight pre, div.admonition.prompt > p:last-child",
    copybutton_exclude=".linenos, .admonition-title",
    theme_options={
        "announcement": (
            "<em>Pre-alpha.</em> APIs may change. "
            "<a href='https://github.com/tony/agentgrep/issues'>Feedback welcome</a>."
        ),
    },
    # AGENTS.md is agent guidance, not a site page; keep Sphinx from
    # treating it as an orphan document. Keep demo sources and alternate
    # renders out of the static copy while publishing the linked MP4 files.
    exclude_patterns=[
        "_build",
        "node_modules",
        "_mermaid_cache",
        "AGENTS.md",
        "CLAUDE.md",
        "demos/asciinema/**",
        "demos/posters/**",
        "demos/vhs/*.gif",
        "demos/vhs/*.tape",
        "demos/vhs/*.webm",
        "demos/*.py",
        "demos/*.sh",
    ],
)

conf["fastmcp_tool_modules"] = ["agentgrep_fastmcp"]
conf["fastmcp_collector_mode"] = "introspect"
conf["fastmcp_area_map"] = {
    "agentgrep_fastmcp": "mcp/tools",
}
conf["fastmcp_server_module"] = "agentgrep.mcp:build_mcp_server"
conf["fastmcp_model_module"] = "agentgrep.mcp"
conf["fastmcp_model_classes"] = (
    "AgentGrepModel",
    "SearchRecordModel",
    "FindRecordModel",
    "SourceVersionDetectionModel",
    "SourceRecordModel",
    "SearchRequestModel",
    "SearchToolResponse",
    "FindRequestModel",
    "FindToolResponse",
    "ResultStatsModel",
    "PageInfoModel",
    "RunStatusModel",
    "DiagnosticModel",
    "BackendAvailabilityModel",
    "CapabilitiesModel",
    "StoreDescriptorModel",
    "ListStoresRequest",
    "ListStoresResponse",
    "GetStoreDescriptorRequest",
    "ListSourcesRequest",
    "ListSourcesResponse",
    "FilterSourcesRequest",
    "DiscoverySummaryRequest",
    "DiscoverySummaryResponse",
    "ValidateQueryRequest",
    "ValidateQueryResponse",
    "RecentSessionsRequest",
    "RecentSessionsResponse",
    "InspectSampleRequest",
    "InspectSampleResponse",
    "InspectResultRequest",
    "InspectResultResponse",
)
conf["fastmcp_section_badge_map"] = {
    "Search": "readonly",
    "Discovery": "readonly",
    "Catalog": "readonly",
    "Diagnostic": "readonly",
}
conf["fastmcp_section_badge_pages"] = ("mcp/tools", "mcp/index", "index")
conf["doctest_global_setup"] = "\n".join(
    (
        "import pathlib",
        "from agentgrep import format_timestamp_tig",
        "from agentgrep.store_catalog import gemini_project_hash",
    )
)

# IBM Plex Mono 400 italic shows up on every page that has a syntax-
# highlighted code block — Furo's Pygments style renders comment tokens
# (.c / .c1 / .cm) italic, so every example with a `# comment` line
# triggers a fetch. gp-sphinx's DEFAULT_SPHINX_FONT_PRELOAD covers Mono
# 400/700 normal and Sans 400 italic, but not Mono italic; append it so
# the browser fetches the face in parallel with the critical CSS
# instead of lazy-loading on first encounter.
conf["sphinx_font_preload"].append(("IBM Plex Mono", 400, "italic"))

_gp_setup = conf.pop("setup")


def setup(app: Sphinx) -> None:
    """Configure project-specific Sphinx hooks."""
    _gp_setup(app)
    app.add_js_file("js/prompt-copy.js", loading_method="defer")
    app.add_css_file("css/project-admonitions.css")
    app.add_css_file("css/project-cards.css")


globals().update(conf)
