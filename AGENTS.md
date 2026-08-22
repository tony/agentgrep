# AGENTS.md

agentgrep is a read-only CLI (`agentgrep`) and MCP server (`agentgrep-mcp`)
for searching local AI agent prompt and conversation history across Codex,
Claude Code, Cursor, and other agents.

Follow the conventions already in the tree, and keep a change scoped to what
was asked for.

## What is here

| Path | What it is |
| ---- | ---------- |
| `src/agentgrep/__init__.py` | Public compatibility facade, record dataclasses |
| `src/agentgrep/cli/` | argparse surface, text/JSON/NDJSON renderers |
| `src/agentgrep/query/` | Field registry, parser, AST, compiler |
| `src/agentgrep/_engine/` | Planning, matching, scanning, scheduling, profiling |
| `src/agentgrep/mcp/` | FastMCP server, models, middleware, tools |
| `src/agentgrep/ui/` | Textual TUI; see its nested `AGENTS.md` |
| `src/pytest_documentation/` | Repo-local pytest plugin for executable docs |
| `tests/` | Focused suite; see its nested `AGENTS.md` |
| `docs/` | Sphinx site; `docs/dev/adr/` holds the ADRs |
| `CHANGES` | The changelog, rendered as the docs history page |
| `scripts/` | Profiler, benchmark, and MCP-config-swap tools |
| `fastmcp.json` | MCP server config, validated as a doc example |

## Which policy applies

- Documentation, user-facing text, `CHANGES`, release notes, commit messages,
  docstrings, and source comments:
  [.github/WRITING.md](.github/WRITING.md)
- Environment, the gates, tests, documentation builds, releases, and pull
  requests: [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md)

Each of those is the single home for its subject. Where a rule seems to be
stated twice, the file listed above is the one that governs.

## Change discipline

- Make the smallest coherent change that solves the verified problem; keep
  unrelated cleanup out of it.
- Reuse an existing file, helper, API, or test before adding a new one. Keep
  new APIs private until a caller outside the module needs them.
- Add a file only for a durable boundary — a distinct responsibility,
  independent reuse, or splitting an oversized module — not for a single-use
  helper or a one-line re-export.
- Add a test for every user-visible behaviour change, and a `CHANGES` entry
  for every change to the public API, CLI, configuration, or output.
- A passing gate is evidence only once it has been shown capable of failing.
  Pair a new test with a deliberate break that proves it bites.

## Domain facts

- Python 3.14+ only. No native Windows support; WSL is supported.
- Python is the default implementation language; native code needs a
  measured baseline and follows ADR 0002 (Rust/Python compatibility) and
  ADR 0003 (native boundary shapes).
- The public CLI/MCP surface — command names, flags, exit statuses, JSON/
  NDJSON keys, MCP tool schemas — is compatibility-sensitive (ADR 0006).
- Pydantic is required, but is the schema/validation layer only at MCP and
  event boundaries; CLI JSON/NDJSON output uses direct TypedDict serializers.
- Subtree-specific rules live nested: `tests/AGENTS.md`,
  `src/agentgrep/ui/AGENTS.md`. Each has a `CLAUDE.md` symlink beside it.

## References

- Documentation: https://agentgrep.org/
- Source: https://github.com/tony/agentgrep
- Architecture decisions: `docs/dev/adr/`
- FastMCP: https://github.com/jlowin/fastmcp
- MCP Specification: https://modelcontextprotocol.io/
- Textual: https://textual.textualize.io/
