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
