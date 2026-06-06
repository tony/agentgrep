(adr-suggestion-skills-agent-instruction-changes)=

# ADR 0007: Suggestion skills and agent instruction changes

## Status

Accepted.

Initial implementation landed with a review-only `SuggestionEngine`, persisted
suggestion artifacts, CLI rendering commands, and read-only MCP listing tools.
Suggestions do not edit instruction files or call an LLM automatically.

## Context

The insights engine from {ref}`adr-agentic-insights-engine` can identify
similar prompts, variants, and meaningful omissions. Those findings are useful
only if they become safe, reviewable changes to project instruction surfaces:
AGENTS.md files, skills, local agent guidance, or future agent-specific
configuration.

That is a separate architectural concern from DB indexing and insight
generation. Suggesting a change has user-facing consequences. It can affect
future agent behavior, cross-project conventions, and local trust boundaries.
agentgrep search must remain a read-only local evidence surface unless a user
explicitly invokes a suggestion workflow.

Prior systems point to the same direction:

- ADR 0004 keeps query intent, planning, execution, and result sinks separate.
  Suggestion skills should follow the same boundary: they query evidence
  through a backend contract and render suggestions as outputs, not hidden side
  effects: {ref}`adr-headless-query-planning-non-blocking-execution`.
- LangGraph models long-running agent work as explicit runs, checkpoints,
  statuses, interrupts, and commands. The useful pattern is not the framework
  itself, but the explicit pause/resume and human-review control surface:
  [SDK run and interrupt schema](https://github.com/langchain-ai/langgraph/blob/1.1.3/libs/sdk-py/langgraph_sdk/schema.py)
  and [Postgres checkpointer setup](https://github.com/langchain-ai/langgraph/blob/1.1.3/libs/checkpoint-postgres/README.md).
- Chroma's separation of system metadata, log state, and execution is a
  useful reminder that suggestion state should be another materialized artifact
  instead of being mixed into raw DB records:
  [sysdb mixin](https://github.com/chroma-core/chroma/blob/1.5.9/chromadb/db/mixins/sysdb.py)
  and [log service](https://github.com/chroma-core/chroma/blob/1.5.9/chromadb/logservice/logservice.py).

The practical rule is simple: an LLM may call agentgrep through a CLI or MCP
tool only when that tool is available and selected. agentgrep itself must not
silently call an LLM during normal search.

## Decision

agentgrep will treat suggestion skills as review workflows over insight
artifacts.

A suggestion skill may query insight outputs, collect evidence packs, call an
LLM judge when explicitly configured, and produce a structured suggestion. It
must not directly edit AGENTS.md, create skills, update project guidance, or
change agent configuration without an explicit patch/apply step owned by the
caller.

Suggested changes take effect only after they are accepted and written to the
relevant instruction surface. Existing agent sessions may need a restart,
reload, or explicit context refresh before they observe the changed AGENTS.md
or skill. New sessions should see the accepted files through their normal
instruction-loading behavior.

## Interfaces

Names below describe intended internal contracts. They are not public APIs
until implemented and documented.

`SuggestionQuery`
: User intent for a suggestion run. It names the target project or instruction
  surface, the requested suggestion type, evidence limits, and whether optional
  LLM judging is allowed.

`SuggestionSkill`
: Headless workflow that queries insight artifacts and emits suggestion
  events. It does not scan raw history directly unless the insights engine
  requests a DB refresh.

`SuggestionArtifact`
: Stored output with target path or scope, suggested change summary, evidence
  ids, confidence, rationale, model/tool provenance, and review state.

`SuggestionPatch`
: Optional patch representation generated from an accepted artifact. It is a
  separate artifact so review can happen before mutation.

`InstructionSurface`
: Typed target for AGENTS.md, skill files, or future instruction stores. It
  records reload expectations and safety constraints for the target.

## Suggestion rules

Suggestion workflows must preserve the read-only default:

- Normal `agentgrep search`, `agentgrep find`, and MCP search tools do not call
  LLMs.
- An explicit future command, such as `agentgrep insights suggest`, may call an
  LLM only when configured and consented.
- LLM input is a bounded evidence pack, not the whole local history.
- LLM output is a judgment or draft suggestion, not an automatic file write.
- Suggestions must record evidence ids and confidence, not just prose.

Instruction changes require review:

1. Query insights for relevant clusters, variants, and omission findings.
2. Build a bounded evidence pack.
3. Optionally ask an LLM judge to classify the evidence and draft wording.
4. Store a `SuggestionArtifact`.
5. Present the suggestion to the caller.
6. Create a patch only after explicit acceptance.
7. Let the caller run the normal verification and commit workflow.

## Consequences

### Positive

- Suggestion behavior is auditable and does not alter future agent behavior
  invisibly.
- LLM usage is explicit, bounded, and tied to stored evidence.
- The same suggestion artifacts can support CLI, MCP, TUI, and future frontend
  surfaces.
- AGENTS.md and skill changes remain normal repo changes that can be reviewed,
  tested, committed, or rejected.

### Tradeoffs

- The workflow has more steps than direct auto-editing.
- Existing sessions may not observe accepted instruction changes immediately.
- Suggestion quality depends on insight quality and evidence selection.

### Risks

Instruction overfitting: a suggestion could encode a local habit that should
not become general guidance. The mitigation is evidence review and target
context matching before patch generation.

Hidden LLM dependence: suggestion workflows could make local search feel like
it requires a model. The mitigation is to keep normal search LLM-free and make
LLM judging opt-in.

Stale reload assumptions: different agent tools load AGENTS.md and skills at
different times. The mitigation is to state reload expectations in the
suggestion artifact rather than pretending all tools apply changes
immediately.

## Final position

Suggestion skills are consumers of insight artifacts. They produce reviewable
recommendations and optional patches, but they do not silently call LLMs during
search and do not directly change AGENTS.md, skills, or agent behavior without
explicit acceptance.
