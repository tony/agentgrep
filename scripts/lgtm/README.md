# Local LGTM Source Linking

`just otel-up` starts the local Grafana LGTM container through
`scripts/lgtm/up.sh`. The helper generates `.tmp/lgtm/.pyroscope.yaml` for
local inspection, then mounts the Grafana datasource and Pyroscope config files
from this directory into the stock `grafana/otel-lgtm` image.

The generated source map contains machine-local absolute prefixes so Pyroscope
can match paths from local Python stack samples. It is intentionally written
under `.tmp/` and should not be committed.

Grafana forwards the `pyroscope_git_session` cookie to Pyroscope through
`grafana-datasources.yaml`. Pyroscope still needs its GitHub OAuth app
environment variables when source links require authenticated GitHub access:

```console
$ GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... GITHUB_SESSION_SECRET=... just otel-up
```

`gh auth token` can provide a GitHub API token for the current user, but
Pyroscope's GitHub source integration does not consume that token directly as
the OAuth app client ID, client secret, or session secret.
