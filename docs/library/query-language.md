(library-query-language)=

# Query language

`agentgrep search`, `agentgrep grep`, and `agentgrep find` accept a
Lucene-style query language for inline field predicates, boolean
composition, and date ranges. The same syntax works across all three
subcommands; each interprets the predicates against its natural
record shape.

The query language is **opt-in**: a bare positional like
`agentgrep search bliss` keeps the legacy fast path with zero
overhead. Detection is a single-character scan for `:` in the
positional tokens — if absent, the query module is never loaded.

## Grammar

```
query        := disjunction
disjunction  := conjunction ("OR" conjunction)*
conjunction  := negation ("AND"? negation)*
negation     := ("NOT" | "-" | "+")? primary
primary      := group | field-expr | term
group        := "(" disjunction ")"
field-expr   := IDENT ":" field-value
field-value  := comparison | range | exact-value
comparison   := (">" | "<" | ">=" | "<=") TERM
range        := "[" TERM "TO" TERM "]"        ; inclusive
              | "{" TERM "TO" TERM "}"        ; exclusive
exact-value  := TERM
term         := TERM
```

Implicit AND between bare terms is preserved: `agentgrep search foo bar`
matches records containing both `foo` and `bar`. Explicit `AND` /
`OR` / `NOT` are case-insensitive and must be whole words.

The sigils `-` and `+` are shortcuts for `NOT` and "required"
respectively. `+` is currently a no-op (implicit AND already
requires every term); it's accepted for rg compatibility.

## Field registry

The default registry ships ten fields, split across two evaluation
layers:

### Source-level fields

These can be decided from a `SourceHandle` alone, so source-level
predicates prune sources before any file is opened.

| Field | Kind | Notes |
|---|---|---|
| `agent` | enum | One of `codex`, `claude`, `cursor`, `gemini` |
| `store` | string | Substring against the source's store name |
| `adapter_id` | string | Substring; alias `adapter` |
| `path` | path | Glob (with `*` / `?` / `[…]`) or substring |
| `mtime` | date | Source-file mtime; supports `>`/`<`/`>=`/`<=` and `[a TO b]` |

### Record-level fields

These need the parsed record, so they filter after the source
predicate has admitted the source.

| Field | Kind | Notes |
|---|---|---|
| `type` | enum | One of `prompts`, `history` |
| `timestamp` | date | Record timestamp; supports comparison + range; alias `date` |
| `model` | string | Substring against `record.model` |
| `role` | string | Substring against `record.role` |
| `text` | string | Substring; implicit field for bare positional terms |

Unknown field names error at parse time with a clean message listing
the registered fields.

## Date literals

The `mtime` and `timestamp` fields accept three forms:

- **ISO 8601**: `2026-05-22`, `2026-05`, `2026`,
  `2026-05-22T14:30:00`, `2026-05-22T14:30:00Z`,
  `2026-05-22T14:30:00+02:00`.
- **Relative**: `today`, `yesterday`, `tomorrow`, `Nd`, `Nw`,
  `Nm`, `Ny` (with optional trailing ` ago`), `N(d|w|m|y) from now`.
  Month ≈ 30 days, year ≈ 365 days.
- **Unbounded marker**: the literal `*` inside a range
  (`field:[* TO 2026-05]`).

Bare-day equality expands to a half-open 24-hour range; bare-month
to the calendar month; bare-year to the calendar year. Exact-time
literals (`2026-05-22T14:30:00`) match the precise instant.

## Two-layer filtering

The compiler classifies each predicate into a source-layer pass and
a record-layer pass. Source-layer predicates prune sources before
any file is opened; record-layer predicates filter parsed records
afterward.

For boolean composition:

- **AND of any layers**: source-layer children prune; record-layer
  children filter. Each layer evaluates its own children.
- **OR of same-layer children**: the OR runs cleanly at that layer.
- **OR mixing source-level and record-level**: the source pass uses
  three-valued logic and conservatively lets the source through
  (the record pass decides). One OR-mixed query is the only perf
  cliff in the design.
