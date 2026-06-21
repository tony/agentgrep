(adr-unsupported-obfuscated-backends)=

# ADR 0008: Unsupported backends with obfuscated storage

## Status

Accepted.

## Context

agentgrep discovers and parses local stores written by AI coding agents.
Some agents persist their conversations in a form agentgrep cannot read:
the bytes are encrypted or use a proprietary, undocumented encoding with
no recoverable schema.

Windsurf (Codeium Cascade) is the concrete example. Its per-session
transcripts live at `~/.codeium/windsurf/cascade/<uuid>.pb` (and the
sibling `implicit/`, `chat_state/`, `memories/` `.pb` files). The
observed payloads are multi-megabyte, high-entropy, not gzip/zlib, and
yield no extractable UTF-8 runs — they are encrypted. A reverse-engineered
`.proto` would not help without Codeium's key, so the content is not
recoverable from the files alone.

The catalogue still benefits from recording *where* such data lives so a
storage audit is complete, but agentgrep must not imply it can search
content it cannot read.

## Decision

A backend whose conversation content is obfuscated or encrypted is
**documented but unsupported**:

1. Its stores are catalogued as `catalog_only` `StoreDescriptor` rows —
   path pattern, role, format, and a `schema_notes` string that states
   the content is unreadable and why. They carry no `discovery` spec and
   no adapter, so nothing claims to parse them.
2. The agent stays in `AgentName` (so the catalogue rows are well-typed
   and appear in `list_stores` and the storage catalogue) but is excluded
   from `AGENT_CHOICES`, the query `agent:` enum, and every search/find
   surface. `--agent <name>` and `agent:<name>` are rejected.
3. User-facing coverage statements (CLI help, MCP instructions, docs lead
   paragraphs) do not list the agent as covered. The MCP instructions
   name it explicitly as catalogued-but-unsupported.
4. Documentation lives under a dedicated "Unsupported backends" section
   (`docs/backends/unsupported/`) that explains the obfuscation and the
   documentary-only treatment.

Readable sidecar artifacts (e.g. Markdown plans or rules) that happen to
live alongside the encrypted transcripts are catalogued by location for
inventory completeness but are also `catalog_only` while the agent as a
whole is unsupported, so coverage is described consistently.

## Consequences

The catalogue and `list_stores` remain complete — a storage audit sees
the encrypted stores and learns they exist — without agentgrep ever
returning empty or misleading "results" for them. If an agent's format is
later opened up (a published schema, or unencrypted data), promoting it to
a supported backend is additive: add a `discovery` spec, an adapter, and
the agent to `AGENT_CHOICES` and the search surfaces.

This ADR governs classification only. It does not authorise decrypting or
working around an agent's protection; agentgrep reads what the user can
already read on disk.
