(library-query-language)=

# Query language

{argparse:subcommand}`agentgrep search`, {argparse:subcommand}`agentgrep grep`,
and {argparse:subcommand}`agentgrep find` accept a Lucene-style query language
for inline field predicates, boolean composition, and date ranges. The same
syntax works across all three subcommands; each interprets the predicates
against its natural record shape.

The query language is **opt-in**: a bare positional like
`agentgrep grep bliss` keeps the legacy fast path with zero
overhead. A cheap, dependency-free scan engages the parser only when
a positional carries query syntax — a **known field predicate**
(`agent:`, `model:`, …), a **standalone uppercase boolean keyword**
(`AND` / `OR` / `NOT`), or a **leading quote** (an intended phrase).
Lowercase `and` / `or` and unquoted bare terms stay literal, and a
plain term list never imports the query module. Restricting the field
scan to registered names keeps incidental colons — URLs like
`https://host`, values like `path/to/file` — from spuriously engaging
the parser.

## Grammar

```
query        := disjunction
disjunction  := conjunction ("OR" conjunction)*
conjunction  := negation ("AND"? negation)*
negation     := ("NOT" | "-" | "+")? primary
primary      := group | field-expr | phrase | term
group        := "(" disjunction ")"
field-expr   := IDENT ":" field-value
field-value  := comparison | range | exists | exact-value
comparison   := (">" | "<" | ">=" | "<=") TERM
range        := "[" TERM "TO" TERM "]"        ; inclusive
              | "{" TERM "TO" TERM "}"        ; exclusive
exists       := "*"                           ; field present + non-empty
exact-value  := TERM                          ; may carry * / ? wildcards
phrase       := '"' TEXT '"'                  ; exact adjacent words
term         := TERM
```

A full query exercising most of the grammar:

```agentgrep-query
(agent:codex OR agent:cursor-cli) model:gpt* timestamp:>2026-01-01 NOT deploy
```

Implicit AND between bare terms is preserved: `agentgrep grep foo bar`
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
| `agent` | enum | One of `codex`, `claude`, `cursor-cli`, `cursor-ide`, `gemini`, `antigravity-cli`, `antigravity-ide`, `grok`, `pi`, `opencode` |
| `store` | string | Substring, or `*` / `?` wildcard, against the source's store name |
| `adapter_id` | string | Substring or `*` / `?` wildcard; alias `adapter` |
| `path` | path | Glob (with `*` / `?` / `[…]`, case-sensitive) or substring |
| `mtime` | date | Source-file mtime; supports `>`/`<`/`>=`/`<=` and `[a TO b]` |

### Record-level fields

These need the parsed record, so they filter after the source
predicate has admitted the source.

| Field | Kind | Notes |
|---|---|---|
| `scope` | enum | One of `prompts`, `conversations`, `all` |
| `timestamp` | date | Record timestamp; supports comparison + range; alias `date` |
| `model` | string | Substring, or `*` / `?` wildcard, against `record.model` (conversation records only) |
| `role` | string | Substring or `*` / `?` wildcard against `record.role` (prompt records are always `user`) |
| `text` | string | Substring or `*` / `?` wildcard (against record text); implicit field for bare positional terms |

Unknown field names error at parse time with a clean message listing
the registered fields, so a mistyped predicate (`agnet:codex`) is
caught immediately rather than silently matching nothing.

Every queryable field, alias, and operator is also reflected
programmatically by `agentgrep.query.help` (`query_language_fields`,
`query_language_operators`), which backs the MCP tool descriptions and
the {resource}`agentgrep_query_language` resource — the same vocabulary,
never out of sync.

## Phrases

A double-quoted string matches its words as one contiguous, casefolded
substring with internal whitespace collapsed: `"deploy v1"` matches
`deploy v1` but not `deploy the v1`. Phrases ride the same fast path as
bare terms — no field machinery — and compose with the boolean
operators like any other term:

```agentgrep-query
"streaming parser" OR "stream reader"
```

Because the parser engages on a leading quote, `agentgrep search
'"exact phrase"'` enters phrase mode even with no field predicate
present. (The shell strips the outer single quotes; the inner
double-quoted token reaches agentgrep intact.)

## Field-exists

`field:*` matches records or sources where the field is **present and
non-empty**, regardless of value:

