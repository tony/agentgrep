"""Documentation contract tests for portable record export."""

from __future__ import annotations

import inspect
import pathlib
import re
import typing as t

import pytest
from pydantic import TypeAdapter

from agentgrep.mcp import ExportRecordsResponse

pytestmark = pytest.mark.documentation

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read_text(relative_path: str) -> str:
    """Return one tracked documentation file's text."""
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"missing documentation contract: {relative_path}"
    return path.read_text(encoding="utf-8")


def _missing_terms(text: str, required: tuple[str, ...]) -> tuple[str, ...]:
    """Return required prose terms missing after Markdown line wrapping."""
    normalized = re.sub(r"\s+", " ", text).casefold()
    return tuple(
        term for term in required if re.sub(r"\s+", " ", term).casefold() not in normalized
    )


def _markdown_section(text: str, heading: str) -> str:
    """Return the body below one named Markdown heading of the same level."""
    marker = f"{heading}\n"
    _, found, remainder = text.partition(marker)
    assert found, f"missing Markdown section: {heading}"
    level = heading.split(maxsplit=1)[0]
    return remainder.partition(f"\n{level} ")[0]


def test_export_docs_are_indexed() -> None:
    """The CLI guide and ADR are reachable through their public indexes."""
    cli_index = _read_text("docs/cli/index.md")
    adr_index = _read_text("docs/dev/adr/index.md")
    cli_guide = _read_text("docs/cli/export.md")
    adr = _read_text("docs/dev/adr/0017-portable-record-export.md")

    assert re.search(r"(?m)^export$", cli_index)
    assert "0017-portable-record-export" in adr_index
    assert "(cli-export)=" in cli_guide
    assert "(adr-portable-record-export)=" in adr


def test_export_changelog_clarifies_focused_input_key() -> None:
    """The shortcut copy scopes ordinary ``e`` text to focused inputs."""
    changes = _read_text("CHANGES")

    assert "`e` remains ordinary text when an input is focused." in changes


def test_export_cli_docs_define_defaults_and_safe_sinks() -> None:
    """The headless guide names exact formats, bounds, bodies, and sinks."""
    guide = _read_text("docs/cli/export.md")
    required = (
        "exactly two formats",
        "`ndjson`",
        "`markdown`",
        "default format is `ndjson`",
        "standard output",
        "`-o -`",
        "record bodies are included by default",
        "`--no-bodies`",
        "default limit is `100`",
        "`1` through `1000`",
        "refuses to overwrite",
        "`--force`",
        "regular file",
        "symlink",
        "source store",
        "read-only",
    )

    missing = _missing_terms(guide, required)
    assert not missing, f"docs/cli/export.md is missing {missing!r}"


def test_export_tui_docs_define_private_off_pump_workflow() -> None:
    """The TUI guide separates reviewed record export from direct thread export."""
    tui = _read_text("docs/tui/index.md")
    section = _markdown_section(tui, "## Export")
    normalized = re.sub(r"\s+", " ", section).casefold()
    required = (
        "`/export [PATH]`",
        "`/export-thread [PATH]`",
        "Press `e`",
        "right detail pane",
        "restores the current search term",
        "selected record",
        "observed thread",
        "current result set",
        "Markdown",
        "private export directory",
        "`XDG_DATA_HOME`",
        "`0700`",
        "`0600`",
        "refuses to overwrite",
        "basename",
        "off the Textual message pump",
        "does not replace",
        "read-only",
    )

    missing = _missing_terms(section, required)
    assert not missing, f"docs/tui/index.md is missing {missing!r}"
    assert re.search(
        r"press `e`.*`/export \[path\]`.*right detail pane.*remembered"
        r".*directory and filename template.*exact filename",
        normalized,
    )
    assert re.search(r"`/export-thread \[path\]`.*one-shot", normalized)
    assert re.search(
        r"without `path`, `/export-thread`.*private export directory",
        normalized,
    )
    assert re.search(r"`/export-thread \[path\]`.*observed thread", normalized)


def test_export_guide_defines_reviewed_tui_destination() -> None:
    """The export guide explains the remembered, exact TUI save flow."""
    guide = _read_text("docs/cli/export.md")
    section = _markdown_section(guide, "## TUI reviewed save")
    normalized = re.sub(r"\s+", " ", section).casefold()

    for literal in (
        "`e`",
        "`/export [PATH]`",
        "`{date} {time} - {title}.md`",
        "`YYYY-MM-DD HH-MM-SS`",
        "no-clobber",
    ):
        assert literal in section
    assert re.search(
        r"exact selected record.*right detail pane"
        r".*remembers the export directory and filename template",
        normalized,
    )
    assert re.search(r"restores the current search term.*exact selection", normalized)
    assert re.search(
        r"first use.*after the preferences are saved successfully"
        r".*remembered directory and template",
        normalized,
    )
    assert re.search(r"local time.*filesystem-safe", normalized)
    assert re.search(r"\bno returns to editing\b", normalized)
    assert re.search(
        r"one-shot `/export-thread` command.*automatic private canonical-id name",
        normalized,
    )
    assert re.search(r"cli and mcp do not consume the tui preference", normalized)
    assert re.search(
        r"mcp.*accepts no local destination.*no filesystem write authority",
        normalized,
    )


