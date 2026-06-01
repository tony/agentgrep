"""Tests for scripts/benchmark.py.

The benchmark harness lives outside ``src/`` as a PEP 723 standalone, so we
load it via :func:`importlib.util.spec_from_file_location` and exercise the
pure-Python logic (stats, config layering, target resolution, templating,
JSON shape). Subprocess invocation is mocked — the hyperfine / DIY timing
code path stays out of pytest where it would take minutes per test.
"""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import shlex
import subprocess
import sys
import typing as t

import pytest
import typer

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


class BenchmarkLimitCase(t.NamedTuple):
    """One configured benchmark and its command tokens."""

    test_id: str
    name: str
    description: str
    tokens: list[str]


def _committed_benchmark_limit_cases() -> tuple[BenchmarkLimitCase, ...]:
    config = benchmark.load_config(
        config_path=benchmark.DEFAULT_CONFIG,
        local_path=_REPO_ROOT / "scripts" / "__missing_benchmark.local.toml",
    )
    return tuple(
        BenchmarkLimitCase(
            test_id=name,
            name=name,
            description=bench.description,
            tokens=shlex.split(bench.command),
        )
        for name, bench in config.bench.items()
    )


def _flag_values(tokens: list[str], flag: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            values.append(tokens[index + 1])
    return values


@pytest.mark.parametrize(
    "case",
    _committed_benchmark_limit_cases(),
    ids=[c.test_id for c in _committed_benchmark_limit_cases()],
)
def test_committed_benchmarks_name_every_command_limit(case: BenchmarkLimitCase) -> None:
    """Committed benchmark keys and descriptions disclose command caps."""
    description = case.description.casefold()
    assert "-m" not in case.tokens

    max_counts = _flag_values(case.tokens, "--max-count")
    limits = _flag_values(case.tokens, "--limit")
    if not max_counts and not limits:
        assert "max-count-" not in case.name
        assert "limit-" not in case.name
        assert "max-count " not in description
        assert "limit " not in description

    for value in max_counts:
        assert f"max-count-{value}" in case.name
        assert f"max-count {value}" in description
    for value in limits:
        assert f"limit-{value}" in case.name
        assert f"limit {value}" in description


def test_committed_benchmarks_include_engine_only_profile_entries() -> None:
    """Committed profiling coverage separates engine timing from CLI rendering."""
    config = benchmark.load_config(
        config_path=benchmark.DEFAULT_CONFIG,
        local_path=_REPO_ROOT / "scripts" / "__missing_benchmark.local.toml",
    )
    expected = {
        "profile-engine-search-all-prompts-limit-500",
        "profile-engine-search-all-conversations-limit-500",
        "profile-engine-grep-all-prompts-max-count-500",
        "profile-engine-grep-all-conversations-max-count-500",
        "profile-engine-find-all-prompts-limit-500",
    }
    assert expected <= set(config.bench)
    for name in expected:
        bench = config.bench[name]
        assert "scripts/profile_engine.py" in bench.command
        if "max-count" in name:
            assert "--max-count 500" in bench.command
            assert "max-count 500" in bench.description.casefold()
        else:
            assert "--limit 500" in bench.command
            assert "limit 500" in bench.description.casefold()


class BenchmarkSelectorCase(t.NamedTuple):
    """One benchmark selector expansion expectation."""

    test_id: str
    commands: str | None
    expected_names: list[str]


PROFILE_ENGINE_BENCHMARKS: list[str] = [
    "profile-engine-search-all-prompts-limit-500",
    "profile-engine-search-all-conversations-limit-500",
    "profile-engine-grep-all-prompts-max-count-500",
    "profile-engine-grep-all-conversations-max-count-500",
    "profile-engine-find-all-prompts-limit-500",
]


BENCHMARK_SELECTOR_CASES: tuple[BenchmarkSelectorCase, ...] = (
    BenchmarkSelectorCase(
        test_id="none-keeps-config-order",
        commands=None,
        expected_names=["grep", *PROFILE_ENGINE_BENCHMARKS, "import-time"],
    ),
    BenchmarkSelectorCase(
        test_id="exact-name",
        commands="grep",
        expected_names=["grep"],
    ),
    BenchmarkSelectorCase(
        test_id="profile-engine-group",
        commands="profile-engine",
        expected_names=PROFILE_ENGINE_BENCHMARKS,
    ),
    BenchmarkSelectorCase(
        test_id="mixed-exact-and-group",
        commands="grep,profile-engine,import-time",
        expected_names=["grep", *PROFILE_ENGINE_BENCHMARKS, "import-time"],
    ),
)


@pytest.mark.parametrize(
    "case",
    BENCHMARK_SELECTOR_CASES,
    ids=[c.test_id for c in BENCHMARK_SELECTOR_CASES],
)
def test_select_bench_names_expands_command_groups(case: BenchmarkSelectorCase) -> None:
    """Benchmark command selectors support exact names and curated groups."""
    config = benchmark.Config(
        bench={
            "grep": benchmark.BenchCommand(command="echo grep"),
            **{
                name: benchmark.BenchCommand(command=f"echo {name}")
                for name in PROFILE_ENGINE_BENCHMARKS
            },
            "import-time": benchmark.BenchCommand(command="echo import"),
        },
    )

    assert benchmark._select_bench_names(config, case.commands) == case.expected_names


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
        template="{venv}/bin/agentgrep grep --max-count 1 {query}",
        context={"venv": ".venv", "query": "libtmux"},
        expected=".venv/bin/agentgrep grep --max-count 1 libtmux",
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
        command_string=".venv/bin/agentgrep grep --max-count 1 libtmux",
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
        "dry_run",
        "profile_payload",
        "profile_capture_error",
        "schema_version",
        "artifact_kind",
    }
    assert payload["samples"] == [0.5, 0.6, 0.55]
    assert payload["dry_run"] is False
    assert payload["profile_payload"] is None
    assert payload["profile_capture_error"] is None
    assert payload["schema_version"] == 1
    assert payload["artifact_kind"] == "agentgrep.benchmark.measurement"
    # Computed stats are properties, not stored fields — they should not leak
    # into model_dump (consumers compute their own from the raw samples).
    assert "avg_s" not in payload
    assert "min_s" not in payload


