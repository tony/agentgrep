#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER_NAME="${AGENTGREP_LGTM_CONTAINER:-agentgrep-lgtm}"
CONFIG_LABEL="source-linking-v1"
SOURCE_MAP="${AGENTGREP_PYROSCOPE_SOURCE_MAP:-$ROOT/.tmp/lgtm/.pyroscope.yaml}"

if [[ -n "${PYTHON:-}" ]]; then
    read -r -a python_cmd <<< "$PYTHON"
elif command -v uv > /dev/null 2>&1 && [[ -f "$ROOT/pyproject.toml" ]]; then
    python_cmd=(uv run python)
else
    python_cmd=(python)
fi

"${python_cmd[@]}" "$ROOT/scripts/lgtm/generate_pyroscope_source_map.py" --output "$SOURCE_MAP"

docker_run=(
    run
    -d
    --name "$CONTAINER_NAME"
    --label "agentgrep.lgtm.config=$CONFIG_LABEL"
    -p 3000:3000
    -p 3100:3100
    -p 3200:3200
    -p 4040:4040
    -p 4317:4317
    -p 4318:4318
    -p 9090:9090
    -v "$ROOT/scripts/lgtm/grafana-datasources.yaml:/otel-lgtm/grafana/conf/provisioning/datasources/grafana-datasources.yaml:ro"
    -v "$ROOT/scripts/lgtm/pyroscope-config.yaml:/otel-lgtm/pyroscope-config.yaml:ro"
)

for name in GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET GITHUB_SESSION_SECRET; do
    if [[ -n "${!name:-}" ]]; then
        docker_run+=(-e "$name")
    fi
done

docker_run+=(grafana/otel-lgtm:latest)

if docker inspect "$CONTAINER_NAME" > /dev/null 2>&1; then
    current_config="$(
        docker inspect --format '{{ index .Config.Labels "agentgrep.lgtm.config" }}' \
            "$CONTAINER_NAME" 2> /dev/null || true
    )"
    if [[ "$current_config" != "$CONFIG_LABEL" ]]; then
        docker rm -f "$CONTAINER_NAME" > /dev/null
        docker "${docker_run[@]}"
    else
        docker start "$CONTAINER_NAME" > /dev/null
    fi
else
    docker "${docker_run[@]}"
fi
