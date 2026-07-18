"""Guard tests for the ADR 0010 module-boundary contract.

The package facade re-exports implementation that lives in single-responsibility
modules. To keep the dependency graph pointing one direction, no module may
import the ``agentgrep`` *facade* with a bare ``import agentgrep`` — implementation
modules import their dependencies directly from the owning module. Only the
package entry points (``__init__``/``__main__``) may name the facade.
"""

from __future__ import annotations

import pathlib
import re

import agentgrep

_BARE_FACADE_IMPORT = re.compile(r"(?m)^[ \t]*import agentgrep[ \t]*$")
_ALLOWED = {"__init__.py", "__main__.py"}


def _package_root() -> pathlib.Path:
    return pathlib.Path(agentgrep.__file__).parent


def test_no_module_imports_the_facade() -> None:
    """No module outside the entry points does a bare ``import agentgrep``."""
    offenders = [
        path.relative_to(_package_root()).as_posix()
        for path in _package_root().rglob("*.py")
        if path.name not in _ALLOWED and _BARE_FACADE_IMPORT.search(path.read_text())
    ]
    assert not offenders, f"bare `import agentgrep` (ADR 0010): {offenders}"


def test_engine_does_not_import_the_facade() -> None:
    """The engine owns its logic (ADR 0004); it never imports the facade root."""
    engine = _package_root() / "_engine"
    offenders = [
        path.relative_to(_package_root()).as_posix()
        for path in engine.rglob("*.py")
        if _BARE_FACADE_IMPORT.search(path.read_text())
    ]
    assert not offenders, f"_engine imports the facade (ADR 0010): {offenders}"


def test_cli_render_split_reexports_are_neutral() -> None:
    """``cli.render`` re-exports its moved serializers/renderers byte-stably.

    The JSON serializers moved to ``cli.serializers`` and the text
    formatters to ``cli.renderers``; ``cli.render`` keeps the subcommand
    dispatchers and re-imports the moved public names so both the facade
    re-exports and ``cli.render.X`` stay byte-stable (ADR 0010).
    """
    import agentgrep
    from agentgrep.cli import render, renderers, serializers

    assert render.serialize_search_record is serializers.serialize_search_record
    assert render.build_envelope is serializers.build_envelope
    assert render.format_grep_record is renderers.format_grep_record
    assert render.filter_find_records is renderers.filter_find_records
    # facade re-export points at the same objects
    assert agentgrep.serialize_search_record is serializers.serialize_search_record
    assert agentgrep.format_grep_record is renderers.format_grep_record


def test_facade_public_surface_is_explicit() -> None:
    """The facade declares an explicit, well-formed ``__all__`` (ADR 0010/0006).

    ``__all__`` is the public surface ADR 0006 governs. Every entry must
    resolve, none may be private, and there are no duplicates. The exact
    membership is snapshotted by :func:`test_facade_public_surface_snapshot`
    so any change to the surface is a deliberate edit.
    """
    import agentgrep

    assert isinstance(agentgrep.__all__, tuple)
    missing = [name for name in agentgrep.__all__ if not hasattr(agentgrep, name)]
    assert not missing, f"__all__ names with no attribute: {missing}"
    assert not any(name.startswith("_") for name in agentgrep.__all__)
    assert len(agentgrep.__all__) == len(set(agentgrep.__all__))


def test_facade_public_surface_snapshot(snapshot: object) -> None:
    """Snapshot the sorted public surface so additions/removals are reviewed."""
    import agentgrep

    assert sorted(agentgrep.__all__) == snapshot


def test_parser_registry_matches_records_adapter_set() -> None:
    """The merged parser registry equals ``ITER_SOURCE_RECORD_ADAPTERS``.

    ``records.py`` must not import adapter code (it sits below the parsers),
    so its dispatchable-adapter frozenset cannot be derived from the registry.
    This equality keeps the two in lockstep: an adapter id added to only one
    side fails here instead of silently yielding nothing.
    """
    import agentgrep.adapters as adapters
    from agentgrep.records import ITER_SOURCE_RECORD_ADAPTERS

    assert set(adapters.PARSER_REGISTRY) == set(ITER_SOURCE_RECORD_ADAPTERS)


def test_stream_parser_specs_pin_the_planning_contract() -> None:
    """Exactly the known stream-aware ids carry ``raw_skip_line``/``reverse``.

    Which parsers receive the raw-prefilter/bounded-reverse arguments is
    planning-visible behavior (ADR 0004); flipping a row between spec shapes
    silently changes what the engine's scan strategies deliver.
    """
    import agentgrep.adapters as adapters

    stream_ids = {
        adapter_id
        for adapter_id, spec in adapters.PARSER_REGISTRY.items()
        if isinstance(spec, adapters.StreamParserSpec)
    }
    assert stream_ids == {
        "antigravity_cli.history_jsonl.v1",
        "claude.projects_jsonl.v1",
        "codex.history_json.v1",
        "codex.history_jsonl.v1",
        "codex.sessions_jsonl.v1",
        "grok.prompt_history_jsonl.v1",
        "grok.sessions_jsonl.v1",
        "pi.sessions_jsonl.v1",
    }


def test_adapters_split_reexports_are_neutral() -> None:
    """The adapters package re-exports its moved names byte-stably.

    Identity (not equality) keeps the single ``@functools.cache`` instance on
    ``store_descriptor_for_record`` and the facade's private vscode helpers
    pointing at the owning modules (ADR 0010).
    """
    import agentgrep
    import agentgrep.adapters as adapters
    from agentgrep.adapters import _extract, _store_roles, cursor_ide, vscode

    assert agentgrep.iter_source_records is adapters.iter_source_records
    assert agentgrep.extract_message_text is _extract.extract_message_text
    assert agentgrep.build_search_record is _extract.build_search_record
    assert agentgrep.parse_cursor_state_db is cursor_ide.parse_cursor_state_db
    assert agentgrep.store_descriptor_for_record is _store_roles.store_descriptor_for_record
    assert agentgrep._vscode_uri_to_path is vscode._vscode_uri_to_path
    assert agentgrep._vscode_workspace_cwd is vscode._vscode_workspace_cwd