def test_benchmark_json_renderer_includes_artifact_metadata() -> None:
    """Benchmark JSON artifacts carry a stable root shape marker."""
    measurement = benchmark.Measurement(
        sha="0" * 40,
        short_sha="0000000",
        subject="subject",
        command_name="grep",
        command_string="{venv}/bin/agentgrep grep {query}",
        samples=[0.1],
    )

    payload = json.loads(benchmark.render_json([measurement], []))

    assert payload["schema_version"] == 1
    assert payload["artifact_kind"] == "agentgrep.benchmark.runs"
    assert payload["runs"][0]["artifact_kind"] == "agentgrep.benchmark.measurement"


def test_benchmark_ndjson_rows_include_artifact_metadata() -> None:
    """Benchmark NDJSON rows are self-describing without a root object."""
    measurement = benchmark.Measurement(
        sha="0" * 40,
        short_sha="0000000",
        subject="subject",
        command_name="grep",
        command_string="{venv}/bin/agentgrep grep {query}",
        samples=[0.1],
    )

    row = json.loads(benchmark.render_ndjson([measurement], []))

    assert row["schema_version"] == 1
    assert row["artifact_kind"] == "agentgrep.benchmark.measurement"


class SafeProfileAttributeCase(t.NamedTuple):
    """One sanitizer expectation — feed ``attributes``, assert the safe dict."""

    test_id: str
    attributes: dict[object, object]
    expected: dict[str, object]


_SAFE_PROFILE_ATTRIBUTE_CASES: tuple[SafeProfileAttributeCase, ...] = (
    SafeProfileAttributeCase(
        test_id="path-kind-kept",
        attributes={"agentgrep_path_kind": "sqlite_db"},
        expected={"agentgrep_path_kind": "sqlite_db"},
    ),
    SafeProfileAttributeCase(
        test_id="env-path-status-kept",
        attributes={"agentgrep_env_path_status": "not_found"},
        expected={"agentgrep_env_path_status": "not_found"},
    ),
    SafeProfileAttributeCase(
        test_id="override-path-status-kept",
        attributes={"agentgrep_override_path_status": "not_a_directory"},
        expected={"agentgrep_override_path_status": "not_a_directory"},
    ),
    SafeProfileAttributeCase(
        test_id="agentgrep-path-dropped",
        attributes={"agentgrep_path": "/home/private/project"},
        expected={},
    ),
    SafeProfileAttributeCase(
        test_id="agentgrep-query-dropped",
        attributes={"agentgrep_query": "private-token"},
        expected={},
    ),
    SafeProfileAttributeCase(
        test_id="non-string-key-dropped",
        attributes={1: "value"},
        expected={},
    ),
    SafeProfileAttributeCase(
        test_id="non-scalar-value-dropped",
        attributes={"agentgrep_extra": ["nested"]},
        expected={},
    ),
)


@pytest.mark.parametrize(
    "case",
    _SAFE_PROFILE_ATTRIBUTE_CASES,
    ids=[c.test_id for c in _SAFE_PROFILE_ATTRIBUTE_CASES],
)
def test_safe_profile_attribute_dict_allowlists_safe_classifiers(
    case: SafeProfileAttributeCase,
) -> None:
    """Denied substrings drop sensitive keys but spare allowlisted classifiers."""
    assert benchmark._safe_profile_attribute_dict(case.attributes) == case.expected


