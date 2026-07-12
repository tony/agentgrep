(cli-bookmark)=

# agentgrep bookmark

`agentgrep bookmark` saves a small, durable pointer to something you found
without copying its prompt or conversation into another database. The pointer
is a complete canonical ID from {ref}`ADR 0015
<adr-deterministic-record-identity>`:

| Scope | Target | Meaning |
| --- | --- | --- |
| `content` | `agc1:` | Equal normalized role, kind, and text |
| `record` | `agr1:`; `bookmark add` also needs `--content-id` | One logical stored occurrence, checked against its `agc1:` content |
| `thread` | `agt1:` | One backend thread with a defensible native anchor |

There are no short IDs or prefix lookups. Copy the complete canonical ID from a
search result. `--content-id` is required for `bookmark add` when its target is
an `agr1:` record ID; the complete content ID prevents a stale or mismatched
record bookmark from opening different content. `bookmark remove` does not
accept that option; removal needs only the complete target ID.

## Add, list, and remove

Save a content bookmark:

```console
$ agentgrep bookmark add agc1:2vlm1978v1np5kg5fkqv539kic
```

Save an exact record bookmark with its content validator:

```console
$ agentgrep bookmark add agr1:uuqn9q331f1fcgsr5gr8agefhs --content-id agc1:2vlm1978v1np5kg5fkqv539kic
```

Save a thread bookmark:

```console
$ agentgrep bookmark add agt1:bkd9k19ok4vvbsf73jornija04
```

List saved bookmarks in creation order:

```console
$ agentgrep bookmark list
```

Emit the same list as deterministic JSON:

```console
$ agentgrep bookmark list --json
```

Remove a bookmark by its complete target ID:

```console
$ agentgrep bookmark remove agc1:2vlm1978v1np5kg5fkqv539kic
```

Add and remove are idempotent. Human output reports `added`, `removed`, or
`unchanged`; `--json` exposes the same action for scripts. Re-adding an existing
target and removing an absent target both succeed as `unchanged`.

For a record target, re-add returns `unchanged` only when `--content-id` matches
the same saved content validator. A different valid `agc1:` validator is a
validation failure and exits `1`; it is not treated as unchanged.

The default capacity is 200 bookmarks. At capacity, a matching re-add still
returns `unchanged`, removal still works, and a new target is refused without
changing the saved list. Successful operations exit `0`; storage, validation,
capacity, and corruption failures exit `1`. Argument errors are reported by the
parser before the command runs.

(cli-bookmark-storage)=

## Private local state

Bookmarks live in agentgrep-owned state under the XDG data directory. When
`XDG_DATA_HOME` is set, the snapshot is
`$XDG_DATA_HOME/agentgrep/bookmarks.json`; otherwise agentgrep uses the XDG
default. The snapshot stores canonical IDs, scope, creation time, and the
content validator required for a record bookmark. It does not store prompt
text, titles, source paths, working directories, or repository paths.

Canonical IDs are pseudonymous equality handles, not secrets or anonymization.
The saved IDs and creation times can still reveal activity, so agentgrep keeps
the application directory and files private. If the snapshot is malformed,
uses an unknown schema, has duplicate targets, or exceeds capacity, agentgrep
reports a path-free corruption error and refuses it as a whole rather than
salvaging or overwriting it.

(cli-bookmark-resolution)=

## Recall in the TUI

The CLI manages canonical targets; it does not scan history to resolve them.
Use the HUD's {ref}`bookmark recall <tui-bookmarks>` to compare saved targets
with the current stores and open an available record. A missing target remains
saved, so it can resolve again if its store becomes available later.

Bookmark writes affect only agentgrep-owned state. Discovery and recall keep
Codex, Claude Code, Cursor, and every other source store read-only.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: bookmark
    :nodescription:
```
