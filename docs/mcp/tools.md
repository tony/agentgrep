(mcp-tools)=

# Tools

agentgrep's tools are read-only. They return structured Pydantic models and
protect private paths before serialization. Invalid-parameter responses return
concise field errors without echoing rejected values. Audit logging redacts
sensitive `terms`, `pattern`, `sample_text`, and `cursor` fields.

## Prompt and Conversation Search

```{fastmcp-tool} search
:no-index:
```

**Use when** you need prompt records matching terms, query-language
fields, or project context. The default reads fast prompt-history
stores only. Set `effort="targeted"` to use prompt evidence to select a
bounded set of conversations, or `effort="exhaustive"` to search every
readable conversation. Targeted effort attempts at most 25 distinct
conversations by default; override that work bound with
`conversation_limit`. Pass `scope="conversations"` for full conversation,
assistant, tool, and event records, or `scope="all"` for both surfaces;
those broader scopes imply exhaustive reads when effort is omitted. Pass
top-level `cwd`, `repo`, or `branch` to apply the same origin filters as
{ref}`agentgrep search <cli-search-project-context>`; use `worktree:`,
`project:`, and `cwd_hash:` inside `terms` when you need those
query-language fields. A request with an origin filter and no terms is
valid.

An inline `depth:`/`effort:` term inside `terms` selects the same read
policy as the `effort` parameter — see
{ref}`library-query-language`. Setting both the `effort` parameter and an
inline term in the same request is rejected; pick one.

With targeted or exhaustive effort and omitted scope, the MCP tool infers
`scope="all"`. It does not broaden explicit prompt scope: targeted effort with
`scope="prompts"` is rejected. Targeted routing is proof-bound for Codex,
Claude Code, Grok, and Antigravity CLI; other conversation backends require
exhaustive effort. Its default of 25 conversation attempts is unmeasured
policy, not the result limit. The independent MCP result `limit` still defaults
to 20 when omitted.

**Returns:** request metadata, effort, run status, coverage, diagnostics,
next actions, result-window metadata, and normalized records with `ref`,
`content_id`, `record_id`,
`record_id_stability`, `thread_id`, agent, store, adapter, path, text, title,
role, timestamp, model, session ID, conversation ID, optional
{class}`~agentgrep.mcp.RecordOriginModel`, and metadata. `content_id` is always a
full string; the record, stability, and thread fields are required but nullable
when the source lacks a defensible coordinate or thread. See the
{ref}`deterministic record identity contract
<adr-deterministic-record-identity>` for their separate meanings.

Canonical IDs compare content, logical occurrences, and namespaced threads;
they do not locate stored results. For inspection, only `ref` is accepted by
`inspect_result`. Positionless refs retain their existing bytes. Positioned
search refs keep the same opaque version 1 shape and length but use a
position-aware fingerprint so repeated equal turns resolve exactly. Historical
position-blind refs keep first-match behavior only while every other normalized
fingerprint input still matches. Refs and cursors are snapshot-relative
physical handles and may stop resolving after store or adapter normalization
changes; run a fresh search to obtain a current ref.

Search responses are cursorless. `status.reason="result_limit"` means more
matches may exist; refine the query or rerun it with a higher limit.
`status.state="truncated"` means the
MCP response budget omitted whole trailing records while preserving the
structured envelope only when the zero-record envelope fits. Otherwise the
client receives a bounded MCP error without `structuredContent`. Results use
newest order. Primary status precedence is failed, cancelled, truncated,
approximate, bounded, then complete; independent facts remain in
`status.conditions`. Response truncation leaves a higher-precedence failed or
cancelled state primary and adds its own condition.

Prompt completions offer `search.targeted` and `search.exhaustive` next
actions. Targeted completions report `status.state="approximate"`, carry
eligible/selected/completed conversation counts, and offer the exhaustive
follow-up. These actions contain bounded request patches; clients should not
infer paths or conversation identifiers from record text.
Inspect `status.conditions` alongside the primary status. Before applying a
follow-up patch, honor `requires_confirmation`; an explicit prompt scope needs
confirmation before a patch broadens it.

**Example:**

```json
{
  "tool": "search",
  "arguments": {
    "terms": ["release notes"],
    "agent": "all",
    "scope": "all",
    "effort": "targeted",
    "conversation_limit": 10,
    "cwd": "~/work/django-project",
    "limit": 20
  }
}
```

```{fastmcp-tool-input} search
```

## Portable Record Export

