(adr-prompt-guided-conversation-routing)=

# ADR 0021: Prompt-guided conversation routing

## Status

Accepted. Targeted routing is implemented.

## Context

Targeted search needs to reach useful conversations without treating every
prompt hit as a final result or falling back to an exhaustive transcript walk.
The router therefore needs a private, deterministic work-selection contract
with bounded locator traversal and clear failure behavior.

## Decision

Targeted routing first reads dedicated prompt-history sources. Positive text
clues, or positive conversation-invariant metadata, may select candidates.
Negated terms never establish routing evidence. Invariant agent and project
predicates run before grouping and before the request bound.

Candidates are grouped by adapter-proven native conversation identity and
ordered deterministically by newest prompt evidence. The router selects at
most `conversation_limit` grouped candidates, then resolves only exact
provider-owned conversation locators. The default is 25 attempts. It is an
unmeasured routing policy, independent of the result `limit`; no result-limit
migration to 25 occurred.

An ambiguous, stale, unreadable, unresolved, or locator-budget-exhausted
selected candidate consumes its attempt. The router never backfills from a
later candidate. Prompt-evidence read failures fail closed. Locator traversal
is request-bounded, and a supported provider may share one bounded walk among
its selected candidates.

The router opens only uniquely proof-resolved conversation sources. Candidate
identities, locators, and paths remain private engine state: they are not
records, cursors, continuation tokens, or durable index contracts. Routing
evidence never establishes a result match; selected conversations go through
the ordinary matcher, ordering, dedupe, and limit stages.

## Consequences

Targeted results can be cheaper than exhaustive search while retaining a clear
coverage boundary. Their summaries report eligible, selected, and completed
conversations plus privacy-safe routing diagnostics. Because candidate
selection can omit a conversation, targeted completion is approximate even
when every selected conversation completes.

{ref}`ADR 0020 <adr-progressive-deep-search>` owns when targeted effort is
selected. {ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>`
owns the shared planner, driver, matcher, collector, lifecycle, and coverage
boundaries.