def _analysis_fixture_measurements() -> list[benchmark.Measurement]:
    """Build benchmark rows with nested profile spans for analyzer tests."""
    return [
        benchmark.Measurement(
            sha="a" * 40,
            short_sha="aaaaaaa",
            subject="profile search",
            command_name="profile-engine-search-all-conversations-limit-500",
            command_string=(
                "{venv}/bin/python scripts/profile_engine.py search-conversations {query}"
            ),
            samples=[0.5, 0.7],
            profile_payload={
                "profile_component": "search-conversations",
                "profile": {
                    "samples": [
                        {
                            "name": "search.collect",
                            "duration_seconds": 1.2,
                            "attributes": {
                                "agentgrep_source_count": 2,
                                "agentgrep_path_kind": "sqlite_db",
                                "agentgrep_query": "private-token",
                                "agentgrep_path": "/home/private/project",
                            },
                        },
                        {
                            "name": "search.discover",
                            "duration_seconds": 0.3,
                            "attributes": {"agentgrep_agent_count": 8},
                        },
                    ],
                },
            },
        ),
        benchmark.Measurement(
            sha="b" * 40,
            short_sha="bbbbbbb",
            subject="profile grep",
            command_name="profile-engine-grep-all-conversations-max-count-500",
            command_string="{venv}/bin/python scripts/profile_engine.py grep-conversations {query}",
            samples=[0.4],
            profile_payload={
                "profile_component": "grep-conversations",
                "profile": {
                    "samples": [
                        {
                            "name": "search.collect",
                            "duration_seconds": 1.0,
                            "attributes": {"agentgrep_source_count": 2},
                        },
                    ],
                },
            },
        ),
        benchmark.Measurement(
            sha="c" * 40,
            short_sha="ccccccc",
            subject="failed bench",
            command_name="profile-engine-find-all-prompts-limit-500",
            command_string="{venv}/bin/python scripts/profile_engine.py find-prompts",
            samples=[],
            status="bench_fail",
            error="boom",
        ),
    ]


class AnalysisLoadCase(t.NamedTuple):
    """One benchmark artifact shape the analyzer can load."""

    test_id: str
    suffix: str
    renderer_name: str


ANALYSIS_LOAD_CASES: tuple[AnalysisLoadCase, ...] = (
    AnalysisLoadCase(test_id="json-root", suffix=".json", renderer_name="render_json"),
    AnalysisLoadCase(test_id="ndjson-rows", suffix=".ndjson", renderer_name="render_ndjson"),
)


@pytest.mark.parametrize(
    "case",
    ANALYSIS_LOAD_CASES,
    ids=[c.test_id for c in ANALYSIS_LOAD_CASES],
)
def test_load_measurement_artifact_accepts_benchmark_json_and_ndjson(
    case: AnalysisLoadCase,
    tmp_path: pathlib.Path,
) -> None:
    """Analyzer input loading accepts the benchmark artifact formats."""
    rows = _analysis_fixture_measurements()
    artifact = tmp_path / f"benchmark{case.suffix}"
    renderer = getattr(benchmark, case.renderer_name)
    artifact.write_text(renderer(rows, []))

    loaded = benchmark.load_measurement_artifact(artifact)

    assert [row.command_name for row in loaded] == [row.command_name for row in rows]
    assert loaded[0].profile_payload == rows[0].profile_payload


def test_load_measurement_artifact_rejects_unknown_shapes(tmp_path: pathlib.Path) -> None:
    """Analyzer loading fails loud for non-benchmark JSON shapes."""
    artifact = tmp_path / "not-benchmark.json"
    artifact.write_text(json.dumps({"profile": {"samples": []}}))

    with pytest.raises(typer.BadParameter, match="unsupported benchmark artifact"):
        benchmark.load_measurement_artifact(artifact)


def test_build_analysis_report_summarizes_commands_and_profile_spans() -> None:
    """Analysis reports expose timing summaries and nested profile bottlenecks."""
    report = benchmark.build_analysis_report(
        _analysis_fixture_measurements(),
        artifact_label="benchmark.json",
        percentile_labels=["min", "avg", "p90"],
        top_spans=2,
        top_groups=2,
    )

    assert report.artifact_label == "benchmark.json"
    assert report.command_summaries[0].command_name == (
        "profile-engine-search-all-conversations-limit-500"
    )
    assert report.command_summaries[0].status_counts == {"ok": 1}
    assert report.command_summaries[0].sample_count == 2
    assert report.command_summaries[0].stats["avg"] == pytest.approx(0.6)
    assert report.top_spans[0].name == "search.collect"
    assert report.top_spans[0].duration_seconds == pytest.approx(1.2)
    assert "agentgrep_query" not in report.top_spans[0].attributes
    assert "agentgrep_path" not in report.top_spans[0].attributes
    assert report.top_spans[0].attributes["agentgrep_path_kind"] == "sqlite_db"
    assert report.span_groups[0].component == "search-conversations"
    assert report.span_groups[0].name == "search.collect"
    assert report.warnings == ["1 measurement(s) have no samples"]


