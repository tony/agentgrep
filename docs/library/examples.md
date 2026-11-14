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

This request uses prompt effort because its prompt-only scope omits effort. For
bounded conversation search, set `effort` to `targeted` and omit scope so MCP
infers `all`. An explicit `scope="prompts"` with targeted effort is rejected.
Targeted results remain approximate; use `exhaustive` when you need every
eligible readable conversation backend.

## Python search query

Use {class}`~agentgrep.RecordOrigin` on {class}`~agentgrep.SearchQuery`
when code builds the same filter directly before calling
{func}`~agentgrep.run_search_query`. Prefer
{func}`~agentgrep.run_search_result` when you also need terminal status,
coverage, diagnostics, or next actions:

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

(library-examples-wsl)=

## Windows host paths from WSL

{mod}`agentgrep.wsl` finds the Windows user profiles a WSL distro can reach
and names the places agent stores live in them. The path type opens and
compares like any {class}`pathlib.Path`; only `repr()` and `.display` replace
the account name with a Windows variable. This example builds a scratch users
directory in place of `/mnt/c/Users`:

```python
import os
import pathlib
import tempfile

from agentgrep import wsl

users = pathlib.Path(tempfile.mkdtemp()) / "Users"
(users / "someone" / "AppData" / "Roaming" / "Claude").mkdir(parents=True)
(users / "Public").mkdir()
os.environ[wsl.USERS_ROOT_ENV] = str(users)
try:
    (profile,) = wsl.host_user_profiles(require_wsl=False)
    claude = profile.app_dirs("Claude")[0]
    assert repr(claude) == "WSLHostPath('%APPDATA%/Claude')"
    assert claude == users / "someone" / "AppData" / "Roaming" / "Claude"
finally:
    del os.environ[wsl.USERS_ROOT_ENV]

drive_path = wsl.WSLHostPath.from_windows("C:\\Users", pathlib.PurePosixPath("/mnt"))
assert str(drive_path) == "/mnt/c/Users"
assert drive_path.as_windows(pathlib.PurePosixPath("/mnt")) == pathlib.PureWindowsPath("C:/Users")
```

(library-examples-mcp-windows)=

## MCP Windows-side search

On WSL, a `path:` predicate in {tool}`search`'s `terms` keeps only the
Windows-side results. Paths in the response print as `%USERPROFILE%/…`,
`%APPDATA%/…`, or `%LOCALAPPDATA%/…`, and `path:%USERPROFILE%` covers all
three. Pass a result's `ref` to {tool}`inspect_result`: the ref records which
profile the result came from, so it opens the real file even when two Windows
profiles hold the same display path.

This example runs both calls through FastMCP's in-process client against a
scratch Windows profile, standing in for a WSL distro so it runs anywhere:

```python
import asyncio
import json
import os
import pathlib
import tempfile
from unittest import mock

from fastmcp import Client

from agentgrep import wsl
from agentgrep.mcp.server import build_mcp_server

users = pathlib.Path(tempfile.mkdtemp()) / "Users"
transcript = users / "someone" / ".claude" / "projects" / "-work" / "s1.jsonl"
transcript.parent.mkdir(parents=True)
prompt = {
    "type": "user",
    "uuid": "u1",
    "sessionId": "s1",
    "timestamp": "2026-01-01T00:00:00.000Z",
    "message": {"role": "user", "content": "deploy the windows build"},
}
transcript.write_text(json.dumps(prompt) + "\n", encoding="utf-8")


async def windows_side_search() -> None:
    async with Client(build_mcp_server()) as client:
        found = await client.call_tool(
            "search",
            {
                "terms": ["path:%USERPROFILE%", "deploy"],
                "agent": "claude",
                "effort": "exhaustive",
            },
        )
        (result,) = found.structured_content["results"]
        assert result["path"] == "%USERPROFILE%/.claude/projects/-work/s1.jsonl"
        opened = await client.call_tool("inspect_result", {"ref": result["ref"]})
        (record,) = opened.structured_content["records"]
        assert record["text"] == "deploy the windows build"


with (
    mock.patch.dict(os.environ, {wsl.USERS_ROOT_ENV: str(users)}),
    mock.patch.object(wsl, "is_wsl", return_value=True),
):
    asyncio.run(windows_side_search())
```

On a WSL machine, leave out both patches: agentgrep finds the Windows profiles
under the automount root itself.

Put `NOT path:%USERPROFILE%` in `terms` to drop the Windows side instead.

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
