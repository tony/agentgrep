(architecture-decisions)=

# Architecture decisions

Architecture decision records document compatibility policies that affect
multiple adapters or public payloads.

ADR numbers are assigned against the target branch when a decision lands.
Numbers used by parallel proposal branches are provisional; reviews and
cross-references should use the stable Sphinx label and decision title until
integration resolves any collision.

An adopted ADR's explicit ownership, compatibility and safety boundaries are
normative. Conflicting unpublished implementation details, branch-local ADRs or
older internal patterns must be migrated, reclassified or removed; they do not
silently narrow the adopted decision. Released public behavior remains a
compatibility fact and requires an explicit migration or superseding decision
rather than being dismissed as an implementation detail. A later adopted ADR
may supersede an earlier one only when it says so directly and names the
affected contract.

```{toctree}
:maxdepth: 1

0001-storage-version-detection
0002-pure-python-rust-accelerator-module-compatibility-requirements
0003-native-boundary-and-execution-architecture
0004-headless-query-planning-and-non-blocking-execution
0005-local-insights-reports-and-model-backed-enrichment
0006-public-cli-mcp-surface-contract
0007-query-language-comparison-and-full-queryability
0008-unsupported-obfuscated-backends
0009-cross-host-discovery
0010-module-boundaries-and-facade-re-export-contract
0011-non-blocking-tui-invariants
0012-reusable-tui-widget-architecture
0013-pluggable-tui-layouts-and-workflows
0014-result-order-limit-and-streaming-merge
0015-durable-prompt-corpus-and-derived-search-indexes
0016-progressive-deep-search
0017-prompt-guided-conversation-routing
```
