#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER_NAME="${AGENTGREP_LGTM_CONTAINER:-agentgrep-lgtm}"
# Pin a known-good otel-lgtm release instead of :latest for reproducible
# dev/CI stacks; override with AGENTGREP_LGTM_IMAGE. 0.28.0 runs Prometheus
# 3.11.3 with --web.enable-otlp-receiver and --enable-feature=exemplar-storage,
# so it ingests the OTLP exemplars the app emits (trace-based filter) and the
# metric->trace pivot works in Grafana out of the box.
#
# Host-port gotcha (Rancher Desktop / WSL): if a host process already listens
# on :9090 (e.g. a Debian-packaged system Prometheus), the host-side forwarder
# shadows the container's published port, so a tool hitting host localhost:9090
# reaches the wrong server and sees no exemplars. Grafana is unaffected — it
# queries the container's Prometheus internally — so verify exemplars in Grafana
# or via `docker exec`, not a bare host curl to :9090.
LGTM_IMAGE="${AGENTGREP_LGTM_IMAGE:-grafana/otel-lgtm:0.28.0}"
# Bump when the mounted config, image, or run shape changes so an existing
# container is recreated (docker run) rather than restarted (docker start) —
# recreation also re-stages the single-file bind mounts cleanly under
# Rancher Desktop / WSL, where docker start reuses a stale empty mount folder.
CONFIG_LABEL="prometheus3-exemplars-dashboards-v1"
SOURCE_MAP="${AGENTGREP_PYROSCOPE_SOURCE_MAP:-$ROOT/.tmp/lgtm/.pyroscope.yaml}"

if [[ -n "${PYTHON:-}" ]]; then
    read -r -a python_cmd <<< "$PYTHON"
elif command -v uv > /dev/null 2>&1 && [[ -f "$ROOT/pyproject.toml" ]]; then
    python_cmd=(uv run python)
else
    python_cmd=(python)
fi

"${python_cmd[@]}" "$ROOT/scripts/lgtm/generate_pyroscope_source_map.py" --output "$SOURCE_MAP"

# Regenerate the provisioned Grafana dashboard suite so a fresh checkout
# always has the agentgrep boards on startup; the folder is bind-mounted below.
"${python_cmd[@]}" "$ROOT/scripts/lgtm/generate_dashboards.py" --output "$ROOT/scripts/lgtm/dashboards"

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
    -v "$ROOT/scripts/lgtm/grafana-dashboards-agentgrep.yaml:/otel-lgtm/grafana/conf/provisioning/dashboards/agentgrep.yaml:ro"
    -v "$ROOT/scripts/lgtm/dashboards:/otel-lgtm/dashboards-agentgrep:ro"
    -v "$ROOT/scripts/lgtm/pyroscope-config.yaml:/otel-lgtm/pyroscope-config.yaml:ro"
)

for name in GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET GITHUB_SESSION_SECRET; do
    if [[ -n "${!name:-}" ]]; then
        docker_run+=(-e "$name")
    fi
done

docker_run+=("$LGTM_IMAGE")

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
