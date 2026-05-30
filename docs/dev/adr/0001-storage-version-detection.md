(adr-storage-version-detection)=

# ADR 0001: Storage version detection

## Status

Accepted.

## Context

agentgrep reads local stores written by several independently released
AI coding assistants. Those tools can change file names, record keys,
SQLite migration suffixes, and embedded metadata over time. Users can
also keep old unmigrated data after upgrading an app, so a current
client version is not enough to identify the data shape of every file
on disk.

Codex is the concrete example that forced this decision:
`history.jsonl` uses `session_id`, `ts`, and `text`, while older
`history.json` files use `command` and `timestamp`. Both can exist in a
modern config directory. The same applies to session transcripts:
current installs write dated `sessions/**/*.jsonl` rollout streams,
while older unmigrated data can remain as root-level
`sessions/rollout-*.json` objects with `session` and `items` keys.

## Decision

agentgrep detects source versions from concrete data evidence first:

1. Embedded metadata in the source, such as Codex
   `session_meta.payload.cli_version` or Claude transcript `version`.
2. Shape inference from file names, record keys, or SQLite suffixes.
3. Local version-check files that do not require spawning the upstream
   CLI, such as Codex `models_cache.json.client_version`.
4. Catalog observation metadata as a low-confidence fallback.

For opt-in inventory stores that are useful for storage coverage but
unsafe or noisy as raw text, shape inference may use structural
summaries: top-level JSON/TOML keys, hook event names, plugin manifest
keys, file suffixes, byte sizes, or line counts. These summaries prove
which storage shape was observed without adding raw logs, shell
snapshots, hook commands, config values, or cache payloads to the
search corpus.

Normal discovery and search must not run `codex`, `claude`, or another
agent CLI just to learn a version. CLI subprocess probes are slow,
side-effect-prone, and can fail for reasons unrelated to local storage.

The public discovery payload exposes a `version_detection` object with
the detected app version, detected data version, strategy, confidence,
and short evidence string. Search result records do not include this
object; source metadata explains how a file was interpreted, while
record payloads stay focused on prompt/history content.

## Consequences

Adapter authors should parse by concrete source shape. When app version
and data shape disagree, the data shape wins. For example, a Codex
config root with current model metadata can still contain legacy
`history.json` or root-level rollout JSON, and agentgrep should parse
each file by that legacy shape.

Shape inference should stay narrow and explicit. It is appropriate for
top-level JSON keys (`session` plus `items`, `id` plus `thread_name`),
SQLite migration suffixes (`state_5.sqlite`, `logs_2.sqlite`), and
known table/column surfaces used by a parser. It should not infer
versions from prompt text, raw settings values, or arbitrary nested
application state.

Evidence strings must identify keys, table names, or filename suffixes
only. They must not include prompt text, raw config values, tokens, or
local absolute paths.

Private stores, including auth files, credentials, security state,
session environment, secrets, and `.env` files, remain catalog
documentation only. Runtime discovery must not enumerate those paths
from disk, so they expose no `version_detection` payload until a future
explicitly safe private-store inventory policy exists.

The store catalogue declares the strategies each descriptor supports,
but runtime discovery records the strategy actually used for each
source. Backends without concrete detectors can still expose the
catalog observation fallback until they gain shape-specific support.
