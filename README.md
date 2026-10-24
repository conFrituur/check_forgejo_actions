# Check Forgejo Actions

Icinga/Nagios plugin that checks the newest Forgejo Actions run for every workflow with recorded tasks in repositories visible to an API token. It requires Python 3.12 or newer and uses the Forgejo v1 API.

## Installation and usage

Install the locked runtime and development dependencies with `uv`:

```console
uv sync --locked --all-extras --dev
uv run check_forgejo_actions.py --help
```

Example:

```console
uv run check_forgejo_actions.py \
  --server https://forgejo.example.org \
  --token "$FORGEJO_TOKEN" \
  --owner my-organization \
  --warning-seconds 3600 \
  --critical-seconds 7200
```

`--server` accepts a hostname, a full HTTP(S) URL, or a Forgejo URL beneath a subpath. `--owner` optionally limits repository discovery to one user or organization. The warning and critical thresholds are independent and optional; they apply only to queued, waiting, pending, requested, or in-progress, running, or blocked tasks. When both are supplied, warning must be lower than critical.

A failed completed run is always CRITICAL. Successful, cancelled, and skipped runs are healthy. Unknown statuses or API errors produce UNKNOWN. The check reads `/repos/{owner}/{repo}/actions/tasks`, selects the highest `run_number` per `workflow_id`, and evaluates all job tasks in that run. Workflows without tasks are ignored. The tasks endpoint does not expose workflow activation state, so disabled workflows with recorded tasks are included.

The token needs read access to all monitored repositories and their Actions data. Store it as a protected Icinga custom variable; never place it directly in the command definition.

## Icinga CheckCommand

```icinga2
object CheckCommand "forgejo-actions" {
    import "plugin-check-command"
    command = [ PluginDir + "/check_forgejo_actions.py" ]
    timeout = 1m
    arguments += {
        "--server" = {
            required = true
            value = "$forgejo_server$"
        }
        "--token" = {
            required = true
            value = "$forgejo_token$"
        }
        "--owner" = "$forgejo_owner$"
        "--warning-seconds" = "$forgejo_warning_seconds$"
        "--critical-seconds" = "$forgejo_critical_seconds$"
    }
}
```

Add `forgejo_token` to **Protected Custom Variables** in Icinga Web so it is not displayed in plaintext.

## Development

```console
uv run ruff check
uv run pytest --cov=check_forgejo_actions
```

Tests use sanitized Forgejo API responses from `tests/ApiResponses/` and never contact a live instance.
