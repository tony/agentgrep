"""ChatGPT desktop adapter: metadata summaries of stores agentgrep cannot read.

Every ChatGPT desktop row is catalog-only, so search never opens these files.
Inventory surfaces (``list_sources`` with ``include_non_default``, and
``inspect_record_sample``) describe them by size and modification time only.
"""

from __future__ import annotations

import functools

from agentgrep.adapters._generic import parse_file_metadata_summary_file
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec

_CHATGPT_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec(
        "chatgpt.file_metadata_summary.v1",
        functools.partial(parse_file_metadata_summary_file, label="ChatGPT desktop (unreadable)"),
    ),
)
