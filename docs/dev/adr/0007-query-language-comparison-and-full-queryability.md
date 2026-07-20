(adr-query-language-comparison-and-full-queryability)=

# ADR 0007: Query language comparison and full queryability

## Status

Accepted.

## Context

agentgrep exposes a Lucene-inspired query language: field predicates
(`agent:codex`), boolean composition (`AND` / `OR` / `NOT`, `+` / `-`),
grouping, date comparisons (`timestamp:>2026-01-01`), and ranges
(`timestamp:[a TO b]`). ADR 0006 makes this language a public, discoverable
surface. This ADR records what that language is, what it deliberately is not,
and how it is completed.

The language is frequently mistaken for a full-text engine. It is not.
agentgrep matches by **substring containment** (casefolded), **regex**
(the `grep` verb), and **rapidfuzz** relevance ranking (the `search` verb).
At this ADR's adoption, the search path had no inverted index, tokenizer,
postings list, or BM25 scoring. Later storage and read-model decisions may add
derived exact indexes without changing the language's matching contract. The
semantic source of truth remains the pure-Python compiler in
`agentgrep.query.compile`.

For comparison we studied Tantivy, a Rust full-text search engine, and its
Python bindings:

- Tantivy [0.26.1](https://github.com/quickwit-oss/tantivy/tree/0.26.1)
  (`QueryParser` and the `Query` trait implementations).
- tantivy-py [0.26.0](https://github.com/quickwit-oss/tantivy-py/tree/0.26.0)
  (the `Query.*` constructors and `Index.parse_query` exposed to Python).

Tantivy tokenizes text into an inverted index and scores matches with BM25.
Its `QueryParser` accepts terms (default **OR**), phrases with slop and
prefix, set membership, boost, configurable fuzzy fields, optional regex,
field-exists, and typed ranges over dates and IP addresses — each lowering to
an indexed `Query`. Adopting those semantics through Tantivy or another native
index would require an ADR 0003 engine or worker decision, not merely a parser
change. A Python-orchestrated SQLite or FTS5 read model is a separate storage
and planning decision and does not by itself adopt Tantivy's query semantics.

Mapping the agentgrep query language against Tantivy's `QueryParser`:

| Capability | Tantivy `QueryParser` | agentgrep |
| --- | --- | --- |
| Bare terms | Yes (default OR) | Yes (default **AND**) |
| `field:value` | Yes | Yes |
| `AND` / `OR` / `NOT`, `+` / `-` | Yes | Yes |
| Grouping `( )` | Yes | Yes |
| Ranges `[a TO b]` / `{a TO b}` | Yes | Yes (date fields) |
| Comparison `< > <= >=` | Yes | Yes (date fields) |
| Phrase `"..."` | Yes | Added by this ADR (substring) |
| Field-exists `field:*` | Yes | Added by this ADR |
| Wildcards `*` / `?` | Partial | Added by this ADR (field values) |
| Phrase slop / prefix `"..."~N` / `"..."*` | Yes | No (non-goal) |
| Set `IN [...]` | Yes | No (non-goal) |
| Boost `^N` | Yes | No (non-goal) |
| Regex field predicates | Yes (opt-in) | No (`grep` is regex-native) |
| Dismax / more-like-this / const-score | API | No (non-goal) |
| Term matching model | Tokenized + BM25 | Substring / regex / rapidfuzz |

Two gaps motivated this ADR. First, the overlapping features are usable but
incomplete: phrases, field-exists, and wildcards have no expression even
though the substring model can support them. Second, and more damaging,
boolean composition only engaged when a positional contained a colon. A query
such as `search "ruff OR uv"` was split into the literal terms `ruff`, `OR`,
and `uv` and AND-matched, returning zero results instead of a union. The
operators existed in the grammar but were unreachable from the most natural
input.

## Decision

agentgrep completes the subset of query features that its substring/regex
matching model can express, and makes boolean composition reachable from bare
input. It does not pursue Tantivy parity.

### Boolean composition without a field predicate

Boolean operators, grouping, and quoted phrases engage the parser whether or
not a field predicate is present. A cheap, dependency-free heuristic decides
whether input carries query syntax (a field colon, an uppercase boolean
keyword, a parenthesis, or a quote) so that plain bare-term queries keep the
legacy fast path and the cold-start budget in ADR 0006.

### Phrase queries

A quoted value (`"deploy v1"`) is a phrase: its internal whitespace is
collapsed and it matches as a single casefolded substring of the record text.
A phrase is a text term for every downstream purpose — prefilter, relevance
ranking, and matching — so it carries no scoring semantics beyond ordered
adjacency within the substring.

### Field-exists

`field:*` matches records or sources where the field has a non-empty value.
An empty string counts as absent. Negation uses the existing `NOT` / `-`
forms (`-model:*`).

### Wildcards on text and string fields

A field value containing `*` or `?` matches by anchored, casefolded glob
(`fnmatch`) rather than substring. Wildcards apply to field values only, never
to bare terms, and never to enum or date fields. The path field keeps its
existing case-sensitive glob.

## Scope

This ADR governs the agentgrep query language as compiled by
`agentgrep.query`. It applies to every surface that consumes a compiled query:
the `search`, `grep`, and `find` CLI verbs, the Textual UI search box, and the
MCP search and validation tools. It does not change the execution engine
(ADR 0004) or introduce native code (ADR 0003).

## Requirements

### Matching semantics

- Bare terms match as casefolded substrings and combine with implicit `AND`.
- Phrases match as a single casefolded substring with collapsed internal
  whitespace.
- Field-exists is true when the field value is non-empty; for source-prunable
  fields it resolves at the source layer, for record fields it resolves after
  parsing.
- Wildcard field values match by anchored casefolded glob; users who want
  substring semantics write `*value*`.

### Discoverability

- The query language must be discoverable from CLI help examples, the MCP
  server instructions, MCP tool and parameter descriptions, and a
  machine-readable MCP resource, consistent with the registry-backed discovery
  direction in ADR 0006.
- Field and operator descriptions derive from the field registry so the
  surfaces cannot drift from the compiler.

### Tests

- Parser, compiler, and engine behavior are covered by fast, pure tests with
  no subprocess or home-directory access, following the existing parametrized
  case-table pattern.
- A guard test proves that plain bare-term queries never import the query
  module, protecting cold start.

## Consequences

### Positive

- Boolean queries, phrases, field-exists, and wildcards are reachable from the
  input users actually type.
- One registry feeds the compiler, CLI help, and MCP hints, so discovery and
  behavior stay aligned.
- The substring model stays the single semantic source of truth; no index or
  native dependency is introduced.

### Behavior changes

Input that previously searched for the literal words `OR`, `AND`, or `NOT`, or
for literal parentheses or quotes, now parses as a query. For example
`search OR` raises a parse error rather than searching for the substring `OR`.
The escape valves are lowercase keywords (`or`, `and`, `not` remain literal
terms) and quoting (`"OR"` searches for the literal text). Users of a published
release experienced the old colon-gated behavior, so this is a documented
behavior change rather than branch-internal narrative.

### Tradeoffs and asymmetries

- Wildcards apply to field values only, never to bare terms. A bare `c*x`
  stays a literal substring.
- The path field keeps case-sensitive glob (filesystem semantics) while
  text and string field wildcards are casefolded (text semantics). This
  asymmetry is intentional.
- `field:*` is field-exists, but `field:**` is a wildcard. The distinction is
  the exact value `*`.

### Risks

- Heuristic over-engagement: a positional that looks like a field predicate
  engages the parser. The heuristic matches only registry-shaped identifiers,
  and unknown fields raise a clean parse error rather than searching silently.
- Generated-description drift: registry-backed help can still drift if
  generation is partial. Drift-guard tests compare the rendered field list
  against the registry.

## Relationship to other ADRs

ADR 0003 owns agentgrep-added native boundaries. A Python-orchestrated read
model using the standard-library `sqlite3` module and SQLite FTS5 adds no new
agentgrep-owned native boundary; its storage, lifecycle, provider and planning
contracts belong to their focused decisions. An alternate provider that adds
an in-process native extension, native engine, long-lived native thread or
worker process must be classified and governed by ADR 0003. This ADR declines
BM25 and index-specific query semantics regardless of provider. ADR 0004 owns
planning, execution, and result payloads, which are unchanged. ADR 0006 owns
the public CLI and MCP surface and calls for registry-backed query discovery;
this ADR supplies the query-language content those surfaces expose.

## Final position

agentgrep's query language is a deliberately bounded, substring-based subset of
Lucene shapes, not a Tantivy-class index. This ADR completes the reachable
parts of that subset, makes boolean composition work from bare input, and keeps
the registry as the one source of both behavior and discovery.
