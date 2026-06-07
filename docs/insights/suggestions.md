(insights-suggestions)=

# Suggestions

Suggestions are review-only artifacts derived from omission findings.
They are designed to help a person decide whether to create or edit
`AGENTS.md` content or a skill. They do not modify files, create
skills, call an LLM, or reload an agent session by themselves.

Create or list suggestions for a target:

```console
$ agentgrep suggestions list \
    --target AGENTS.md \
    --json
```

Render one suggestion for review:

```console
$ agentgrep suggestions render <suggestion-id>
```

## When changes take effect

A suggested instruction change takes effect only after a patch is
accepted and the relevant agent reloads context. Existing sessions may
need a restart or explicit reload. New sessions normally pick up the
changed `AGENTS.md` or skill file through their normal startup context
loading.

## Review state

Each suggestion carries a confidence score, rationale, target path,
body, and reload note. Treat those fields as an evidence pack for
human review, not as authority to apply the change automatically.

See the command reference for exact flags:

- {ref}`cli-suggestions`
- {ref}`cli-suggestions-list`
- {ref}`cli-suggestions-show`
- {ref}`cli-suggestions-render`
