(backends-unsupported)=

# Unsupported backends

These agents are documented for storage discovery but **not supported**
for search: their conversation transcripts are stored in an obfuscated
or encrypted form that agentgrep cannot read. agentgrep catalogues
*where* their data lives (so a storage audit is complete) but makes no
claim to cover their prompts or conversations, and they are excluded
from `--agent` selection and default search.

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Windsurf
:link: windsurf
:link-type: doc
Codeium Cascade. Per-session `.pb` transcripts are high-entropy,
apparently-encrypted binary with no extractable text; only the storage
locations are catalogued.
:::

::::

```{toctree}
:hidden:

windsurf
```
