"""Antigravity IDE registry fragment.

Antigravity IDE has no dedicated parser: its conversation and
implicit ``.pb`` artifacts are encrypted, so the only readable
surfaces are Markdown brain notes and skills, all routed through the
generic text parser.
"""

from __future__ import annotations

from agentgrep.adapters._generic import (
    parse_text_store_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec

_ANTIGRAVITY_IDE_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("antigravity_ide.brain_text.v1", parse_text_store_file),
    ParserSpec("antigravity_ide.brain_resolved_text.v1", parse_text_store_file),
    ParserSpec("antigravity_ide.skills_text.v1", parse_text_store_file),
)
"""Dispatch rows for every ``antigravity_ide.*`` adapter id."""
