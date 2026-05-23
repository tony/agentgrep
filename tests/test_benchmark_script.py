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


# ---------------------------------------------------------------------------
# Regression: _parse_percentile_labels + _select_bench_names reject bad input
# (was: typer.MissingParameter AttributeError in the except clause)
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
# (was: standalone_mode=False converted typer.Exit to return value,
# but main() never captured it — always returned 0)
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


# ---------------------------------------------------------------------------
# Regression: _select_targets converts CalledProcessError → BadParameter
# (was: uncaught traceback on bad git refs)
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
# (was: render_rich added add_column("max") on top of the one from
# percentile_labels, doubling the column in the header)
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


# ---------------------------------------------------------------------------
# Regression: load_config rejects malformed TOML, extra keys, missing fields
# (was: uncaught pydantic.ValidationError / tomllib.TOMLDecodeError tracebacks)
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
# (was: --runs 0 deadlocked the hyperfine path because the constraint
# wasn't enforced — model_copy(update=...) skipped validators)
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
# (was: model_copy(update=...) silently skipped Field(ge=…) constraints)
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
# (was: FileNotFoundError / IsADirectoryError traceback at the end of a run)
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