- **NOT** propagates per layer; a `NOT` over a mixed subtree falls
  back to record-only evaluation, same as OR-mixed.

## Examples

```console
$ agentgrep search agent:codex bliss
```

Records from codex matching "bliss". Claude / cursor / gemini sources
are never opened.

```console
$ agentgrep search '(agent:codex OR agent:cursor) AND deploy'
```

Records from either codex or cursor mentioning "deploy". Claude /
gemini are pruned at source level.

```console
$ agentgrep search '-agent:claude bliss'
```

Records from anyone except claude that mention "bliss".

```console
$ agentgrep search 'timestamp:>2026-01-01 bliss'
```

Records after 2026-01-01 mentioning "bliss". The timestamp filter
runs at the record layer.

```console
$ agentgrep search 'timestamp:[2026-01 TO 2026-03] model:claude'
```

Records in Q1 2026 from any claude-* model.

```console
$ agentgrep find path:~/.codex agent:codex
```

Codex-agent sources under `~/.codex/`.

```console
$ agentgrep grep agent:codex bliss
```

Grep over codex records for "bliss" — same line-aware output as
plain `agentgrep grep bliss`, but with the codex prefilter.

## Flag / field collisions

`agentgrep` rejects ambiguous combinations of CLI flags and inline
field predicates:

```console
$ agentgrep search --agent codex agent:claude bliss
agentgrep search: error: cannot combine --agent flag with agent: field predicate; pick one syntax
```

Currently checked: `--agent` × `agent:`, `--type` × `type:`. Other
flags don't yet have query-field counterparts.

## Performance

When the positionals contain no `:`, the query module is never
imported and zero work is added — the legacy fast path runs exactly
as before. When the syntax is used:

- **Parse + compile** is sub-millisecond for typical queries.
- **Source pruning** is O(predicates) per `SourceHandle`. Pruning
  saves multiple seconds on multi-thousand-file trees when a
  single field rules out most sources.
- **Record filtering** runs in the existing per-record hot loop and
  short-circuits as soon as a child predicate fails. The net effect
  on records that pass is sub-5% overhead; rejected records save
  time vs. the legacy path because no haystack is built.

The one perf cliff is **OR-mixed**: an OR that straddles source-
and record-level predicates can't push down past the source-prune
boundary. The compiler degrades safely (lets the source through;
the record pass decides) — it just costs the file read.

## Known limitations

### Leading `-` on a field predicate

A field predicate that begins with a bare `-` (e.g.
`-agent:claude` as the negation shortcut for `NOT agent:claude`)
collides with argparse's short-option collapse rule. The argv
token `-agent:claude` would otherwise parse as the combined short
options `-a -g -e nt:claude` because each leading character
matches a defined short flag, silently turning the user's intent
into a totally different command.

agentgrep rejects this argv shape at parse time with a clear
error and two workarounds:

```console
$ agentgrep find -agent:claude
agentgrep: error: argument '-agent:claude' looks like a field
predicate but argparse parses the leading '-' as combined short
options. Use one of:
  --                  positional separator: agentgrep ... -- -agent:claude
  keyword negation:   agentgrep ... 'NOT agent:claude'
```

Pick the form that fits your scripting style. The `NOT` keyword
is the most readable; `--` is the most surgical. Note that
shell-level quoting (`'-agent:claude'`) does **not** help — the
shell strips quotes before argparse runs, so the quoted token
arrives at argparse identically to the unquoted form and the
pre-scan rejects both. Use `NOT` or `--`.

### `field:` with no inline value

The query `agent: bliss` parses as a single
`FieldEq(agent, "bliss")` predicate, not as "missing value
followed by separate term `bliss`". The tokenizer emits
`ident("agent"), colon` and the next term token becomes the
value. Defensible (the colon's `:` separator is a contiguous
operator, the space after is just whitespace) but unintuitive
when typing.

If you want the bare term `bliss` plus a separate `agent`
predicate, write `agent:codex bliss` (filled-in value) or
`bliss` (no `agent:` predicate at all).
