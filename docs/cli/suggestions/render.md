(cli-suggestions-render)=

# agentgrep suggestions render

Render one suggestion as review text that can be copied into a patch
or review note. Rendering does not edit `AGENTS.md`, create skills, or
reload an agent session.

## Examples

Render one suggestion:

```console
$ agentgrep suggestions render <suggestion-id>
```

Read from a non-default agentgrep database:

```console
$ agentgrep suggestions render \
    --db .tmp/agentgrep.sqlite \
    <suggestion-id>
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: suggestions render
    :nodescription:
```
