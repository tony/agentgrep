#!/usr/bin/env bash
# Build the demo sandbox: synthetic agent history + a bin/ shim for the tools
# the tapes invoke.
#
# The VHS tapes are committed, so they must not contain a developer's absolute
# paths. Instead of baking a venv path into PATH, resolve agentgrep and jq here
# and expose them through the sandbox's own bin -- the tapes then only ever
# reference /tmp/agentgrep-demo. The agentgrep wrapper also removes ambient
# color controls so recordings do not inherit an automation shell's palette.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DEMO_HOME="/tmp/agentgrep-demo"

AGENTGREP_BIN="${AGENTGREP_BIN:-$REPO/.venv/bin/agentgrep}"
if [ ! -x "$AGENTGREP_BIN" ]; then
  echo "agentgrep not found at $AGENTGREP_BIN (run: uv sync)" >&2
  exit 1
fi

python3 "$REPO/docs/_static/demos/seed_demo_home.py" "$DEMO_HOME"

mkdir -p "$DEMO_HOME/.local/bin"
AGENTGREP_SHIM="$DEMO_HOME/.local/bin/agentgrep"
{
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'unset NO_COLOR FORCE_COLOR' \
    'export COLORTERM=truecolor'
  printf 'exec %q "$@"\n' "$AGENTGREP_BIN"
} > "$AGENTGREP_SHIM"
chmod 0755 "$AGENTGREP_SHIM"

JQ_BIN="$(command -v jq || true)"
if [ -n "$JQ_BIN" ]; then
  ln -sf "$JQ_BIN" "$DEMO_HOME/.local/bin/jq"
else
  echo "warning: jq not on PATH; the --json | jq tapes will fail" >&2
fi

echo "demo sandbox ready at $DEMO_HOME"
