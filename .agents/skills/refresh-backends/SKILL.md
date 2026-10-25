---
name: refresh-backends
description: >-
  Assess and update agentgrep's backends and storage catalogue against the
  agent clients installed on this machine (claude, codex, cursor-agent,
  gemini, agy, grok, pi, opencode, VS Code, Cursor IDE, Antigravity IDE,
  Windsurf), versioning what each client version stores. Use when asked to
  sweep, refresh, audit, or update the backends or storages, check for drift
  after a client upgrade, observe a new client version, or record which
  version a storage change landed in.
argument-hint: "[agent ...]"
---

# Refreshing agent backends

Each installed client version gets a schema-only manifest in
`docs/_observations/<agent>/<version>.toml`, and the catalogue stamps in
`src/agentgrep/store_catalog/` must match the newest one. A refresh records
new versions, catalogues what changed, ties each change to the version that
first showed it, and proves search still works and is no slower.

## Arguments

`$ARGUMENTS` lists agent ids as `uv run scripts/observe_stores.py --help`
prints them; `--agent` takes one id, so run each command once per id. Empty
means `--agent all`. "In scope" below means these agents.

## Rules

- Agent homes, `/mnt/c` included, are read-only: read files and run
  `--version`, never another client subcommand. No `HOME=` overrides.
- No prompt text, PII, or real home paths anywhere: manifests use `${HOME}`
  tokens, docs pages keep their existing `~/` form.
- Never hand-edit a manifest, and never rewrite or delete one for a version
  that is no longer installed. One that already leaks a path is the user's
  call to scrub: report it.
- Never push, tag, or open a PR. No backup branches.
- If the user asked for `CHANGES`, its entries land last, in their own
  `docs(CHANGES) <description>` commit under the existing unreleased
  heading, with no version lead paragraph. Otherwise leave `CHANGES` alone
  and list the entries it needs in the report.

## Commits

Commit in this order, skipping a kind with nothing in it. Subjects stay at
50 characters or fewer, and bodies take the `why:`/`what:` form from
`.github/WRITING.md`.

1. `agentgrep(fix[observe]): ...` when the observer itself misbehaves,
   with a test in `tests/test_storage_observations.py`.
2. `agentgrep(chore[catalog]): Refresh observed stamps`: every new
   manifest, restamp, and version line. The body names each agent and
   version recorded, and says earlier manifests stay untouched.
3. `agentgrep(feat[catalog]): Catalogue <n> new stores`: new rows, the
   `catalog_version` bump, and the touched agents re-observed at their
   unchanged versions.
4. `agentgrep(feat[<agent>]): ...` or `agentgrep(fix[<agent>]): ...`:
   adapter changes with their fixture and test.
5. `docs(backends): Correct claims the sweep disproved`.
6. `docs(backends): Record storage changes by version`.
7. `docs(backends): Observe <Agent> <version>` for a client that updates
   mid-sweep: its manifest, restamp, version lines, and entry in one commit.
8. `docs(CHANGES) <description>`, when the user asked for it.

## Steps

1. **Worktree.** Unless the user names a branch, branch from fresh
   `origin/master` into a sibling worktree, with the session date as
   `YYYYMMDD`. Work only there.

   ```console
   $ git fetch origin
   ```

   ```console
   $ git worktree add --no-track -b storages-YYYYMMDD ../agentgrep-storages-YYYYMMDD origin/master
   ```

2. **Baseline.** Exit 1 means drift. Table each in-scope agent's documented
   version (newest manifest; `_<AGENT>_OBSERVED_VERSION` in
   `src/agentgrep/store_catalog/<agent>.py`, hyphens as underscores) against
   the installed version this prints.

   ```console
   $ uv run scripts/observe_stores.py check --agent <id>
   ```

   `cursor-ide`, `antigravity-ide`, and `windsurf` have no version probe, so
   their manifests stay `unknown.toml` and `check` never reports an upgrade.
   Read the newest `~/.<ide>-server/bin/<commit>/product.json` by hand
   (`version` for Cursor and Antigravity, `windsurfVersion` for Windsurf)
   and compare it with the stamp.

3. **Observe** each in-scope agent whose version changed or that `check`
   reports drifting. A new version writes a new manifest; drift at the same
   version rewrites that version's file. A refusal to write `unknown.toml`
   over a versioned manifest means the CLI's `--version` failed: report it,
   never work around it.

   ```console
   $ uv run scripts/observe_stores.py observe --agent <id>
   ```

   A new version diffs against the previous manifest:

   ```console
   $ git diff --no-index docs/_observations/<id>/<old>.toml docs/_observations/<id>/<new>.toml
   ```

   A same-version rewrite diffs against the commit:

   ```console
   $ git diff -- docs/_observations/<id>/<version>.toml
   ```

