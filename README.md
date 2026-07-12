# agentgrep

[![PyPI version](https://img.shields.io/pypi/v/agentgrep.svg)](https://pypi.org/project/agentgrep/)
[![Python versions](https://img.shields.io/pypi/pyversions/agentgrep.svg)](https://pypi.org/project/agentgrep/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Read-only search for local AI agent prompts and opt-in conversations
across Codex, Claude Code, Cursor, Gemini, Antigravity, Grok, Pi, OpenCode, and VS Code.

`agentgrep` provides a CLI and an MCP server over the same discovery + parsing layer:

- **A terminal CLI** (`agentgrep`) with a Textual TUI for interactive
  browsing of normalized records.
- **An MCP server** (`agentgrep-mcp`) that exposes search, discovery,
  catalog, and validation tools to any client that speaks Model
  Context Protocol.

> **Pre-alpha.** APIs may change.

## Install

```console
$ uvx agentgrep --help
```

Other install methods (pipx, uv add, pip install) and full setup
snippets live in the
[installer widget on agentgrep.org/cli/](https://agentgrep.org/cli/).

## CLI quickstart

Search your prompts across every configured agent — ranked, deduped,
newest first:

```console
$ agentgrep search "deploy"
```

<a href="https://agentgrep.org/_static/demos/vhs/agentgrep-search.mp4"><img src="https://raw.githubusercontent.com/tony/agentgrep/master/docs/_static/demos/posters/agentgrep-search.png" alt="agentgrep search ranking matching prompts by relevance" width="100%" /></a>

Search prompts and conversations together in one sweep:

```console
$ agentgrep search "deploy" --scope all
```

Prefer ripgrep-shaped flags? `grep` mirrors `rg` / `ag` against the
same records:

```console
$ agentgrep grep "deploy" --scope conversations
```

Stream JSON so a non-MCP agent or shell pipeline can consume the
results:

```console
$ agentgrep find --json
```

Open the read-only Textual explorer, seeded with a query:

```console
$ agentgrep ui "deploy"
```

<a href="https://agentgrep.org/_static/demos/vhs/agentgrep-ui.mp4"><img src="https://raw.githubusercontent.com/tony/agentgrep/master/docs/_static/demos/posters/agentgrep-ui.png" alt="agentgrep Textual explorer filtering local agent prompts" width="100%" /></a>

`--json` and `--ndjson` make every command pipe-friendly, and any
search-shaped subcommand takes `--ui` to hand the same query to the
explorer (e.g. `agentgrep grep "deploy" --ui`). Agents that don't
speak MCP can drive the CLI directly; see
<https://agentgrep.org/cli/> for the per-subcommand reference.

## MCP server: quickest setup

In Claude Code:

```console
$ claude mcp add agentgrep -- uvx --from agentgrep agentgrep-mcp
```

For Claude Desktop / Codex / Cursor / Gemini snippets, see
<https://agentgrep.org/mcp/>.

## Library quickstart

```python
from pathlib import Path

import agentgrep

backends = agentgrep.select_backends()
query = agentgrep.SearchQuery(
    terms=("hello",),
    scope="all",
    any_term=False,
    regex=False,
    case_sensitive=False,
    agents=agentgrep.AGENT_CHOICES,
    limit=10,
)
for record in agentgrep.run_search_query(Path.home(), query, backends=backends):
    print(record.agent, record.title or record.path)
```

## Links

- Documentation: <https://agentgrep.org/>
- Source: <https://github.com/tony/agentgrep>
- Issues: <https://github.com/tony/agentgrep/issues>
- Changelog: [CHANGES](CHANGES)
- License: [MIT](LICENSE)
