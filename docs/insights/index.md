(insights)=

# Insights

Insights compare the normalized records already present in the
DB index. They are deterministic local analysis steps for
finding similar prompts, duplicated instruction families, variants,
and meaningful omissions.

The agentgrep database is required because insight runs need stable
record ids, normalized text hashes, metadata, and persisted evidence.
Run a DB sync before running insights:

```console
$ agentgrep db sync
```

`agentgrep db sync` defers expensive feature rows by default so the
cache refresh path stays fast. Insight runs refresh any missing
deterministic features before they compare records, so the default sync
mode is sufficient for later similarity and omission analysis.

Analyze similarity and omission evidence:

```console
$ agentgrep insights analyze
```

List a bounded human summary of stored evidence:

```console
$ agentgrep insights list --limit 10
```

Emit the same evidence page as JSON:

```console
$ agentgrep insights list --limit 10 --json
```

Get cheap persisted-insight counts without returning evidence rows:

```console
$ agentgrep insights explain --json
```

## Similarity

Similarity insights group records that share deterministic signals:
exact normalized text, lexical overlap, and metadata that makes the
relationship meaningful. The current implementation persists variant
edges so later tools can inspect why two records were considered
related.

Use similarity mode when you want to find repeated prompts,
copy-pasted instruction fragments, or near-equivalent prompt families
across agent stores:

```console
$ agentgrep insights analyze --kind similarity
```

## Omissions

Omission insights compare indexed instructions against a target
surface, such as `AGENTS.md`. A finding means the indexed DB
contains a recurring instruction-like record that is absent from the
target text.

Run omission detection for one target:

```console
$ agentgrep insights analyze \
    --kind omissions \
    --target AGENTS.md
```

Omission findings are evidence, not edits. They can later feed
review-only suggestions; see {ref}`insights-suggestions`.

## LLM boundary

Normal search and insight commands do not silently call an LLM. An LLM
can call agentgrep only when a user or client gives it access to the
CLI or MCP server and it chooses, or is instructed, to use that tool.

Future LLM-assisted judgement should run as an explicit command or MCP
tool over a small evidence pack. The output should be a persisted
suggestion artifact with provenance, confidence, and review state.

```{toctree}
:hidden:

suggestions
```
