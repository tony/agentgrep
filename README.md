# agentgrep

[![PyPI version](https://img.shields.io/pypi/v/agentgrep.svg)](https://pypi.org/project/agentgrep/)
[![Python versions](https://img.shields.io/pypi/pyversions/agentgrep.svg)](https://pypi.org/project/agentgrep/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Read-only search for local AI agent prompts and history across Codex,
Claude Code, Cursor, and Gemini.

`agentgrep` ships two surfaces over the same discovery + parsing layer:

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

Other install methods (pipx, uv add, pip install) and full MCP-client
setup snippets live in the [installer widgets on agentgrep.org](https://agentgrep.org/library/)
— one tabbed picker per surface.

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
    search_type="all",
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