```{fastmcp-tool} export_records
```

**Use when** you already have one to 20 unique `agref1:` search refs and need
their selected records as one portable inline artifact. The format defaults to
`ndjson`. Choose `markdown` for a human-readable artifact. Flat `records` are
the default selection. `thread` requires all refs to resolve to one non-null
canonical observed thread.

Prompt and history bodies are private by default: `include_bodies` defaults to
false, and text appears only with `include_bodies=true`. The result carries one
`TextContent` artifact, at most 400 KiB of UTF-8, plus structured metadata for
schema, format, selection, body policy, record count, and byte count.

The tool accepts search refs only. It has no local destination, query, or
cursor argument and never writes a server-local file. Use {tooliconl}`search`
for discovery and pagination, then pass refs from the desired page. Refs use
the same exact repeated-occurrence and historical compatibility behavior as
{tooliconl}`inspect_result`; duplicate physical selections are refused.
Each ref is limited to 8,192 characters and is rejected before decoding or
source discovery when it exceeds that bound.

The deterministic allowlist and observed-thread fidelity are shared with the
{ref}`CLI export guide <cli-export>`. The tool remains read-only, idempotent,
and closed-world even when bodies are requested.

```{fastmcp-tool-input} export_records
```

## Time-Windowed Activity

```{fastmcp-tool} recent_sessions
```

**Use when** you want the most-recently modified sources for an agent — newest-first, optionally bounded by a time window.

**Returns:** the cutoff timestamp plus source records ordered by ``mtime_ns`` descending.

```{fastmcp-tool-input} recent_sessions
```

## Store Discovery

```{fastmcp-tool} find
```

**Use when** you need to inspect which stores, session files, and databases agentgrep can read.

**Returns:** request metadata, run status, result stats, page metadata, and source records with `ref`, agent, store, adapter, protected path, path kind, and metadata. When `page.next_cursor` is present, pass it back as `cursor` to continue the same discovery scan.

**Example:**

```json
{
  "tool": "find",
  "arguments": {
    "pattern": "sessions",
    "agent": "codex",
    "limit": 50
  }
}
```

```{fastmcp-tool-input} find
```

## Structured Source Listing

```{fastmcp-tool} list_sources
```

**Use when** you want a structured listing of discovered sources with
optional path-kind, source-kind, and coverage filters. By default this
matches the default-search surface; pass `include_non_default=true` or
set `coverage_filter` to inspect inventory-only stores such as Codex
SQLite DBs or Claude session memory. Each returned source includes
`searchable`, coverage-based `search_by_default`, `store_role`,
`required_effort`, `searchable_reason`, `inspectable`, and
`version_detection`. Coverage reports whether normal discovery admits a
source before effort and role filtering; `required_effort` reports whether a
prompt search can read it with prompt effort or needs exhaustive effort.
`version_detection` records the strategy and evidence agentgrep used to
identify the app/data version for that concrete file or DB.

```{fastmcp-tool-input} list_sources
```

## Required-Pattern Filtering

```{fastmcp-tool} filter_sources
```

**Use when** you want to narrow discovered sources by required substring pattern (a stricter ``find``).

```{fastmcp-tool-input} filter_sources
```

## Discovery Counts

```{fastmcp-tool} summarize_discovery
```

**Use when** you want aggregate counts of discovered sources by agent, format, and path-kind.

```{fastmcp-tool-input} summarize_discovery
```

## Catalog

```{fastmcp-tool} list_stores
```

**Use when** you want the canonical catalog of on-disk stores agentgrep knows about — including stores that are not searched by default.

```{fastmcp-tool-input} list_stores
```

```{fastmcp-tool} get_store_descriptor
```

**Use when** you need the full descriptor (role, format, upstream reference, schema notes) for a single store id.

```{fastmcp-tool-input} get_store_descriptor
```

```{fastmcp-tool} inspect_record_sample
```

**Use when** you want a few raw records from one adapter+path to validate parser output or discover schema variations.

```{fastmcp-tool-input} inspect_record_sample
```

```{fastmcp-tool} inspect_result
```

**Use when** you have a `ref` returned by `search` or `find` and need to inspect the matching result or sample records from that source without reconstructing local paths.

```{fastmcp-tool-input} inspect_result
```

## Diagnostics

```{fastmcp-tool} validate_query
```

**Use when** you want to dry-run a literal pattern against sample text before issuing a broad cross-agent search.

```{fastmcp-tool-input} validate_query
```
