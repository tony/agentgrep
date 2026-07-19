"""Module-size ratchet guard (#131).

New source modules stay at or under ``_SRC_LINE_BUDGET`` lines and test
files at or under ``_TEST_LINE_BUDGET``. Files that predate the ratchet are
grandfathered in ``_RATCHET_CEILINGS`` near their landing size: they may
shrink but not grow. A grandfather entry is a debt marker, not a licence —
raising a ceiling is a reviewed table edit, never a drive-by. Once a
grandfathered file drops under the generic budget,
:func:`test_ratchet_entries_are_live` demands the entry's removal, so the
table only ever shrinks.

The table follows the pandas/pytest per-file-ignore convention: every
cluster carries an intent comment saying why it is allowed to stay large,
so an entry can be challenged on its stated reason instead of on
archaeology.
"""

from __future__ import annotations

import pathlib

_SRC_LINE_BUDGET = 500
_TEST_LINE_BUDGET = 2500

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_RATCHET_CEILINGS: dict[str, int] = {
    # Compatibility facade: still re-exports the full legacy surface while
    # the strangler-fig split settles (ADR 0010).
    "src/agentgrep/__init__.py": 950,
    # Engine pipeline: planning/scanning/scheduling stages are one
    # architecture (ADR 0004); further splits are their own deliverable.
    "src/agentgrep/_engine/orchestration.py": 900,
    "src/agentgrep/_engine/planning.py": 700,
    "src/agentgrep/_engine/profiling.py": 550,
    "src/agentgrep/_engine/scanning.py": 700,
    "src/agentgrep/_engine/scheduling.py": 1250,
    # Per-agent parser clusters (#128): family cohesion mirrors
    # store_catalog/ naming, so the two biggest agents land oversized and
    # are ratcheted rather than split against the family boundary.
    "src/agentgrep/adapters/claude.py": 550,
    "src/agentgrep/adapters/codex.py": 800,
    # Store catalogue data tables: one descriptor per store, prose-heavy
    # schema notes; row count tracks the agent's on-disk surface.
    "src/agentgrep/store_catalog/claude.py": 1100,
    "src/agentgrep/store_catalog/codex.py": 1050,
    # CLI surface: argparse tree and renderers grow with subcommands.
    "src/agentgrep/cli/parser.py": 1350,
    "src/agentgrep/cli/render.py": 700,
    "src/agentgrep/cli/renderers.py": 650,
    # Single-topic modules that predate the ratchet.
    "src/agentgrep/_text.py": 800,
    "src/agentgrep/discovery.py": 1600,
    "src/agentgrep/mcp/models.py": 550,
    "src/agentgrep/origin.py": 700,
    "src/agentgrep/progress.py": 1050,
    "src/agentgrep/query/compile.py": 600,
    "src/agentgrep/query/evaluate.py": 550,
    "src/agentgrep/readers.py": 1000,
    # TUI: the HUD screen split is tracked by the reusable-widget plan
    # (ADR 0012); the shell/runtime/chrome modules sit just over budget.
    "src/agentgrep/ui/_runtime.py": 550,
    "src/agentgrep/ui/layouts/greplog.py": 550,
    "src/agentgrep/ui/layouts/hud.py": 2700,
    "src/agentgrep/ui/widgets/status.py": 800,
    # The remaining library/CLI compatibility module still predates the
    # generic budget; the extracted TUI matrix must not grow it again.
    "tests/test_agentgrep.py": 10000,
}


def _line_count(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _over_budget(
    paths: list[pathlib.Path],
    budget: int,
) -> list[str]:
    """Return ``path: lines`` diagnostics for files beyond their allowance."""
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(_REPO_ROOT).as_posix()
        lines = _line_count(path)
        ceiling = _RATCHET_CEILINGS.get(relative, budget)
        if lines > ceiling:
            offenders.append(f"{relative}: {lines} lines (allowed {ceiling})")
    return offenders


def test_src_modules_within_budget() -> None:
    """Every source module fits the budget or its grandfathered ceiling."""
    paths = sorted((_REPO_ROOT / "src" / "agentgrep").rglob("*.py"))
    assert paths
    offenders = _over_budget(paths, _SRC_LINE_BUDGET)
    assert not offenders, (
        "split the module, or raise its ceiling in a reviewed table edit:\n" + "\n".join(offenders)
    )


def test_test_files_within_budget() -> None:
    """Every test file fits the budget or its grandfathered ceiling."""
    paths = sorted((_REPO_ROOT / "tests").rglob("*.py"))
    assert paths
    offenders = _over_budget(paths, _TEST_LINE_BUDGET)
    assert not offenders, (
        "split the test file, or raise its ceiling in a reviewed table edit:\n"
        + "\n".join(offenders)
    )


def test_ratchet_entries_are_live() -> None:
    """Every ratchet entry names an existing, still-oversized file.

    Entries whose file shrank under the generic budget (or disappeared) are
    stale and must be deleted so the table only ever shrinks.
    """
    stale: list[str] = []
    for relative, ceiling in _RATCHET_CEILINGS.items():
        path = _REPO_ROOT / relative
        budget = _TEST_LINE_BUDGET if relative.startswith("tests/") else _SRC_LINE_BUDGET
        if not path.is_file():
            stale.append(f"{relative}: file no longer exists")
            continue
        if ceiling <= budget or _line_count(path) <= budget:
            stale.append(f"{relative}: back under the {budget}-line budget")
    assert not stale, "remove stale ratchet entries:\n" + "\n".join(stale)
