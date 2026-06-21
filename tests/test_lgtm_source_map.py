"""Tests for scripts/lgtm/generate_pyroscope_source_map.py."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "lgtm" / "generate_pyroscope_source_map.py"

_spec = importlib.util.spec_from_file_location("lgtm_source_map", _SCRIPT)
assert _spec and _spec.loader
lgtm_source_map = importlib.util.module_from_spec(_spec)
sys.modules["lgtm_source_map"] = lgtm_source_map
_spec.loader.exec_module(lgtm_source_map)


def test_build_mappings_covers_agentgrep_and_otel_packages(tmp_path: pathlib.Path) -> None:
    """Generated source maps should cover app code and known OTel dependencies."""
    repo_root = tmp_path / "agentgrep"
    site_packages = repo_root / ".venv" / "lib" / "python3.14" / "site-packages"
    module_origins = {
        "opentelemetry.exporter.otlp.proto.common": site_packages
        / "opentelemetry"
        / "exporter"
        / "otlp"
        / "proto"
        / "common"
        / "__init__.py",
        "opentelemetry.instrumentation.sqlite3": site_packages
        / "opentelemetry"
        / "instrumentation"
        / "sqlite3"
        / "__init__.py",
        "opentelemetry.sdk": site_packages / "opentelemetry" / "sdk",
    }
    distribution_versions = {
        "opentelemetry-exporter-otlp-proto-common": "1.42.1",
        "opentelemetry-instrumentation-sqlite3": "0.63b1",
        "opentelemetry-sdk": "1.42.1",
    }

    mappings = lgtm_source_map.build_mappings(
        repo_root=repo_root,
        module_origins=module_origins,
        distribution_versions=distribution_versions,
    )

    rendered = lgtm_source_map.render_pyroscope_yaml(mappings)

    assert str(repo_root) not in rendered
    assert str(site_packages) not in rendered
    assert "prefix: src" in rendered
    assert "path: src" in rendered
    assert "prefix: opentelemetry/exporter/otlp/proto/common" in rendered
    assert "owner: open-telemetry" in rendered
    assert "repo: opentelemetry-python" in rendered
    assert "ref: v1.42.1" in rendered
    assert (
        "path: exporter/opentelemetry-exporter-otlp-proto-common/src/"
        "opentelemetry/exporter/otlp/proto/common"
    ) in rendered
    assert "prefix: opentelemetry/instrumentation/sqlite3" in rendered
    assert "repo: opentelemetry-python-contrib" in rendered
    assert "ref: v0.63b1" in rendered
    assert "prefix: opentelemetry/sdk" in rendered
    assert "path: opentelemetry-sdk/src/opentelemetry/sdk" in rendered


def test_render_pyroscope_yaml_has_no_project_local_path_without_input() -> None:
    """Committed templates should not carry this checkout's absolute path."""
    rendered = lgtm_source_map.render_pyroscope_yaml([])

    assert "prefix:" not in rendered
    assert "source_code:" in rendered
    assert "mappings: []" in rendered
