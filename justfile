# justfile for agentgrep
# https://just.systems/

set shell := ["bash", "-uc"]

# File patterns
py_files := "find . -type f -not -path '*/\\.*' | grep -i '.*[.]py$' 2> /dev/null"
doc_files := "find . -type f -not -path '*/\\.*' | grep -i '.*[.]rst$\\|.*[.]md$\\|.*[.]css$\\|.*[.]py$\\|mkdocs\\.yml\\|CHANGES\\|README\\|TODO\\|.*conf\\.py' 2> /dev/null"
all_files := "find . -type f -not -path '*/\\.*' | grep -i '.*[.]py$\\|.*[.]rst$\\|.*[.]md$\\|.*[.]css$\\|.*[.]py$\\|mkdocs\\.yml\\|CHANGES\\|TODO\\|.*conf\\.py' 2> /dev/null"

# List all available commands
default:
    @just --list

# Run the default non-slow pytest lane.
[group: 'test']
test *args:
    uv run py.test {{ args }}

# Run every configured test, including slow coverage.
[group: 'test']
test-all *args:
    uv run py.test {{ args }} -m ""

# Run executable examples and documentation infrastructure.
[group: 'test']
test-docs *args:
    uv run py.test {{ args }} -m documentation

# Run the complete MCP resource cluster.
[group: 'test']
test-mcp *args:
    uv run py.test {{ args }} -m mcp

# Run retained setup and repository-configuration coverage.
[group: 'test']
test-setup *args:
    uv run py.test {{ args }} -m setup

# Run pure and mounted Textual coverage.
[group: 'test']
test-tui *args:
    uv run py.test {{ args }} -m tui

# Run every test excluded only for execution cost.
[group: 'test']
test-slow *args:
    uv run py.test {{ args }} -m slow

# Run tests then start continuous testing with pytest-watcher
[group: 'test']
start:
    just test
    uv run ptw .

# Watch files and run tests on change (requires entr)
[group: 'test']
watch-test:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v entr > /dev/null; then
        {{ all_files }} | entr -c just test
    else
        just test
        just _entr-warn
    fi

# Build documentation
[group: 'docs']
build-docs:
    just -f docs/justfile html

# Watch files and rebuild docs on change
[group: 'docs']
watch-docs:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v entr > /dev/null; then
        {{ doc_files }} | entr -c just build-docs
    else
        just build-docs
        just _entr-warn
    fi

# Serve documentation
[group: 'docs']
serve-docs:
    just -f docs/justfile serve

# Watch and serve docs simultaneously
[group: 'docs']
dev-docs:
    #!/usr/bin/env bash
    set -euo pipefail
    just watch-docs &
    just serve-docs

# Start documentation server with auto-reload
[group: 'docs']
start-docs:
    just -f docs/justfile start

# Start documentation design mode (watches static files)
[group: 'docs']
design-docs:
    just -f docs/justfile design

# Format code with ruff
[group: 'lint']
ruff-format:
    uv run ruff format .

# Run ruff linter
[group: 'lint']
ruff:
    uv run ruff check .

# Watch files and run ruff on change
[group: 'lint']
watch-ruff:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v entr > /dev/null; then
        {{ py_files }} | entr -c just ruff
    else
        just ruff
        just _entr-warn
    fi

# Run ty type checker
[group: 'lint']
ty:
    uv run ty check

# Watch files and run ty on change
[group: 'lint']
watch-ty:
    uv run ty check --watch

# ---- OpenTelemetry / LGTM dev workflow ----

# Start the local Grafana LGTM stack used by telemetry acceptance checks
[group: 'otel']
otel-up:
    #!/usr/bin/env bash
    set -euo pipefail
    if docker inspect agentgrep-lgtm > /dev/null 2>&1; then
        docker start agentgrep-lgtm > /dev/null
    else
        docker run -d --name agentgrep-lgtm -p 3000:3000 -p 3100:3100 -p 3200:3200 -p 4040:4040 -p 4317:4317 -p 4318:4318 -p 9090:9090 grafana/otel-lgtm:latest
    fi

# Stop and remove the local Grafana LGTM stack
[group: 'otel']
otel-down:
    docker rm -f agentgrep-lgtm

# Run a local telemetry smoke workload against LGTM
[group: 'otel']
otel-smoke run_id='agentgrep-otel-smoke':
    AGENTGREP_OTEL=live AGENTGREP_DEBUG_SESSION_ID={{ run_id }} OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 PYROSCOPE_SERVER_ADDRESS=http://localhost:4040 uv run python scripts/otel_smoke.py --run-id {{ run_id }}

# Run live LGTM acceptance checks for traces, metrics, logs, and profiles
[group: 'otel']
otel-acceptance *args:
    uv run python scripts/otel_acceptance.py --start-stack {{ args }}

[private]
_entr-warn:
    @echo "----------------------------------------------------------"
    @echo "     ! File watching functionality non-operational !      "
    @echo "                                                          "
    @echo "Install entr(1) to automatically run tasks on file change."
    @echo "See https://eradman.com/entrproject/                      "
    @echo "----------------------------------------------------------"

# ---- MCP dev workflow ----

# Detect installed MCP-aware CLIs and their config files
[group: 'mcp-swap']
mcp-detect:
    uv run scripts/mcp_swap.py detect

# Show the current MCP server entry per CLI for this repo
[group: 'mcp-swap']
mcp-status:
    uv run scripts/mcp_swap.py status --repo .

# Rewrite installed CLI configs to run this local checkout (pass --scope user, etc.)
[group: 'mcp-swap']
mcp-use-local *args:
    uv run scripts/mcp_swap.py use-local --repo . {{ args }}

# Restore CLI configs from the timestamped backup written by mcp-use-local
[group: 'mcp-swap']
mcp-revert *args:
    uv run scripts/mcp_swap.py revert {{ args }}