```agentgrep-query
model:* ruff
```

Records that carry any model string and mention `ruff`. Negate for
absence with `NOT field:*` (or `-field:*` inside a larger quoted
query):

```agentgrep-query
NOT model:* deploy
```

Field-exists works on every field kind; it is the readable way to ask
"was this attribute captured at all?".

## Wildcards

String and text fields (`store`, `adapter_id`, `model`, `role`,
`text`) accept `*` and `?` glob wildcards. A wildcard value is matched
as an **anchored, case-insensitive glob** — `model:gpt*` means "starts
with `gpt`", not "contains `gpt`". For a substring match, wrap with
explicit wildcards (`model:*gpt*`) or drop the wildcard entirely
(`model:gpt`, which keeps the historical casefolded substring
behavior). A wildcard on `text` matches the record text only, while a
plain `text:` value keeps its multi-surface substring match.

```agentgrep-query
model:gpt*
```

The `path` field also globs (`*` / `?` / `[…]`), but path globs are
**case-sensitive** and anchored to the whole path. Enum fields
(`agent`, `scope`) and date fields (`mtime`, `timestamp`) do not take
wildcards — enums match by exact membership, dates by literal or
range.

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
$ agentgrep search 'ruff OR uv'
```

Ranked prompts mentioning either `ruff` or `uv`. A bare uppercase
`OR` engages the query language without any field predicate.

```console
$ agentgrep search 'model:gpt* caching'
```

Prompts from any `gpt`-prefixed model that mention `caching`. The
`model:gpt*` wildcard is an anchored, case-insensitive glob.

```console
$ agentgrep search 'model:* ruff'
```

Prompts that recorded any model and mention `ruff` — `model:*` tests
presence, not a value.

```console
$ agentgrep grep agent:codex bliss
```

Records from codex matching "bliss". Claude / cursor / gemini sources
are never opened.

```console
$ agentgrep grep '(agent:codex OR agent:cursor-cli) AND deploy'
```

Records from either codex or cursor mentioning "deploy". Claude /
gemini are pruned at source level.

```console
$ agentgrep grep 'NOT agent:claude' bliss
```

Records from anyone except claude that mention "bliss". The
`-agent:claude` negation shortcut is rejected at parse time (see
"Leading `-` on a field predicate" below) — `NOT` is the readable
form, `--` the surgical one.

```console
$ agentgrep grep 'timestamp:>2026-01-01 bliss'
```

Records after 2026-01-01 mentioning "bliss". The timestamp filter
runs at the record layer.

```console
$ agentgrep grep 'scope:conversations timestamp:[2026-01 TO 2026-03] model:claude bliss'
```

Q1 2026 conversation records from any claude-* model that mention
"bliss". `model:` is conversation-scoped — prompt records carry no
model — so a `scope:conversations` (or `scope:all`) predicate is
required; `grep` still needs a text term to match lines against.

```console
$ agentgrep grep 'scope:conversations pytest'
```

Conversation-scope records mentioning "pytest". A bare search uses
prompt scope; `scope:conversations` is the inline form of
`--scope conversations`.

```console
$ agentgrep find 'path:*codex* agent:codex'
```

Codex-agent sources whose path contains `codex`. `find` takes a
single positional, so quote the whole query as one token; `path:`
matches against the absolute path and accepts current-user `~`
prefixes, so both `path:*codex*` and `path:~/.codex` work.

```console
$ agentgrep grep agent:codex bliss
```

Grep over codex records for "bliss" — same line-aware output as
plain `agentgrep grep bliss`, but with the codex prefilter.

## Flag / field collisions

`agentgrep` rejects ambiguous combinations of CLI flags and inline
field predicates:

```console
$ agentgrep grep --agent codex agent:claude bliss
agentgrep grep: error: cannot combine --agent flag with agent: field predicate; pick one syntax
```

Currently checked: `--agent` × `agent:`, `--scope` × `scope:`. Other
flags don't yet have query-field counterparts.

## Performance

When no positional carries query syntax — no known field predicate, no
standalone `AND` / `OR` / `NOT`, no leading quote — the query module is
never imported and zero work is added; the legacy fast path runs
exactly as before. The gate scan itself is a dependency-free string
check. When the syntax is used:

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
