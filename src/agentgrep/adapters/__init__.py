"""Per-agent store parsers, the record-normalization layer, and dispatch.

Reads Codex, Claude, Cursor, Gemini, Antigravity, Grok, Pi, OpenCode, and
VS Code store files and databases into normalized
:class:`~agentgrep.records.SearchRecord` objects. Each per-agent module owns
its parsers plus a typed registry fragment mapping its adapter ids to them;
this facade merges the fragments into :data:`PARSER_REGISTRY` and dispatches
through ``iter_source_records`` with a single lookup. The ``extract_*`` /
``build_search_record`` helpers in ``_extract`` are the shared normalization
seam. Depends on the readers (I/O floor), the record types, the store
catalog, and stdlib; it imports no engine or frontend.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

from agentgrep.adapters._extract import (
    build_search_record,
    candidate_from_mapping,
    extract_conversation_id,
    extract_message_text,
    extract_model,
    extract_role,
    extract_session_id,
    extract_timestamp,
    extract_title,
    flatten_content_value,
    flatten_summary_bullets,
    iter_message_candidates,
    iter_text_fragments,
)
from agentgrep.adapters._generic import (
    parse_file_metadata_summary_file,
    parse_hooks_summary_file,
    parse_json_summary_file,
    parse_text_store_file,
    parse_toml_summary_file,
)
from agentgrep.adapters._registry import (
    AnyParserSpec,
    ParserSpec,
    StreamParserSpec,
    merge_parser_specs,
)
from agentgrep.adapters._store_roles import (
    find_store_roles_for_type_filter,
    store_descriptor_for_record,
    store_role_for_record,
)
from agentgrep.adapters.antigravity_cli import (
    _ANTIGRAVITY_CLI_PARSERS,
    parse_antigravity_cli_conversation_db,
    parse_antigravity_cli_history_file,
    parse_antigravity_cli_transcript,
)
from agentgrep.adapters.antigravity_ide import _ANTIGRAVITY_IDE_PARSERS
from agentgrep.adapters.claude import (
    _CLAUDE_PARSERS,
    CLAUDE_PASTE_HASH_RE,
    CLAUDE_PASTE_REF_RE,
    claude_event_is_human_authored,
    claude_history_paste_text,
    expand_claude_history_pastes,
    parse_claude_history_file,
    parse_claude_project_file,
    parse_claude_settings_file,
    parse_claude_store_db,
    parse_claude_task_file,
    parse_claude_team_file,
    parse_claude_todo_file,
    parse_claude_usage_facet,
)
from agentgrep.adapters.codex import (
    _CODEX_PARSERS,
    codex_event_is_human_authored,
    parse_codex_external_imports_file,
    parse_codex_goals_db,
    parse_codex_history_file,
    parse_codex_legacy_session_file,
    parse_codex_logs_db,
    parse_codex_memories_db,
    parse_codex_session_file,
    parse_codex_session_index_file,
    parse_codex_state_db,
)
from agentgrep.adapters.cursor_cli import (
    _CURSOR_CLI_PARSERS,
    parse_cursor_ai_tracking_db,
    parse_cursor_cli_chats_db,
    parse_cursor_cli_transcript,
    parse_cursor_prompt_history,
)
from agentgrep.adapters.cursor_ide import (
    _CURSOR_IDE_PARSERS,
    iter_cursor_prompt_candidates,
    parse_cursor_state_db,
)
from agentgrep.adapters.gemini import (
    _GEMINI_PARSERS,
    parse_gemini_chat_file,
    parse_gemini_chat_legacy_file,
    parse_gemini_logs_file,
)
from agentgrep.adapters.grok import (
    _GROK_PARSERS,
    parse_grok_chat_history,
    parse_grok_prompt_history,
    parse_grok_session_search_db,
    parse_grok_subagents,
)
from agentgrep.adapters.opencode import (
    _OPENCODE_PARSERS,
    parse_opencode_db,
)
from agentgrep.adapters.pi import (
    _PI_PARSERS,
    parse_pi_context_mode_db,
    parse_pi_session_file,
)
from agentgrep.adapters.vscode import (
    _VSCODE_PARSERS,
    _vscode_uri_to_path as _vscode_uri_to_path,
    _vscode_workspace_cwd as _vscode_workspace_cwd,
    parse_vscode_chat_session,
    parse_vscode_inline_history,
)
from agentgrep.records import RawJsonlSkipLine, SearchRecord, SourceHandle

__all__ = (
    "CLAUDE_PASTE_HASH_RE",
    "CLAUDE_PASTE_REF_RE",
    "build_search_record",
    "candidate_from_mapping",
    "claude_event_is_human_authored",
    "claude_history_paste_text",
    "codex_event_is_human_authored",
    "expand_claude_history_pastes",
    "extract_conversation_id",
    "extract_message_text",
    "extract_model",
    "extract_role",
    "extract_session_id",
    "extract_timestamp",
    "extract_title",
    "find_store_roles_for_type_filter",
    "flatten_content_value",
    "flatten_summary_bullets",
    "iter_cursor_prompt_candidates",
    "iter_message_candidates",
    "iter_source_records",
    "iter_text_fragments",
    "parse_antigravity_cli_conversation_db",
    "parse_antigravity_cli_history_file",
    "parse_antigravity_cli_transcript",
    "parse_claude_history_file",
    "parse_claude_project_file",
    "parse_claude_settings_file",
    "parse_claude_store_db",
    "parse_claude_task_file",
    "parse_claude_team_file",
    "parse_claude_todo_file",
    "parse_claude_usage_facet",
    "parse_codex_external_imports_file",
    "parse_codex_goals_db",
    "parse_codex_history_file",
    "parse_codex_legacy_session_file",
    "parse_codex_logs_db",
    "parse_codex_memories_db",
    "parse_codex_session_file",
    "parse_codex_session_index_file",
    "parse_codex_state_db",
    "parse_cursor_ai_tracking_db",
    "parse_cursor_cli_chats_db",
    "parse_cursor_cli_transcript",
    "parse_cursor_prompt_history",
    "parse_cursor_state_db",
    "parse_file_metadata_summary_file",
    "parse_gemini_chat_file",
    "parse_gemini_chat_legacy_file",
    "parse_gemini_logs_file",
    "parse_grok_chat_history",
    "parse_grok_prompt_history",
    "parse_grok_session_search_db",
    "parse_grok_subagents",
    "parse_hooks_summary_file",
    "parse_json_summary_file",
    "parse_opencode_db",
    "parse_pi_context_mode_db",
    "parse_pi_session_file",
    "parse_text_store_file",
    "parse_toml_summary_file",
    "parse_vscode_chat_session",
    "parse_vscode_inline_history",
    "store_descriptor_for_record",
    "store_role_for_record",
)

PARSER_REGISTRY: t.Final[dict[str, AnyParserSpec]] = merge_parser_specs(
    _ANTIGRAVITY_CLI_PARSERS,
    _ANTIGRAVITY_IDE_PARSERS,
    _CLAUDE_PARSERS,
    _CODEX_PARSERS,
    _CURSOR_CLI_PARSERS,
    _CURSOR_IDE_PARSERS,
    _GEMINI_PARSERS,
    _GROK_PARSERS,
    _OPENCODE_PARSERS,
    _PI_PARSERS,
    _VSCODE_PARSERS,
)
"""``adapter_id`` -> parser spec, merged from every per-agent fragment.

:data:`~agentgrep.records.ITER_SOURCE_RECORD_ADAPTERS` mirrors this key set
without importing adapter code; a boundary test asserts the two stay equal.
"""


def iter_source_records(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Dispatch to the adapter parser for one source.

    Looks up ``source.adapter_id`` in :data:`PARSER_REGISTRY`. Stream-aware
    rows receive the planning-visible ``raw_skip_line``/``reverse`` contract;
    plain rows drop it. Unknown adapter ids yield nothing.
    """
    spec = PARSER_REGISTRY.get(source.adapter_id)
    if spec is None:
        return
    if isinstance(spec, StreamParserSpec):
        yield from spec.parser(source, raw_skip_line=raw_skip_line, reverse=reverse)
        return
    yield from spec.parser(source)
