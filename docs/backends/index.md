(backends)=

# Backends

agentgrep reads on-disk stores from multiple AI coding assistants.
Each backend page documents the agent's path layout, environment
overrides, store descriptors, and record schemas.

## Support matrix

### Searched by default

Cells link to backend pages with the matching store descriptors and
record schemas.

| Agent   | Prompt History | Primary Chat | Supplementary Chat | Index / Summaries |
|---------|:--------------:|:------------:|:------------------:|:-----------------:|
| Codex   | {doc}`codex`   | {doc}`codex` |                    |                   |
| Claude  | {doc}`claude`  | {doc}`claude` | {doc}`claude`     |                   |
| Cursor  |                | {doc}`cursor` | {doc}`cursor`     | {doc}`cursor`     |
| Gemini  | {doc}`gemini`  | {doc}`gemini` | {doc}`gemini`     |                   |
| Grok    | {doc}`grok`    | {doc}`grok`  |                    | {doc}`grok`       |

### Catalogued only

These stores are documented in the catalogue but are not searched by
default. `deferred` means adapter support may be added later; `off`
means agentgrep intentionally skips that store type.

| Agent   | Memory | Plans / Todos | App State / Config | Cache / Source Trees |
|---------|:------:|:-------------:|:------------------:|:--------------------:|
| Codex   | deferred |             | deferred           |                      |
| Claude  | deferred | deferred    | deferred           | off                  |
| Cursor  |        | deferred      | deferred / off     | off                  |
| Gemini  |        |               | off                |                      |
| Grok    | deferred |             | deferred / off     | off                  |

```{toctree}
:hidden:

codex
claude
cursor
gemini
grok
```