def test_reviewed_tui_docs_define_save_mutation_boundary() -> None:
    """Every reviewed-save contract distinguishes review from durable writes."""
    for relative_path in (
        "docs/dev/adr/0017-portable-record-export.md",
        "docs/cli/export.md",
        "docs/tui/index.md",
    ):
        text = _read_text(relative_path)
        missing = _missing_terms(
            text,
            (
                "Save is the mutation boundary",
                "No and cancel perform no filesystem mutation",
                "TUI-private preference file",
            ),
        )
        assert not missing, f"{relative_path} is missing {missing!r}"


def test_export_mcp_docs_define_bounded_inline_contract() -> None:
    """The MCP guide distinguishes selection from discovery and local writes."""
    tools = _read_text("docs/mcp/tools.md")
    required = (
        "```{fastmcp-tool} export_records",
        "one to 20",
        "`agref1:`",
        "search refs",
        "defaults to `ndjson`",
        "defaults to false",
        "`include_bodies=true`",
        "400 KiB",
        "49,152 characters",
        "one `TextContent` artifact",
        "structured metadata",
        "local destination",
        "query",
        "cursor",
        "{tooliconl}`search`",
        "discovery",
        "pagination",
    )

    missing = _missing_terms(tools, required)
    assert not missing, f"docs/mcp/tools.md is missing {missing!r}"


def test_export_docs_shim_registers_bounded_public_signature() -> None:
    """The collector shim mirrors the bounded public MCP schema and metadata."""
    from docs._ext import agentgrep_fastmcp

    tool = agentgrep_fastmcp.export_records
    parameters = inspect.signature(tool).parameters
    hints = t.get_type_hints(tool, include_extras=True)
    refs_schema = TypeAdapter(hints["refs"]).json_schema()
    format_schema = TypeAdapter(hints["format"]).json_schema()
    selection_schema = TypeAdapter(hints["selection"]).json_schema()
    metadata = t.cast(t.Any, tool).__fastmcp__

    assert tuple(parameters) == ("refs", "format", "selection", "include_bodies")
    assert refs_schema["type"] == "array"
    assert refs_schema["items"] == {
        "maxLength": 48 * 1024,
        "minLength": 1,
        "type": "string",
    }
    assert refs_schema["minItems"] == 1
    assert refs_schema["maxItems"] == 20
    assert format_schema["enum"] == ["ndjson", "markdown"]
    assert selection_schema["enum"] == ["records", "thread"]
    assert parameters["format"].default == "ndjson"
    assert parameters["selection"].default == "records"
    assert parameters["include_bodies"].default is False
    assert hints["return"] is ExportRecordsResponse
    assert metadata.name == "export_records"
    assert metadata.title == "Export Records"
    assert metadata.tags == {"agentgrep", "export", "readonly"}
    assert metadata.annotations.readOnlyHint is True
    assert metadata.annotations.idempotentHint is True
    assert metadata.annotations.openWorldHint is False


async def test_docs_inspect_result_ref_schema_matches_runtime() -> None:
    """The docs-only ref bound stays byte-for-byte aligned with the tool."""
    from agentgrep.mcp import build_mcp_server
    from docs._ext import agentgrep_fastmcp

    hints = t.get_type_hints(agentgrep_fastmcp.inspect_result, include_extras=True)
    docs_schema = TypeAdapter(hints["ref"]).json_schema()
    runtime_tool = await build_mcp_server().get_tool("inspect_result")

    assert runtime_tool is not None
    assert docs_schema == runtime_tool.parameters["properties"]["ref"]
    assert docs_schema["maxLength"] == 48 * 1024


def test_documented_markdown_runtime_emits_schema_version_once() -> None:
    """The documented Markdown format carries its schema contract at runtime."""
    from agentgrep.record_export import render_export

    artifact = render_export((), format="markdown", include_bodies=False)

    assert artifact.text.splitlines()[:5] == [
        "# agentgrep record export",
        "",
        "- Schema version: agentgrep.v1",
        "- Selection: records",
        "- Record count: 0",
    ]
    assert artifact.text.count("Schema version: agentgrep.v1") == 1


def test_export_models_are_in_public_docs_reference_inventory() -> None:
    """Both request and response models render through config and API reference."""
    config = _read_text("docs/conf.py")
    reference = _read_text("docs/mcp/reference.md")

    for model_name in ("ExportRecordsRequest", "ExportRecordsResponse"):
        assert f'"{model_name}"' in config
        assert f".. autoclass:: agentgrep.mcp.{model_name}" in reference


