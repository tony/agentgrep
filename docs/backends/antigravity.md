(backend-antigravity)=

# Antigravity

Google Antigravity uses two local storage layouts under the broader Gemini
home tree. agentgrep exposes them as separate backends so CLI prompt recall
and IDE-local artifacts keep distinct store descriptors and search behavior.

- {doc}`/backends/antigravity-cli` covers `--agent antigravity-cli`,
  including the default-searchable CLI `history.jsonl` prompt log and
  inspectable protobuf conversation artifacts.
- {doc}`/backends/antigravity-ide` covers `--agent antigravity-ide`,
  including IDE protobuf transcripts, Markdown brain artifacts, skills, and
  settings.
