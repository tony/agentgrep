"""Documentation contract tests for durable bookmarks."""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read_text(relative_path: str) -> str:
    """Return one tracked documentation file's text."""
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"missing documentation contract: {relative_path}"
    return path.read_text(encoding="utf-8")


def test_bookmark_docs_are_indexed() -> None:
    """The CLI guide and ADR are reachable through their public indexes."""
    cli_index = _read_text("docs/cli/index.md")
    adr_index = _read_text("docs/dev/adr/index.md")
    cli_guide = _read_text("docs/cli/bookmark.md")
    adr = _read_text("docs/dev/adr/0016-durable-bookmarks.md")

    assert re.search(r"(?m)^bookmark$", cli_index)
    assert "0016-durable-bookmarks" in adr_index
    assert "(cli-bookmark)=" in cli_guide
    assert "(adr-durable-bookmarks)=" in adr


def test_bookmark_cli_docs_define_canonical_mutation_contract() -> None:
    """The headless guide covers each scope and bounded idempotent mutations."""
    guide = _read_text("docs/cli/bookmark.md")
    required = (
        "`content`",
        "`agc1:`",
        "`record`",
        "`agr1:`",
        "`--content-id`",
        "`thread`",
        "`agt1:`",
        "complete canonical ID",
        "idempotent",
        "`added`",
        "`removed`",
        "`unchanged`",
        "200",
        "capacity",
        "`--json`",
        "read-only",
    )

    missing = tuple(term for term in required if term not in guide)
    assert not missing, f"docs/cli/bookmark.md is missing {missing!r}"


def test_bookmark_cli_docs_distinguish_record_add_and_remove() -> None:
    """Record validation is required only for add and must match on re-add."""
    guide = _read_text("docs/cli/bookmark.md")
    required = (
        "`--content-id` is required for `bookmark add`",
        "`bookmark remove`",
        "removal needs only the complete target ID",
        "same saved content validator",
        "different valid `agc1:` validator",
        "validation failure",
        "exits `1`",
    )

    missing = tuple(term for term in required if term not in guide)
    assert not missing, f"record validator semantics are missing {missing!r}"


def test_bookmark_tui_docs_define_toggle_and_recall_contract() -> None:
    """The TUI guide explains pi-like toggles and current-store recall."""
    tui = _read_text("docs/tui/index.md")
    required = (
        "`b`",
        "`/bookmark`",
        "`/bookmark record`",
        "`/bookmark thread`",
        "`/bookmark content`",
        "`/bookmarks`",
        "`★`",
        "resolved",
        "unresolved",
        "current stores",
        "does not replace",
        "filter",
        "`Enter`",
        "`Esc`",
        "`Ctrl-C`",
        "read-only",
    )

    missing = tuple(term for term in required if term not in tui)
    assert not missing, f"docs/tui/index.md is missing {missing!r}"


def test_bookmark_adr_pins_privacy_storage_and_resolution() -> None:
    """The ADR keeps persistence minimal and source-store semantics explicit."""
    adr = _read_text("docs/dev/adr/0016-durable-bookmarks.md")
    required = (
        "adr-deterministic-record-identity",
        "XDG data directory",
        "`schema_version`",
        "`target_id`",
        "`scope`",
        "`content_id`",
        "`created_at`",
        "default capacity is 200",
        "prompt text",
        "titles",
        "source paths",
        "working directories",
        "repository paths",
        "refuses the snapshot as a whole",
        "unknown schema",
        "duplicate targets",
        "malformed entries",
        "over-capacity",
        "source stores remain read-only",
        "unresolved bookmarks remain saved",
        "representative current record",
        "equal-content occurrence",
        "idempotent",
        "MCP",
    )

    missing = tuple(term for term in required if term not in adr)
    assert not missing, f"bookmark ADR is missing {missing!r}"


def test_bookmark_console_examples_are_individually_copyable() -> None:
    """Every bookmark console block contains exactly one shell command."""
    guide = _read_text("docs/cli/bookmark.md")
    blocks = re.findall(r"```console\n(?P<body>.*?)\n```", guide, flags=re.DOTALL)

    assert len(blocks) >= 5
    for block in blocks:
        prompts = [line for line in block.splitlines() if line.startswith("$ ")]
        assert len(prompts) == 1, f"console block is not one command: {block!r}"


def test_bookmark_changelog_has_one_product_deliverable() -> None:
    """The unreleased section has one section-first issue 79 deliverable."""
    changes = _read_text("CHANGES")
    release_match = re.search(
        r"^## agentgrep \d+\.\d+\.\d+\w* \(Yet to be released\)\n"
        r"(?P<body>.*?)(?=^## agentgrep |\Z)",
        changes,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert release_match is not None
    release = release_match.group("body")
    heading = "#### Durable bookmarks across CLI and TUI (#79)"
    end_marker = "<!-- END PLACEHOLDER - ADD NEW CHANGELOG ENTRIES BELOW THIS LINE -->"

    assert release.count(heading) == 1
    assert changes.count(heading) == 1
    assert end_marker in release
    assert release.split(end_marker, maxsplit=1)[1].lstrip().startswith("### ")
    assert release.count("(#79)") == 1
    assert "cli-bookmark" in release
    assert all(term in release for term in ("record", "thread", "content", "current stores"))
    assert not any(term in release for term in ("flock", "fsync", "tempfile"))


def test_bookmark_docs_do_not_divulge_local_paths() -> None:
    """New public bookmark docs contain no host-specific absolute paths."""
    paths = (
        "docs/cli/bookmark.md",
        "docs/dev/adr/0016-durable-bookmarks.md",
    )

    for relative_path in paths:
        text = _read_text(relative_path)
        assert "/home/" not in text
        assert "/Users/" not in text
