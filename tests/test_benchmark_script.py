"""Tests for scripts/benchmark.py.

The benchmark harness lives outside ``src/`` as a PEP 723 standalone, so we
load it via :func:`importlib.util.spec_from_file_location` and exercise the
pure-Python logic (stats, config layering, target resolution, templating,
JSON shape). Subprocess invocation is mocked — the hyperfine / DIY timing
code path stays out of pytest where it would take minutes per test.
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import sys
import typing as t

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "benchmark.py"

_spec = importlib.util.spec_from_file_location("benchmark_script", _SCRIPT)
assert _spec and _spec.loader
benchmark = importlib.util.module_from_spec(_spec)
sys.modules["benchmark_script"] = benchmark
_spec.loader.exec_module(benchmark)


# ---------------------------------------------------------------------------
# Stats: percentile + stat_for_label
# ---------------------------------------------------------------------------


class StatsCase(t.NamedTuple):
    """One stats expectation — feed ``samples``, assert each labelled stat."""

    test_id: str
    samples: list[float]
    expected: dict[str, float]


STATS_CASES: tuple[StatsCase, ...] = (
    StatsCase(
        test_id="single-sample",
        samples=[1.0],
        expected={"min": 1.0, "max": 1.0, "avg": 1.0, "p50": 1.0, "p90": 1.0, "p99": 1.0},
    ),
    StatsCase(
        test_id="all-equal",
        samples=[2.0, 2.0, 2.0, 2.0],
        expected={"min": 2.0, "max": 2.0, "avg": 2.0, "p50": 2.0, "p90": 2.0, "p99": 2.0},
    ),
    StatsCase(
        test_id="odd-count",
        # k = ceil(pct/100 * n); p50 → k=2 (index 1) → 2.0; p90 → k=3 (index 2) → 3.0
        samples=[1.0, 2.0, 3.0],
        expected={"min": 1.0, "max": 3.0, "avg": 2.0, "p50": 2.0, "p90": 3.0, "p99": 3.0},
    ),
    StatsCase(
        test_id="even-count",
        # p50 → k=2 (index 1) → 2.0; p90 → k=4 (index 3) → 4.0
        samples=[1.0, 2.0, 3.0, 4.0],
        expected={"min": 1.0, "max": 4.0, "avg": 2.5, "p50": 2.0, "p90": 4.0, "p99": 4.0},
    ),
    StatsCase(
        test_id="single-outlier-pulls-max",
        # p50 → k=3 (index 2) → 1.0 (outlier excluded from median)
        # p90 → k=5 (index 4) → 100.0 (outlier pulls high percentiles)
        samples=[1.0, 1.0, 1.0, 1.0, 100.0],
        expected={"min": 1.0, "max": 100.0, "avg": 20.8, "p50": 1.0, "p90": 100.0, "p99": 100.0},
    ),
    StatsCase(
        test_id="hundred-samples-percentiles-align",
        # 1..100 → p50 → k=50 → value at index 49 → 50; p90 → 90; p99 → 99
        samples=[float(i) for i in range(1, 101)],
        expected={"min": 1.0, "max": 100.0, "avg": 50.5, "p50": 50.0, "p90": 90.0, "p99": 99.0},
    ),
)


@pytest.mark.parametrize("case", STATS_CASES, ids=[c.test_id for c in STATS_CASES])
def test_stat_for_label_matches_expected(case: StatsCase) -> None:
    """Every labelled stat resolves to the expected value (nearest-rank semantics)."""
    for label, expected in case.expected.items():
        actual = benchmark.stat_for_label(case.samples, label)
        assert actual == pytest.approx(expected), f"{case.test_id} / {label}"


def test_stat_for_label_rejects_unknown_label() -> None:
    """An unrecognised label fails loud rather than silently returning ``nan``."""
    with pytest.raises(ValueError, match="unknown stat label"):
        _ = benchmark.stat_for_label([1.0, 2.0], "median")


def test_percentile_empty_returns_nan() -> None:
    """An empty sample list returns NaN — the renderer dashes the cell out."""
    assert math.isnan(benchmark.percentile([], 50))


# ---------------------------------------------------------------------------
# Target resolution (git mocked)
# ---------------------------------------------------------------------------


class TargetResolutionCase(t.NamedTuple):
    """One target-selector resolution expectation."""

    test_id: str
    kwargs: dict[str, t.Any]
    git_table: dict[tuple[str, ...], str]
    expected_shas: list[str]


def _make_git_runner(table: dict[tuple[str, ...], str]) -> t.Callable[[tuple[str, ...]], str]:
    """Build a ``git_runner`` callable backed by a fixed lookup table.

    Any unmatched argv tuple raises ``AssertionError`` so a test that calls
    git unexpectedly fails loud rather than silently returning ``''``.
    """

    def runner(args: tuple[str, ...]) -> str:
        if args not in table:
            msg = f"unexpected git invocation: {args}"
            raise AssertionError(msg)
        return table[args]

    return runner


TARGET_CASES: tuple[TargetResolutionCase, ...] = (
    TargetResolutionCase(
        test_id="single-target-head",
        kwargs={"target": "HEAD"},
        git_table={
            ("rev-parse", "HEAD"): "aaaaaaa1111",
            ("log", "-1", "--pretty=format:%s", "aaaaaaa1111"): "head subject",
        },
        expected_shas=["aaaaaaa1111"],
    ),
    TargetResolutionCase(
        test_id="head-vs-trunk-yields-two",
        kwargs={"head_vs_trunk": True, "trunk": "master"},
        git_table={
            ("rev-parse", "HEAD"): "aaaaaaa1111",
            ("log", "-1", "--pretty=format:%s", "aaaaaaa1111"): "head subject",
            ("rev-parse", "master"): "bbbbbbb2222",
            ("log", "-1", "--pretty=format:%s", "bbbbbbb2222"): "trunk subject",
        },
        expected_shas=["aaaaaaa1111", "bbbbbbb2222"],
    ),
    TargetResolutionCase(
        test_id="explicit-commits-comma-list",
        kwargs={"commits": "abc1234,def5678"},
        git_table={
            ("rev-parse", "abc1234"): "abc1234aaa",
            ("log", "-1", "--pretty=format:%s", "abc1234aaa"): "first",
            ("rev-parse", "def5678"): "def5678bbb",
            ("log", "-1", "--pretty=format:%s", "def5678bbb"): "second",
        },
        expected_shas=["abc1234aaa", "def5678bbb"],
    ),
    TargetResolutionCase(
        test_id="range-uses-reversed-rev-list",
        kwargs={"range_spec": "master..HEAD"},
        git_table={
            ("rev-list", "--reverse", "master..HEAD"): "sha1\nsha2\nsha3",
            ("rev-parse", "sha1"): "sha1fullsha",
            ("log", "-1", "--pretty=format:%s", "sha1fullsha"): "one",
            ("rev-parse", "sha2"): "sha2fullsha",
            ("log", "-1", "--pretty=format:%s", "sha2fullsha"): "two",
            ("rev-parse", "sha3"): "sha3fullsha",
            ("log", "-1", "--pretty=format:%s", "sha3fullsha"): "three",
        },
        expected_shas=["sha1fullsha", "sha2fullsha", "sha3fullsha"],
    ),
    TargetResolutionCase(
        test_id="lookback-reverses-newest-first-to-chronological",
        kwargs={"lookback": 3},
        git_table={
            # git rev-list returns newest-first; harness reverses to oldest-first.
            ("rev-list", "-n", "3", "HEAD"): "new\nmid\nold",
            ("rev-parse", "old"): "oldfullsha0",
            ("log", "-1", "--pretty=format:%s", "oldfullsha0"): "old subj",
            ("rev-parse", "mid"): "midfullsha1",
            ("log", "-1", "--pretty=format:%s", "midfullsha1"): "mid subj",
            ("rev-parse", "new"): "newfullsha2",
            ("log", "-1", "--pretty=format:%s", "newfullsha2"): "new subj",
        },
        expected_shas=["oldfullsha0", "midfullsha1", "newfullsha2"],
    ),
    TargetResolutionCase(
        test_id="tags-uses-version-sort",
        kwargs={"tags": True},
        git_table={
            ("tag", "--sort=v:refname"): "v0.1.0\nv0.1.1\nv1.0.0",
            ("rev-parse", "v0.1.0"): "v010fullsha",
            ("log", "-1", "--pretty=format:%s", "v010fullsha"): "v0.1.0 release",
            ("rev-parse", "v0.1.1"): "v011fullsha",
            ("log", "-1", "--pretty=format:%s", "v011fullsha"): "v0.1.1 release",
            ("rev-parse", "v1.0.0"): "v100fullsha",
            ("log", "-1", "--pretty=format:%s", "v100fullsha"): "v1.0.0 release",
        },
        expected_shas=["v010fullsha", "v011fullsha", "v100fullsha"],
    ),
)


@pytest.mark.parametrize("case", TARGET_CASES, ids=[c.test_id for c in TARGET_CASES])
def test_resolve_target_returns_expected_commits(case: TargetResolutionCase) -> None:
    """Each selector resolves to the expected chronologically-ordered SHA list."""
    runner = _make_git_runner(case.git_table)
    commits = benchmark.resolve_target(git_runner=runner, **case.kwargs)
    assert [c.sha for c in commits] == case.expected_shas
    # Short SHAs are the first seven chars of the full SHA.
    for c in commits:
        assert c.short_sha == c.sha[:7]


# ---------------------------------------------------------------------------
# Config layering
# ---------------------------------------------------------------------------


class ConfigLayeringCase(t.NamedTuple):
    """One config-layering scenario."""

    test_id: str
    primary_toml: str | None
    local_toml: str | None
    cli_overrides: dict[str, t.Any] | None
    expected_runs: int
    expected_trunk: str
    expected_bench_keys: list[str]
    expected_grep_command: str | None


CONFIG_CASES: tuple[ConfigLayeringCase, ...] = (
    ConfigLayeringCase(
        test_id="defaults-only-no-benches",
        primary_toml=None,
        local_toml=None,
        cli_overrides=None,
        expected_runs=3,
        expected_trunk="master",
        expected_bench_keys=[],
        expected_grep_command=None,
    ),
    ConfigLayeringCase(
        test_id="primary-toml-populates-bench-and-settings",
        primary_toml=(
            '[settings]\nruns = 5\ntrunk = "main"\n\n'
            '[bench.grep]\ncommand = "agentgrep grep {query}"\ndefault_query = "foo"\n'
        ),
        local_toml=None,
        cli_overrides=None,
        expected_runs=5,
        expected_trunk="main",
        expected_bench_keys=["grep"],
        expected_grep_command="agentgrep grep {query}",
    ),
    ConfigLayeringCase(
        test_id="local-overlay-replaces-keys-and-merges-bench",
        primary_toml=('[settings]\nruns = 3\n\n[bench.grep]\ncommand = "primary-grep"\n'),
        local_toml=(
            "[settings]\nruns = 9\n\n"
            '[bench.grep]\ncommand = "local-grep"\n'
            '[bench.find]\ncommand = "local-find"\n'
        ),
        cli_overrides=None,
        expected_runs=9,
        expected_trunk="master",
        expected_bench_keys=["grep", "find"],
        expected_grep_command="local-grep",
    ),
    ConfigLayeringCase(
        test_id="cli-overrides-trump-all-toml-layers",
        primary_toml=('[settings]\nruns = 3\n\n[bench.grep]\ncommand = "primary-grep"\n'),
        local_toml="[settings]\nruns = 9\n",
        cli_overrides={"settings": {"runs": 42}},
        expected_runs=42,
        expected_trunk="master",
        expected_bench_keys=["grep"],
        expected_grep_command="primary-grep",
    ),
)


@pytest.mark.parametrize("case", CONFIG_CASES, ids=[c.test_id for c in CONFIG_CASES])
def test_load_config_layers_in_documented_precedence_order(
    case: ConfigLayeringCase,
    tmp_path: pathlib.Path,
) -> None:
    """Each layer is folded onto the previous; CLI overrides win."""
    primary = tmp_path / "benchmark.toml"
    local = tmp_path / "benchmark.local.toml"
    if case.primary_toml is not None:
        primary.write_text(case.primary_toml)
    if case.local_toml is not None:
        local.write_text(case.local_toml)
    config = benchmark.load_config(
        config_path=primary if case.primary_toml is not None else tmp_path / "missing.toml",
        local_path=local if case.local_toml is not None else tmp_path / "missing.local.toml",
        cli_overrides=case.cli_overrides,
    )
    assert config.settings.runs == case.expected_runs
    assert config.settings.trunk == case.expected_trunk
    assert list(config.bench) == case.expected_bench_keys
    if case.expected_grep_command is not None:
        assert config.bench["grep"].command == case.expected_grep_command


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


class TemplatingCase(t.NamedTuple):
    """One ``render_command`` expectation."""

    test_id: str
    template: str
    context: dict[str, str]
    expected: str | None
    raises: type[BaseException] | None


TEMPLATE_CASES: tuple[TemplatingCase, ...] = (
    TemplatingCase(
        test_id="bare-query-substitution",
        template="echo {query}",
        context={"query": "hello"},
        expected="echo hello",
        raises=None,
    ),
    TemplatingCase(
        test_id="multi-token-grep-shape",
        template="{venv}/bin/agentgrep grep -m 1 {query}",
        context={"venv": ".venv", "query": "libtmux"},
        expected=".venv/bin/agentgrep grep -m 1 libtmux",
        raises=None,
    ),
    TemplatingCase(
        test_id="empty-query-is-rendered-as-empty",
        template="{venv}/bin/agentgrep --help {query}",
        context={"venv": ".venv", "query": ""},
        expected=".venv/bin/agentgrep --help ",
        raises=None,
    ),
    TemplatingCase(
        test_id="unknown-token-raises-keyerror",
        template="echo {undefined}",
        context={"query": "x"},
        expected=None,
        raises=KeyError,
    ),
)


@pytest.mark.parametrize("case", TEMPLATE_CASES, ids=[c.test_id for c in TEMPLATE_CASES])
def test_render_command_substitutes_or_raises(case: TemplatingCase) -> None:
    """Known placeholders interpolate; unknown placeholders raise."""
    if case.raises is not None:
        with pytest.raises(case.raises):
            _ = benchmark.render_command(case.template, case.context)
    else:
        actual = benchmark.render_command(case.template, case.context)
        assert actual == case.expected


# ---------------------------------------------------------------------------
# JSON shape — Measurement.model_dump round-trip
# ---------------------------------------------------------------------------


def test_measurement_json_shape_preserves_documented_keys() -> None:
    """The serialised payload exposes raw ``samples`` plus the documented keys."""
    m = benchmark.Measurement(
        sha="0123456789abcdef0123456789abcdef01234567",
        short_sha="0123456",
        subject="feat: a thing",
        command_name="grep",
        command_string=".venv/bin/agentgrep grep -m 1 libtmux",
        samples=[0.5, 0.6, 0.55],
        status="ok",
    )
    payload = m.model_dump(mode="json")
    assert set(payload) == {
        "sha",
        "short_sha",
        "subject",
        "command_name",
        "command_string",
        "samples",
        "status",
        "error",
    }
    assert payload["samples"] == [0.5, 0.6, 0.55]
    # Computed stats are properties, not stored fields — they should not leak
    # into model_dump (consumers compute their own from the raw samples).
    assert "avg_s" not in payload
    assert "min_s" not in payload


def test_measurement_property_stats_match_manual_calculation() -> None:
    """Properties on Measurement match the docstring contract (avg / min / max / stddev)."""
    m = benchmark.Measurement(
        sha="x" * 40,
        short_sha="xxxxxxx",
        subject="s",
        command_name="grep",
        command_string="cmd",
        samples=[1.0, 2.0, 3.0],
    )
    assert m.min_s == 1.0
    assert m.max_s == 3.0
    assert m.avg_s == pytest.approx(2.0)
    # Sample stddev for [1,2,3]: sqrt(((1-2)^2 + (2-2)^2 + (3-2)^2) / 2) = sqrt(1) = 1.0
    assert m.stddev_s == pytest.approx(1.0)


def test_measurement_with_no_samples_returns_nan_for_stats() -> None:
    """A failed (no-samples) measurement returns NaN — never crashes."""
    m = benchmark.Measurement(
        sha="x" * 40,
        short_sha="xxxxxxx",
        subject="s",
        command_name="grep",
        command_string="cmd",
        samples=[],
        status="bench_fail",
        error="boom",
    )
    assert math.isnan(m.min_s)
    assert math.isnan(m.max_s)
    assert math.isnan(m.avg_s)
    assert m.stddev_s == 0.0


def test_md_escape_neutralises_pipe_in_subject() -> None:
    """A subject containing a literal ``|`` would otherwise split the markdown row."""
    raw = "feat: support a|b switch with newline\nin subject"
    escaped = benchmark._md_escape(raw)
    # No raw | left to break the table; newline collapsed.
    assert "|" not in escaped.replace("\\|", "")
    assert "\n" not in escaped
    # Backslashes themselves are doubled so they round-trip through the
    # markdown lexer.
    assert benchmark._md_escape("a\\b") == "a\\\\b"
