(package-agentgrep-examples)=

# Examples

These examples show the same read-only search surface from the two common
entry points: CLI commands for humans at a shell, and MCP payloads for clients
that call agentgrep as a tool.

## CLI search

Search only Codex prompt records and stop after ten matches:

```console
$ uv run agentgrep grep "database migration" --agent codex --limit 10
```

Prefer records from the current project while keeping global matches
visible:

```console
$ uv run agentgrep search --here "database migration"
```

## CLI discovery

List Cursor CLI sources as structured JSON when you want to script the result:

```console
$ uv run agentgrep find cursor-cli --agent cursor-cli --json
```

## MCP search call

Call {tool}`search` when a client needs normalized result records rather than
terminal text. Add `cwd`, `repo`, or `branch` when the client wants the
same project-aware filtering described in {ref}`cli-search-project-context`:

```json
{
  "tool": "search",
  "arguments": {
    "terms": ["database migration"],
    "agent": "codex",
    "scope": "prompts",
    "cwd": "~/work/django-project",
    "limit": 10
  }
}
```

## Python search query

Use {class}`~agentgrep.RecordOrigin` on {class}`~agentgrep.SearchQuery`
when code builds the same filter directly before calling
{func}`~agentgrep.run_search_query`:

```python
import agentgrep

query = agentgrep.SearchQuery(
    terms=("database migration",),
    scope="prompts",
    any_term=False,
    regex=False,
    case_sensitive=False,
    agents=("codex",),
    limit=10,
    origin_filter=agentgrep.RecordOrigin(cwd="~/work/django-project"),
)

assert query.origin_filter == agentgrep.RecordOrigin(cwd="~/work/django-project")
```

## MCP discovery call

Call {tool}`find` when a client needs source metadata before choosing a search:

```json
{
  "tool": "find",
  "arguments": {
    "pattern": "sessions",
    "agent": "codex",
    "limit": 50
  }
}
```

## MCP resource reads

Read resources when the client needs passive metadata without running a tool:

```text
agentgrep://capabilities
agentgrep://sources
agentgrep://sources/codex
```
