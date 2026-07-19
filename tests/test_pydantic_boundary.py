"""Contracts for the required Pydantic dependency boundary."""

from __future__ import annotations

import json
import pathlib

import agentgrep
import agentgrep._types as agentgrep_types
from agentgrep.cli import render, serializers


def test_cli_json_uses_direct_serializers_without_optional_bridge() -> None:
    """Direct serializers remain JSON-safe without an optional bridge."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("history.jsonl"),
        text="needle",
    )
    serialized = agentgrep.serialize_search_record(record)
    envelope = agentgrep.build_envelope("search", {"terms": ["needle"]}, [dict(serialized)])

    assert json.loads(json.dumps(envelope, ensure_ascii=False)) == envelope

    bridge_names = {
        "PydanticModule",
        "PydanticTypeAdapter",
        "PydanticTypeAdapterFactory",
        "maybe_build_pydantic",
        "maybe_use_pydantic",
    }
    modules_and_names = (
        (agentgrep, bridge_names),
        (agentgrep_types, bridge_names),
        (render, bridge_names),
        (serializers, bridge_names),
    )
    leaked = {
        f"{module.__name__}.{name}"
        for module, names in modules_and_names
        for name in names
        if name in vars(module) or name in module.__all__
    }

    assert not leaked
