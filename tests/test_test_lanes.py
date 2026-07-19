"""Behavioral contracts for pytest lane selection."""

from __future__ import annotations

import pytest

pytest_plugins = ("pytester",)


def test_repository_registers_lane_policy(pytestconfig: pytest.Config) -> None:
    """The root configuration keeps the local selector and marker schema explicit."""
    addopts = pytestconfig.getini("addopts")
    markers = pytestconfig.getini("markers")

    assert addopts[-2:] == ["-m", "not slow"]
    for marker in ("documentation", "legacy", "mcp", "setup", "slow", "tui"):
        assert any(line.startswith(f"{marker}:") for line in markers)


def test_default_lane_and_empty_override_follow_pytest_semantics(
    pytester: pytest.Pytester,
) -> None:
    """Unmarked tests run locally and an empty CLI expression restores all tests."""
    pytester.makeini(
        """
        [pytest]
        addopts = --strict-markers -m "not slow"
        asyncio_default_fixture_loop_scope = function
        markers =
            slow: opt-in test
            tui: Textual resource test
        """,
    )
    pytester.makepyfile(
        """
        import pytest

        def test_fast():
            pass

        @pytest.mark.tui
        def test_fast_tui():
            pass

        @pytest.mark.slow
        def test_slow():
            pass
        """,
    )

    default = pytester.runpytest("-q")
    default.assert_outcomes(passed=2, deselected=1)

    exhaustive = pytester.runpytest("-q", "-m", "")
    exhaustive.assert_outcomes(passed=3)


def test_strict_markers_reject_lane_typos(pytester: pytest.Pytester) -> None:
    """A misspelled lane fails collection instead of silently skipping coverage."""
    pytester.makeini(
        """
        [pytest]
        addopts = --strict-markers
        asyncio_default_fixture_loop_scope = function
        markers = slow: opt-in test
        """,
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.sloow
        def test_typo():
            pass
        """,
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*'sloow' not found in `markers` configuration option*"])
