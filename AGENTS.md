# AGENTS.md — agentex

## Shipped vs. Branch-Internal Narrative

Long-running branches accumulate tactical decisions — renames,
refactors, attempts-then-reverts, intermediate states. Commit messages
and the diff hold *what changed* and *why*. Do not restate either in
artifacts the downstream reader holds: code, docstrings, README,
CHANGES, PR descriptions, release notes, migration guides.

When deciding what counts as branch-internal, use trunk or the parent
branch as the baseline — not intermediate states inside the current
branch.

**The Published-Release Test**

Before adding rename history, "previously" / "formerly" / "no longer
X" phrasing, "removed" / "moved" / "refactored" / "fixed" diff
paraphrases, or `### Fixes` entries to a user-facing surface, ask:

> Did users of the most recently published release ever experience
> this old name, old behavior, or bug?

If the answer is no, it is branch-internal narrative. Move it to the
commit message and describe only the current state in the artifact.

**Keep in shipped artifacts**

- Deprecations and migration guides for symbols that actually shipped.
- `### Fixes` entries for bugs that affected users of a published
  release.
- Comments explaining *why the current code looks this way* —
  invariants, platform quirks, upstream bug workarounds — that make
  sense to a reader who never saw the previous version.

**Default**: when in doubt, keep the artifact clean and put the story
in the commit.

### Cleanup in Hindsight

When applying this rule retroactively from inside a feature branch,
first establish scope by diffing against the parent branch (or trunk)
to identify which commits this branch actually introduced. Then:

- **Commits introduced in this branch** — prompt the user with two
  options: `fixup!` commits with `git rebase --autosquash` to address
  each causal commit at its source, or a single cleanup commit at
  branch tip. User chooses.
- **Commits already in trunk or a parent branch** — default to
  leaving them alone. Do not raise them as cleanup candidates; act
  only on explicit user instruction. If the user opts in, fold the
  cleanup into a single commit at branch tip and do not rewrite trunk
  or parent-branch history.
- **Scope guard** — if cleaning in-branch bleed would touch a
  colleague's in-flight work or expand the branch beyond its stated
  goal, default to staying in lane: protect the project's current
  goal, leave prior bleed alone, and don't introduce new bleed in the
  current change.
