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
        "sphinx_autodoc_api_style",
        "sphinx_autodoc_fastmcp",
        "docs._ext.widgets",
    ],
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
)

conf["fastmcp_tool_modules"] = ["agentgrep_fastmcp"]
conf["fastmcp_collector_mode"] = "introspect"
conf["fastmcp_area_map"] = {
    "agentgrep_fastmcp": "mcp/tools",
}
conf["fastmcp_server_module"] = "agentgrep.mcp:build_mcp_server"
conf["fastmcp_model_module"] = "agentgrep.mcp"
conf["fastmcp_model_classes"] = (
    "SearchRecordModel",
    "FindRecordModel",
    "SourceRecordModel",
    "SearchToolQuery",
    "SearchToolResponse",
    "FindToolQuery",
    "FindToolResponse",
    "BackendAvailabilityModel",
    "CapabilitiesModel",
    "SearchRequestModel",
    "FindRequestModel",
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
)
conf["fastmcp_section_badge_map"] = {
    "Search": "readonly",
    "Discovery": "readonly",
    "Catalog": "readonly",
    "Diagnostic": "readonly",
}
conf["fastmcp_section_badge_pages"] = ("mcp/tools", "mcp/index", "index")

_gp_setup = conf.pop("setup")


def setup(app: Sphinx) -> None:
    """Configure project-specific Sphinx hooks."""
    _gp_setup(app)


globals().update(conf)
