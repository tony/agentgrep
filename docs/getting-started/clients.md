(clients)=

# MCP Clients

agentgrep exposes a local stdio MCP server. Any MCP client that can launch a command can run it.

## Codex CLI

From a development checkout:

```toml
[mcp_servers.agentgrep]
command = "uv"
args = ["run", "agentgrep-mcp"]
cwd = "/path/to/agentgrep"
```

For an installed package:

```toml
[mcp_servers.agentgrep]
command = "agentgrep-mcp"
```

## Claude Code

From a development checkout:

```console
$ claude mcp add agentgrep --cwd /path/to/agentgrep -- uv run agentgrep-mcp
```

For an installed package:

```console
$ claude mcp add agentgrep -- agentgrep-mcp
```

## Claude Desktop and Cursor

Use a JSON `mcpServers` entry:

```json
{
  "mcpServers": {
    "agentgrep": {
      "command": "uv",
      "args": ["run", "agentgrep-mcp"],
      "cwd": "/path/to/agentgrep"
    }
  }
}
```

For an installed package:

```json
{
  "mcpServers": {
    "agentgrep": {
      "command": "agentgrep-mcp"
    }
  }
}
```

## FastMCP

The repository includes `fastmcp.json`:

```console
$ uv run fastmcp run fastmcp.json
```

Inspect the server surface:

```console
$ uv run fastmcp inspect fastmcp.json
```
