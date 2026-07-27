(mcp-prompts)=

# Prompts

MCP prompts are reusable recipes a client can render before calling tools.

## Search prompts

```{fastmcp-prompt} search_prompts
```

Use this when the user wants matching user prompts. The recipe starts with
`effort="prompt"`, which reads dedicated prompt-history stores only; a fast
miss is not corpus-wide. It names `effort="targeted"` as an explicit,
user-requested escalation for bounded conversation search and tells the client
not to escalate automatically.

```{fastmcp-prompt-input} search_prompts
```

## Search conversations

```{fastmcp-prompt} search_conversations
```

Use this when the user wants full conversation records.

```{fastmcp-prompt-input} search_conversations
```

## Inspect stores

```{fastmcp-prompt} inspect_stores
```

Use this when the user wants to inspect discovered local stores before searching.

```{fastmcp-prompt-input} inspect_stores
```