def test_export_adr_pins_portability_privacy_and_fidelity() -> None:
    """The ADR records exact payload, Unicode, ordering, and thread limits."""
    adr = _read_text("docs/dev/adr/0017-portable-record-export.md")
    allowlist = (
        "`schema_version`",
        "`agent`",
        "`store`",
        "`kind`",
        "`role`",
        "`timestamp`",
        "`model`",
        "`content_id`",
        "`record_id`",
        "`record_id_stability`",
        "`thread_id`",
        "`text`",
    )
    exclusions = (
        "source paths",
        "display paths",
        "adapter metadata",
        "origin",
        "titles",
        "session IDs",
        "conversation IDs",
        "working directories",
    )
    semantics = (
        "one canonical JSON object per line",
        "stable key order",
        "lone surrogates as JSON escapes",
        "rejects lone surrogates",
        "dynamic backtick fence",
        "longer than every backtick run",
        "input permutation",
        "canonical thread ID",
        "`native_tree`",
        "`source_order`",
        "`unordered`",
        "observed thread",
        "does not claim completeness",
        "does not claim chronology",
    )

    missing = _missing_terms(adr, allowlist + exclusions + semantics)
    assert not missing, f"export ADR is missing {missing!r}"


def test_export_adr_pins_writer_and_deferred_tiers() -> None:
    """The ADR keeps durable output narrow and records deferred tradeoffs."""
    adr = _read_text("docs/dev/adr/0017-portable-record-export.md")
    writer = (
        "same-directory private temporary file",
        "complete writes",
        "file and parent directory",
        "atomic no-clobber",
        "explicit force",
        "symlink destinations",
        "source-store aliases",
        "`0700`",
        "`0600`",
    )
    deferred = (
        "HTML",
        "CSV",
        "Mermaid",
        "provider training profiles",
        "re-import",
        "sanitization",
        "nested metadata",
        "topology",
        "provider coupling",
        "conflict policy",
        "no new dependency",
    )

    missing = _missing_terms(adr, writer + deferred)
    assert not missing, f"export ADR is missing {missing!r}"


def test_export_adr_pins_interactive_filename_exception() -> None:
    """The ADR keeps reviewed TUI names separate from automatic private names."""
    adr = _read_text("docs/dev/adr/0017-portable-record-export.md")
    surface = re.sub(
        r"\s+",
        " ",
        _markdown_section(adr, "### Surface defaults"),
    ).casefold()
    durable_section = _markdown_section(adr, "### Durable file output")
    durable = re.sub(r"\s+", " ", durable_section).casefold()

    assert "`e`" in surface
    assert "`/export [path]`" in surface
    assert re.search(r"right detail pane.*restores.*search", surface)
    assert re.search(r"exact selected record.*remembers.*directory and filename template", surface)
    assert re.search(r"exact basename.*\bno returns.*explicit no-clobber destination", surface)
    assert re.search(r"cli and mcp do not consume this preference", surface)

    assert "`SearchRecord.title`" in durable_section
    assert re.search(
        r"automatic private filenames derive only from canonical ids",
        durable,
    )
    assert re.search(
        r"narrow exception.*filesystem-safe local timestamp.*bounded normalized",
        durable,
    )
    assert re.search(r"exact basename.*explicit no-clobber destination", durable)
    assert re.search(r"never reads the record body or source path", durable)


def test_export_console_examples_are_individually_copyable() -> None:
    """Every export console block contains exactly one shell command."""
    guide = _read_text("docs/cli/export.md")
    blocks = re.findall(r"```console\n(?P<body>.*?)\n```", guide, flags=re.DOTALL)

    assert len(blocks) >= 5
    for block in blocks:
        prompts = [line for line in block.splitlines() if line.startswith("$ ")]
        assert len(prompts) == 1, f"console block is not one command: {block!r}"


def test_export_changelog_has_one_product_deliverable() -> None:
    """The unreleased section lists issue 81 without a release summary."""
    changes = _read_text("CHANGES")
    release_match = re.search(
        r"^## agentgrep \d+\.\d+\.\d+\w* \(Yet to be released\)\n"
        r"(?P<body>.*?)(?=^## agentgrep |\Z)",
        changes,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert release_match is not None
    release = release_match.group("body")
    heading = "#### Portable record export across CLI, TUI, and MCP (#81)"
    end_marker = "<!-- END PLACEHOLDER - ADD NEW CHANGELOG ENTRIES BELOW THIS LINE -->"

    assert end_marker in release
    assert release.split(end_marker, maxsplit=1)[1].lstrip().startswith("### ")
    assert release.count(heading) == 1
    assert changes.count(heading) == 1
    assert release.count("(#81)") == 1
    assert "cli-export" in release
    assert all(term in release for term in ("NDJSON", "Markdown", "CLI", "HUD", "MCP"))
    assert not any(term in release for term in ("fsync", "temporary file", "400 KiB"))


def test_export_docs_do_not_divulge_local_paths_or_prompt_text() -> None:
    """New public export docs contain no host paths or real history excerpts."""
    paths = (
        "CHANGES",
        "docs/cli/export.md",
        "docs/cli/index.md",
        "docs/dev/adr/0017-portable-record-export.md",
        "docs/dev/adr/index.md",
        "docs/mcp/tools.md",
        "docs/tui/index.md",
    )

    for relative_path in paths:
        text = _read_text(relative_path)
        assert "/home/" not in text
        assert "/Users/" not in text
        assert "prompt body example" not in text