4. **Triage** every diff line:

   - Before calling a key added or removed, check older manifests. The
     observer samples each store's newest 40 files, so keys drift in and out
     of the sample without a schema change.
   - Version changed, shape didn't: restamp only.
   - New unclaimed path: add a `catalog_only` row, or `private` when it
     holds credentials or keys. A new home directory for the agent also goes
     in its `AgentProbe.homes` in `scripts/observe_stores.py`.
   - Changed discriminator, keys, tables, or columns in a store agentgrep
     reads: update the descriptor and adapter, add a redacted fixture under
     `tests/samples/<agent>/<store_id>/`, and show the new test failing
     before it passes.
   - New readable conversation store: give it the `primary_chat` or
     `supplementary_chat` role, never `prompt_history`, so default search
     does not open it.
   - A file newer than its newest record is an empty draft session, not a
     parse failure.
   - Re-read the agent's backend page and each touched row's
     `schema_notes`, and correct any claim the observation disproves.

   Any descriptor add, removal, or reshape follows "Adding or updating a
   store" in `docs/dev/storage-catalog.md`, including its `upstream_ref`
   (pinned to an upstream commit, `github.com/<org>/<repo>@<sha>/...`) or
   `sample_record`. Bump `catalog_version` and re-observe the agent in that
   same commit. If the unclaimed list now names the children of a directory,
   the new row claims that directory by name without covering its children:
   put a `<placeholder>` or glob at that level of its pattern.

5. **Restamp** only what you re-read:

   - `_<AGENT>_OBSERVED_VERSION`, keeping its prefix (`claude-code v...`).
   - `_<AGENT>_OBSERVED_AT`, `_CATALOG_CAPTURED_AT`, and the date in the
     comment above the dated block, in
     `src/agentgrep/store_catalog/_common.py`.
   - The `observed_version` line wherever `docs/backends/<agent>.md` and
     `docs/dev/storage-catalog.md` carry one. Windsurf's page is
     `docs/backends/unsupported/windsurf.md`.

   An agent that is not installed, or whose version you could not read,
   keeps its version and date: move its `_OBSERVED_AT` out of the dated
   block with a comment saying why, as Windsurf's is. A row with its own
   `observed_at=` date instead of the module constant is stale: this lists
   such rows and prints nothing when there are none. Re-verify each on disk
   and point it at the constant.

   ```console
   $ grep -n 'observed_at=' src/agentgrep/store_catalog/*.py | grep -v '_OBSERVED_AT'
   ```

6. **Version the change** under `## Changes by version` on the agent's page,
   adding that section, with the opening paragraph other pages use, where it
   is missing. Each `### <new>` entry opens "Seen in <new> (YYYY-MM-DD);
   absent in <old> (YYYY-MM-DD)." Versions between two observations were
   never seen, so a change landed after `<old>` and by `<new>`; never name an
   exact version. With no readable version, the entry is `### Observed
   YYYY-MM-DD`, bracketed "seen <date>, absent <date>", naming any build
   version you could read. A version with no storage change still gets an
   entry saying so. A store agentgrep does not read is "not searched", never
   "excluded".

7. **Prove search** for every touched agent id except `windsurf`, which has
   no search surface; check it through its docs page. Pick `<term>` from a
   store you changed, and keep it out of commits and the report.

   ```console
   $ uv run agentgrep find --agent <id> --json
   ```

   ```console
   $ uv run agentgrep search --agent <id> --scope all --exhaustive --json <term>
   ```

   Pass means records returned, `summary.coverage.sources_failed` is 0, and
   `summary.diagnostics` has nothing new. For a field you started reading
   (model, cwd, title), returned records must carry real values; a key in
   the output proves nothing. Then run the Layer 0 MCP smoke from the
   `testing-mcp-with-cli-agents` skill: a throwaway FastMCP stdio client in
   the scratchpad doing `tools/list` plus one `search`.

8. **Gate, then commit.** Before every commit: `rm -rf docs/_build`, the
   gates in `.github/CONTRIBUTING.md`, `just test-all`, and
   `just build-docs`. When `FORCE_COLOR` is set, run tests under
   `NO_COLOR=1`. Read each command's own pass/fail line, never a `| tail` or
   `| grep` of it. `just build-docs` lines starting `storage observations:`
   mean a stamp and its newest manifest disagree. Then stage and scan the
   added lines; it must print nothing. It catches encoded project
   directories such as `home-<user>-work-...` and `%2Fhome%2F...`.

   ```console
   $ git diff --cached | grep -nE "^\+.*(/home/|/mnt/|/Users/|home-$USER-|%2F(home|Users)%2F)"
   ```

   Before the last commit, `check` exits 0 for every in-scope agent; a
   client that updated mid-sweep gets commit 7.

9. **Measure** after the last commit, with every configured bench; the
   `benchmark` skill covers flags and `analyze`. Default `search` must not
   slow beyond run-to-run noise; explain any remaining delta with
   `scripts/profile_engine.py` evidence. A fix goes back through step 8.

   ```console
   $ uv run scripts/benchmark.py compare origin/master HEAD --format json --output <scratch>/bench.json --no-progress
   ```

## Report

End with exactly these parts:

- A table: agent | old → new version | what changed | searchable (record
  count) | perf delta.
- Each store left documented but unread, with the reason.
- `CHANGES` entries the user-visible changes need, unless commit 8 landed
  them.
- Anything left undone, with the reason.