class AnalysisRenderCase(t.NamedTuple):
    """One analysis reporter expectation."""

    test_id: str
    output_format: str
    expected_fragment: str


ANALYSIS_RENDER_CASES: tuple[AnalysisRenderCase, ...] = (
    AnalysisRenderCase(
        test_id="rich",
        output_format="rich",
        expected_fragment="benchmark analysis",
    ),
    AnalysisRenderCase(
        test_id="json",
        output_format="json",
        expected_fragment="agentgrep.benchmark.analysis",
    ),
    AnalysisRenderCase(
        test_id="ndjson",
        output_format="ndjson",
        expected_fragment="agentgrep.benchmark.analysis.command_summary",
    ),
)


@pytest.mark.parametrize(
    "case",
    ANALYSIS_RENDER_CASES,
    ids=[c.test_id for c in ANALYSIS_RENDER_CASES],
)
def test_render_analysis_report_supports_human_and_machine_formats(
    case: AnalysisRenderCase,
) -> None:
    """Analysis reports render as plain rich text, JSON, or NDJSON."""
    report = benchmark.build_analysis_report(
        _analysis_fixture_measurements(),
        artifact_label="/home/private/benchmark.json",
        percentile_labels=["min", "avg"],
        top_spans=1,
        top_groups=1,
    )

    text = benchmark.render_analysis_report(report, output_format=case.output_format)

    assert case.expected_fragment in text
    assert "\x1b[" not in text
    assert "/home/private" not in text
    assert "private-token" not in text
    if case.output_format == "json":
        payload = json.loads(text)
        assert payload["schema_version"] == 1
        assert payload["artifact_kind"] == "agentgrep.benchmark.analysis"
        assert payload["artifact_label"] == "benchmark.json"
        assert payload["top_spans"][0]["attributes"] == {
            "agentgrep_path_kind": "sqlite_db",
            "agentgrep_source_count": 2,
        }
    if case.output_format == "ndjson":
        rows = [json.loads(line) for line in text.splitlines()]
        assert {row["artifact_kind"] for row in rows} >= {
            "agentgrep.benchmark.analysis.command_summary",
            "agentgrep.benchmark.analysis.span",
            "agentgrep.benchmark.analysis.span_group",
            "agentgrep.benchmark.analysis.warning",
        }


def test_build_analysis_report_can_suppress_spans_and_groups() -> None:
    """Zero limits suppress optional span sections while keeping command summaries."""
    report = benchmark.build_analysis_report(
        _analysis_fixture_measurements(),
        artifact_label="benchmark.json",
        percentile_labels=["min"],
        top_spans=0,
        top_groups=0,
    )

    assert report.command_summaries
    assert report.top_spans == ()
    assert report.span_groups == ()


def test_analyze_command_writes_requested_format(
    tmp_path: pathlib.Path,
) -> None:
    """The analyze CLI reads a benchmark artifact and writes the rendered report."""
    artifact = tmp_path / "benchmark.json"
    output = tmp_path / "analysis.json"
    artifact.write_text(benchmark.render_json(_analysis_fixture_measurements(), []))

    rc = benchmark.main(
        [
            "analyze",
            str(artifact),
            "--format",
            "json",
            "--output",
            str(output),
            "--top-spans",
            "1",
            "--top-groups",
            "1",
        ],
    )

    payload = json.loads(output.read_text())
    assert rc == 0
    assert payload["artifact_kind"] == "agentgrep.benchmark.analysis"
    assert len(payload["top_spans"]) == 1
    assert len(payload["span_groups"]) == 1


def test_sanitize_command_string_replaces_local_context_values(tmp_path: pathlib.Path) -> None:
    """Rendered commands can be shared without local paths or query text."""
    context = {
        "repo": str(tmp_path),
        "venv": str(tmp_path / ".venv"),
        "query": "private-token",
        "sha": "a" * 40,
        "short_sha": "aaaaaaa",
    }
    raw = f"{tmp_path}/.venv/bin/agentgrep grep --max-count 500 private-token --repo {tmp_path}"

    sanitized = benchmark.sanitize_command_string(raw, context)

    assert str(tmp_path) not in sanitized
    assert "private-token" not in sanitized
    assert "{venv}/bin/agentgrep grep" in sanitized
    assert "--max-count 500 {query}" in sanitized
    assert "--repo {repo}" in sanitized


