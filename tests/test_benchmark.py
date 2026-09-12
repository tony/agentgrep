"""Tests for scripts/benchmark.py.

The harness lives outside the ``src/`` package, so it is loaded from its file
path, the same way ``tests/test_mcp_swap.py`` loads its script.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import typing as t

import pytest

pytestmark = pytest.mark.setup

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "benchmark.py"

_spec = importlib.util.spec_from_file_location("benchmark", _SCRIPT)
assert _spec and _spec.loader
benchmark = importlib.util.module_from_spec(_spec)
sys.modules["benchmark"] = benchmark
_spec.loader.exec_module(benchmark)


class TimeoutCase(t.NamedTuple):
    """One bench timeout resolved against the global setting."""

    test_id: str
    bench_timeout: int | None
    settings_timeout: int
    expected: int


TIMEOUT_CASES = (
    TimeoutCase("inherits-global", None, 300, 300),
    TimeoutCase("bench-extends-global", 1200, 300, 1200),
    TimeoutCase("bench-shortens-global", 60, 300, 60),
)


@pytest.mark.parametrize(
    list(TimeoutCase._fields),
    TIMEOUT_CASES,
    ids=[case.test_id for case in TIMEOUT_CASES],
)
def test_bench_timeout_replaces_the_global_limit(
    test_id: str,
    bench_timeout: int | None,
    settings_timeout: int,
    expected: int,
) -> None:
    """A bench's own ``timeout_seconds`` wins; without one it inherits."""
    bench = benchmark.BenchCommand(command="true", timeout_seconds=bench_timeout)
    settings = benchmark.Settings(timeout_seconds=settings_timeout)
    assert benchmark._bench_timeout_seconds(bench, settings) == expected


def test_all_agent_conversation_benches_outlast_the_global_limit(
    tmp_path: pathlib.Path,
) -> None:
    """Every committed all-agent conversation bench sets its own timeout.

    One run reads every agent's conversation stores, which takes minutes on a
    large corpus, so a warmup plus three runs overran the 300 s default and
    the bench recorded no samples on any ref.
    """
    config = benchmark.load_config(local_path=tmp_path / "absent.local.toml")
    heavy = {name: bench for name, bench in config.bench.items() if "all-conversations" in name}
    assert heavy
    short = {
        name: bench.timeout_seconds
        for name, bench in heavy.items()
        if benchmark._bench_timeout_seconds(bench, config.settings)
        <= config.settings.timeout_seconds
    }
    assert short == {}
