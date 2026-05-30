(backends)=

# Backends

agentgrep reads on-disk stores from multiple AI coding assistants.
Each backend page documents the agent's path layout, environment
overrides, store descriptors, and record schemas.

## Support matrix

| Agent   | Prompt History | Chat Sessions | Session Index | Memory | Plans |
|---------|:--------------:|:-------------:|:-------------:|:------:|:-----:|
| Codex   | {doc}`codex`   | {doc}`codex`  |               |        |       |
| Claude  | {doc}`claude`  | {doc}`claude` |               |        |       |
| Cursor  |                | {doc}`cursor` | {doc}`cursor` |        |       |
| Gemini  | {doc}`gemini`  | {doc}`gemini` |               |        |       |
| Grok    | {doc}`grok`    | {doc}`grok`   | {doc}`grok`   |        |       |

Cells with links are actively searched by default. Blank cells are
either catalogued but not yet parsed, or not applicable to the agent.

```{toctree}
:hidden:

codex
claude
cursor
gemini
grok
```