def test_run_for_commit_captures_profile_payload_and_sanitizes_command(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile-engine benches preserve child profile JSON next to timing samples."""
    config = benchmark.Config(
        bench={
            "profile": benchmark.BenchCommand(
                command=(
                    "{venv}/bin/python scripts/profile_engine.py grep-prompts "
                    "--agent all --max-count 500 {query}"
                ),
                default_query="private-token",
            ),
        },
        settings=benchmark.Settings(sync_command="", venv=".venv"),
    )
    profile_payload: dict[str, object] = {
        "kind": "search",
        "profile_component": "grep-prompts",
        "profile": {"samples": [{"name": "search.collect", "duration_seconds": 1.0}]},
    }
    captured_commands: list[str] = []

    monkeypatch.setattr(benchmark, "_checkout", lambda _sha, _repo: None)
    monkeypatch.setattr(
        benchmark,
        "time_command",
        lambda _cmd_str, **_kwargs: [0.25],
    )

    def fake_capture_profile_payload(
        cmd_str: str,
        *,
        timeout_seconds: int,
    ) -> tuple[dict[str, object] | None, str | None]:
        captured_commands.append(cmd_str)
        assert timeout_seconds == config.settings.timeout_seconds
        return profile_payload, None

    monkeypatch.setattr(benchmark, "_capture_profile_payload", fake_capture_profile_payload)

    rows = benchmark._run_one_commit(
        commit=benchmark.CommitRef(sha="a" * 40, short_sha="aaaaaaa", subject="subject"),
        config=config,
        bench_names=["profile"],
        query_overrides={},
        runs=1,
        warmup=0,
        no_sync=True,
        dry_run=False,
        repo=tmp_path,
        prefer_hyperfine=False,
        notify=lambda _message: None,
    )

    assert len(rows) == 1
    row = rows[0]
    assert captured_commands
    assert row.samples == [0.25]
    assert row.profile_payload == profile_payload
    assert row.profile_capture_error is None
    assert row.dry_run is False
    assert str(tmp_path) not in row.command_string
    assert "private-token" not in row.command_string
    assert "{venv}/bin/python scripts/profile_engine.py" in row.command_string
    assert "--max-count 500 {query}" in row.command_string


def test_run_for_commit_marks_dry_run_and_skips_profile_capture(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run rows are machine distinguishable without executing profiles."""
    config = benchmark.Config(
        bench={
            "profile": benchmark.BenchCommand(
                command="{venv}/bin/python scripts/profile_engine.py find-prompts --limit 500",
            ),
        },
        settings=benchmark.Settings(sync_command="", venv=".venv"),
    )

    monkeypatch.setattr(benchmark, "_checkout", lambda _sha, _repo: None)

    def fail_capture_profile_payload(
        _cmd_str: str,
        *,
        timeout_seconds: int,
    ) -> tuple[dict[str, object] | None, str | None]:
        msg = "dry-run should not capture profile payloads"
        raise AssertionError(msg)

    monkeypatch.setattr(benchmark, "_capture_profile_payload", fail_capture_profile_payload)

    rows = benchmark._run_one_commit(
        commit=benchmark.CommitRef(sha="a" * 40, short_sha="aaaaaaa", subject="subject"),
        config=config,
        bench_names=["profile"],
        query_overrides={},
        runs=1,
        warmup=0,
        no_sync=True,
        dry_run=True,
        repo=tmp_path,
        prefer_hyperfine=False,
        notify=lambda _message: None,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ok"
    assert row.samples == []
    assert row.dry_run is True
    assert row.profile_payload is None
    assert row.profile_capture_error is None


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


# ---------------------------------------------------------------------------
# Regression: _parse_percentile_labels + _select_bench_names reject bad input
# ---------------------------------------------------------------------------


class ValidationRejectCase(t.NamedTuple):
    """One argument-validation rejection expectation."""

    test_id: str
    fn_name: str
    args: tuple[t.Any, ...]
    match: str


VALIDATION_REJECT_CASES: tuple[ValidationRejectCase, ...] = (
    ValidationRejectCase(
        test_id="bad-percentile-label",
        fn_name="_parse_percentile_labels",
        args=("min,wat,p99",),
        match="unknown stat label",
    ),
    ValidationRejectCase(
        test_id="unknown-bench-name",
        fn_name="_select_bench_names",
        args=(
            benchmark.Config(
                bench={"grep": benchmark.BenchCommand(command="echo x")},
            ),
            "bogus",
        ),
        match="unknown benchmark name",
    ),
    ValidationRejectCase(
        test_id="empty-bench-selector",
        fn_name="_select_bench_names",
        args=(
            benchmark.Config(
                bench={"grep": benchmark.BenchCommand(command="echo x")},
            ),
            "",
        ),
        match="did not select any benchmarks",
    ),
    ValidationRejectCase(
        test_id="separator-only-bench-selector",
        fn_name="_select_bench_names",
        args=(
            benchmark.Config(
                bench={"grep": benchmark.BenchCommand(command="echo x")},
            ),
            ",,,",
        ),
        match="did not select any benchmarks",
    ),
)


@pytest.mark.parametrize(
    "case",
    VALIDATION_REJECT_CASES,
    ids=[c.test_id for c in VALIDATION_REJECT_CASES],
)
def test_validation_rejects_bad_input(case: ValidationRejectCase) -> None:
    """Argument-validation helpers raise BadParameter on invalid input."""
    fn = getattr(benchmark, case.fn_name)
    with pytest.raises(typer.BadParameter, match=case.match):
        fn(*case.args)


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


# ---------------------------------------------------------------------------
# Regression: main() exit-code propagation
# ---------------------------------------------------------------------------


class MainExitCodeCase(t.NamedTuple):
    """One main() exit-code expectation."""

    test_id: str
    argv: list[str]
    expected_rc: int


MAIN_EXIT_CODE_CASES: tuple[MainExitCodeCase, ...] = (
    MainExitCodeCase(
        test_id="show-config-returns-0",
        argv=["show-config"],
        expected_rc=0,
    ),
    MainExitCodeCase(
        test_id="no-benches-returns-2",
        argv=["run", "--config", "__EMPTY_TOML__", "--no-progress"],
        expected_rc=2,
    ),
)


@pytest.mark.parametrize(
    "case",
    MAIN_EXIT_CODE_CASES,
    ids=[c.test_id for c in MAIN_EXIT_CODE_CASES],
)
def test_main_returns_expected_exit_code(
    case: MainExitCodeCase,
    tmp_path: pathlib.Path,
) -> None:
    """main() propagates typer.Exit(code=N) as an int return, not 0."""
    argv = list(case.argv)
    if "__EMPTY_TOML__" in argv:
        empty = tmp_path / "empty.toml"
        empty.write_text("")
        argv[argv.index("__EMPTY_TOML__")] = str(empty)
    rc = benchmark.main(argv)
    assert rc == case.expected_rc


def test_main_formats_unknown_benchmark_selector_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown ``--commands`` selectors return a one-line CLI error."""
    rc = benchmark.main(["run", "--commands", "bogus", "--no-progress"])

    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown benchmark name" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Regression: _select_targets converts CalledProcessError → BadParameter
# ---------------------------------------------------------------------------


def test_select_targets_converts_git_error_to_bad_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``git rev-parse`` surfaces as BadParameter, not a traceback."""

    def _fail(
        **_kwargs: t.Any,
    ) -> t.NoReturn:
        raise subprocess.CalledProcessError(
            returncode=128,
            cmd=("git", "rev-parse", "bogus"),
            stderr="fatal: ambiguous argument 'bogus'",
        )

    monkeypatch.setattr(benchmark, "resolve_target", _fail)
    with pytest.raises(typer.BadParameter, match="git failed resolving target"):
        benchmark._select_targets(
            target="bogus",
            range_spec=None,
            lookback=None,
            from_trunk_back=None,
            tags=False,
            commits=None,
            head_vs_trunk=False,
            trunk="master",
        )


# ---------------------------------------------------------------------------
# Regression: aggregate table has no duplicate max column
# ---------------------------------------------------------------------------


def _make_multi_commit_measurements() -> list[t.Any]:
    """Two ok measurements for the same command — triggers the aggregate table."""
    return [
        benchmark.Measurement(
            sha="a" * 40,
            short_sha="aaaaaaa",
            subject="first commit",
            command_name="grep",
            command_string="cmd",
            samples=[0.5, 0.6],
        ),
        benchmark.Measurement(
            sha="b" * 40,
            short_sha="bbbbbbb",
            subject="second commit",
            command_name="grep",
            command_string="cmd",
            samples=[0.7, 0.8],
        ),
    ]


def test_render_markdown_no_duplicate_max_column() -> None:
    """render_markdown header row has exactly one ``max`` column."""
    ms = _make_multi_commit_measurements()
    md = benchmark.render_markdown(ms, ["min", "avg", "max"])
    header_line = next(line for line in md.splitlines() if line.startswith("| sha"))
    assert header_line.count("max") == 1


def test_render_rich_no_duplicate_max_column() -> None:
    """render_rich aggregate section has exactly one ``max`` header."""
    ms = _make_multi_commit_measurements()
    text = benchmark.render_rich(ms, ["min", "avg", "max"])
    agg_section = text[text.index("Distribution across") :]
    header_line = next(line for line in agg_section.splitlines() if "min" in line and "avg" in line)
    assert header_line.count("max") == 1


def test_render_rich_reports_nested_profile_payload_top_spans() -> None:
    """Rich benchmark output can show slow profile spans beside timing rows."""
    measurement = benchmark.Measurement(
        sha="a" * 40,
        short_sha="aaaaaaa",
        subject="profile commit",
        command_name="profile-engine-grep-all-prompts-max-count-500",
        command_string="{venv}/bin/python scripts/profile_engine.py grep-prompts {query}",
        samples=[0.25],
        profile_payload={
            "profile_component": "grep-prompts",
            "profile": {
                "samples": [
                    {
                        "name": "search.discover",
                        "duration_seconds": 0.1,
                        "attributes": {"agentgrep_source_count": 2},
                    },
                    {
                        "name": "search.collect",
                        "duration_seconds": 1.2,
                        "attributes": {
                            "agentgrep_source_count": 1,
                            "agentgrep_query": "private-token",
                            "agentgrep_path": "/home/private/project",
                        },
                    },
                ],
            },
        },
    )

    text = benchmark.render_rich([measurement], ["min", "avg"], top_spans=1)

    assert "profile payload slowest spans" in text
    assert "grep-prompts" in text
    assert "search.collect" in text
    assert "search.discover" not in text
    assert "private-token" not in text
    assert "/home/private" not in text


def test_render_rich_top_spans_zero_suppresses_profile_payload_table() -> None:
    """Users can disable nested profile span rendering for compact rich output."""
    measurement = benchmark.Measurement(
        sha="a" * 40,
        short_sha="aaaaaaa",
        subject="profile commit",
        command_name="profile",
        command_string="{venv}/bin/python scripts/profile_engine.py grep-prompts",
        samples=[0.25],
        profile_payload={
            "profile_component": "grep-prompts",
            "profile": {
                "samples": [
                    {
                        "name": "search.collect",
                        "duration_seconds": 1.2,
                        "attributes": {"agentgrep_source_count": 1},
                    },
                ],
            },
        },
    )

    text = benchmark.render_rich([measurement], ["min"], top_spans=0)

    assert "profile payload slowest spans" not in text
    assert "search.collect" not in text


def test_run_accepts_top_spans_flag_for_rich_output(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run command exposes --top-spans without requiring a real benchmark."""
    config = benchmark.Config(
        bench={"profile": benchmark.BenchCommand(command="echo {query}", default_query="tmux")},
        settings=benchmark.Settings(sync_command=""),
    )
    output = tmp_path / "rich.txt"
    monkeypatch.setattr(benchmark, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        benchmark,
        "_select_targets",
        lambda **_kwargs: [
            benchmark.CommitRef(sha="a" * 40, short_sha="aaaaaaa", subject="subject"),
        ],
    )
    monkeypatch.setattr(benchmark, "_git_dirty", lambda _repo: False)
    monkeypatch.setattr(benchmark, "_git", lambda *_args, **_kwargs: "streamline-02")
    monkeypatch.setattr(benchmark, "_install_restore_guard", lambda **_kwargs: None)
    monkeypatch.setattr(
        benchmark,
        "_run_one_commit",
        lambda **_kwargs: [
            benchmark.Measurement(
                sha="a" * 40,
                short_sha="aaaaaaa",
                subject="subject",
                command_name="profile",
                command_string="echo {query}",
                samples=[0.1],
            ),
        ],
    )

    rc = benchmark.main(
        [
            "run",
            "--commands",
            "profile",
            "--format",
            "rich",
            "--top-spans",
            "3",
            "--output",
            str(output),
            "--no-progress",
        ],
    )

    assert rc == 0
    assert output.exists()


def test_run_accepts_profile_engine_command_group(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run command expands ``--commands profile-engine`` before execution."""
    config = benchmark.Config(
        bench={
            name: benchmark.BenchCommand(command=f"echo {name}", default_query="tmux")
            for name in benchmark.PROFILE_ENGINE_BENCHMARK_GROUP
        },
        settings=benchmark.Settings(sync_command=""),
    )
    output = tmp_path / "profile-engine.json"
    captured_bench_names: list[str] = []
    monkeypatch.setattr(benchmark, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        benchmark,
        "_select_targets",
        lambda **_kwargs: [
            benchmark.CommitRef(sha="a" * 40, short_sha="aaaaaaa", subject="subject"),
        ],
    )
    monkeypatch.setattr(benchmark, "_git_dirty", lambda _repo: False)
    monkeypatch.setattr(benchmark, "_git", lambda *_args, **_kwargs: "streamline-02")
    monkeypatch.setattr(benchmark, "_install_restore_guard", lambda **_kwargs: None)

    def run_one_commit(**kwargs: t.Any) -> list[benchmark.Measurement]:
        names = t.cast("list[str]", kwargs["bench_names"])
        captured_bench_names.extend(names)
        return [
            benchmark.Measurement(
                sha="a" * 40,
                short_sha="aaaaaaa",
                subject="subject",
                command_name=name,
                command_string=config.bench[name].command,
                samples=[0.1],
            )
            for name in names
        ]

    monkeypatch.setattr(benchmark, "_run_one_commit", run_one_commit)

    rc = benchmark.main(
        [
            "run",
            "--commands",
            "profile-engine",
            "--format",
            "json",
            "--output",
            str(output),
            "--no-progress",
        ],
    )

    assert rc == 0
    assert captured_bench_names == list(benchmark.PROFILE_ENGINE_BENCHMARK_GROUP)
    assert output.exists()


def test_list_commands_prints_command_groups(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command discovery output includes ergonomic command groups."""
    config = benchmark.Config(
        bench={
            name: benchmark.BenchCommand(command=f"echo {name}", default_query="tmux")
            for name in benchmark.PROFILE_ENGINE_BENCHMARK_GROUP
        },
    )
    monkeypatch.setattr(benchmark, "load_config", lambda **_kwargs: config)

    benchmark.cmd_list_commands()

    output = capsys.readouterr().out
    assert "command groups:" in output
    assert "profile-engine:" in output
    assert "profile-engine-grep-all-prompts-max-count-500" in output


# ---------------------------------------------------------------------------
# Regression: load_config rejects malformed TOML, extra keys, missing fields
# ---------------------------------------------------------------------------


class ConfigErrorCase(t.NamedTuple):
    """One load_config error expectation."""

    test_id: str
    toml_content: str
    match: str


CONFIG_ERROR_CASES: tuple[ConfigErrorCase, ...] = (
    ConfigErrorCase(
        test_id="malformed-toml",
        toml_content="[settings",
        match="failed to parse",
    ),
    ConfigErrorCase(
        test_id="extra-settings-key-rejected",
        toml_content='[settings]\nmystery = "x"\n',
        match="Extra inputs are not permitted",
    ),
    ConfigErrorCase(
        test_id="missing-required-bench-field",
        toml_content='[bench.grep]\ndefault_query = "x"\n',
        match="Field required",
    ),
)


@pytest.mark.parametrize(
    "case",
    CONFIG_ERROR_CASES,
    ids=[c.test_id for c in CONFIG_ERROR_CASES],
)
def test_load_config_rejects_invalid_toml(
    case: ConfigErrorCase,
    tmp_path: pathlib.Path,
) -> None:
    """Malformed TOML and schema violations surface as BadParameter."""
    toml_file = tmp_path / "benchmark.toml"
    toml_file.write_text(case.toml_content)
    missing_local = tmp_path / "no-local.toml"
    with pytest.raises(typer.BadParameter, match=case.match):
        benchmark.load_config(config_path=toml_file, local_path=missing_local)


# ---------------------------------------------------------------------------
# Regression: Settings.runs=0 rejected by Field(ge=1)
# ---------------------------------------------------------------------------


def test_load_config_rejects_runs_zero_via_cli_overrides(
    tmp_path: pathlib.Path,
) -> None:
    """``runs=0`` via cli_overrides is rejected by the pydantic ge=1 bound."""
    valid = tmp_path / "benchmark.toml"
    valid.write_text('[bench.echo]\ncommand = "echo"\n')
    with pytest.raises(typer.BadParameter, match="greater than or equal to 1"):
        benchmark.load_config(
            config_path=valid,
            local_path=tmp_path / "no-local.toml",
            cli_overrides={"settings": {"runs": 0}},
        )


# ---------------------------------------------------------------------------
# Regression: cli_overrides bypass pydantic validators
# ---------------------------------------------------------------------------


class CliOverrideRejectCase(t.NamedTuple):
    """One invalid cli_overrides expectation."""

    test_id: str
    cli_overrides: dict[str, t.Any]
    match: str


CLI_OVERRIDE_REJECT_CASES: tuple[CliOverrideRejectCase, ...] = (
    CliOverrideRejectCase(
        test_id="warmup-negative",
        cli_overrides={"settings": {"warmup": -1}},
        match="greater than or equal to 0",
    ),
    CliOverrideRejectCase(
        test_id="timeout-seconds-zero",
        cli_overrides={"settings": {"timeout_seconds": 0}},
        match="greater than or equal to 1",
    ),
)


@pytest.mark.parametrize(
    "case",
    CLI_OVERRIDE_REJECT_CASES,
    ids=[c.test_id for c in CLI_OVERRIDE_REJECT_CASES],
)
def test_load_config_rejects_invalid_cli_overrides(
    case: CliOverrideRejectCase,
    tmp_path: pathlib.Path,
) -> None:
    """CLI overrides route through model_validate so pydantic bounds fire."""
    valid = tmp_path / "benchmark.toml"
    valid.write_text('[bench.echo]\ncommand = "echo"\n')
    with pytest.raises(typer.BadParameter, match=case.match):
        benchmark.load_config(
            config_path=valid,
            local_path=tmp_path / "no-local.toml",
            cli_overrides=case.cli_overrides,
        )


# ---------------------------------------------------------------------------
# Regression: --output pre-flight rejects bad paths before git checkout
# ---------------------------------------------------------------------------


def test_main_exits_2_when_output_parent_missing() -> None:
    """``--output /nonexistent/dir/x.md`` is caught before any git interaction."""
    rc = benchmark.main(
        ["run", "--output", "/nonexistent/dir/x.md", "--no-progress"],
    )
    assert rc == 2


def test_main_exits_2_when_output_is_directory(tmp_path: pathlib.Path) -> None:
    """``--output <directory>`` is caught before any git interaction."""
    rc = benchmark.main(
        ["run", "--output", str(tmp_path), "--no-progress"],
    )
    assert rc == 2
