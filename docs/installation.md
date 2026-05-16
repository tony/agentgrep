(installation)=

# Installation

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/) for the development workflow
- Optional command-line backends: `fd`, `rg`, `ag`, and `jq`

agentgrep falls back to Python implementations when optional read-only command-line backends are unavailable.

## Development install

```console
$ git clone https://github.com/tony/agentgrep.git
```

```console
$ cd agentgrep
```

```console
$ uv sync --all-groups
```

Run the CLI from the checkout:

```console
$ uv run agentgrep search "bliss"
```

Run the MCP server:

```console
$ uv run agentgrep-mcp
```

## Package install

When published, install the package into an environment that can read your local agent stores:

```console
$ uv pip install agentgrep
```

or:

```console
$ pip install agentgrep
```

## Optional tools

agentgrep detects available read-only helpers at runtime:

- `fd` for source discovery
- `rg` or `ag` for prefiltering text sources
- `jq` for JSON string flattening

The selected helpers are reported through the MCP capabilities resource.
