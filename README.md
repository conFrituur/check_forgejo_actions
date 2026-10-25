# Check_Forgejo_Actions
[![Build](https://github.com/stefankonig/check_forgejo_actions/actions/workflows/build.yml/badge.svg)](https://github.com/conFrituur/check_forgejo_actions/actions/workflows/build.yml)
[![Coverage](https://raw.githubusercontent.com/conFrituur/check_forgejo_actions/refs/heads/coverage-badge/coverage.svg)](https://github.com/conFrituur/check_forgejo_actions/actions/workflows/build.yml)
[![uv](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FconFrituur%2Fcheck_forgejo_actions%2Frefs%2Fheads%2Fmain%2Fpyproject.toml&style=flat&logo=python&logoColor=lightgrey
)](https://www.python.org)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

Icinga/Nagios plugin that checks the last Forgejo action run for every workflow in repositories visible to the API token. It requires Python >=3.12 with the `requests` module and uses the Forgejo v1 API.

>**AI NOTICE:** This project was derived from my (handwritten) check_drone_build plugin and altered with help from AI. All code has been reviewed and tested by me

## Usage

```console
foo@bar:~$ uv run check_forgejo_actions.py --help    
usage: check_forgejo_actions.py [-h] --server <FORGEJO_SERVER> --token <FORGEJO_TOKEN> [--owner OWNER]
                                [--warning-seconds WARNING_SECONDS] [--critical-seconds CRITICAL_SECONDS]
                                [--verbose]

Check Forgejo actions for failed or stuck runs

options:
  -h, --help            show this help message and exit
  --owner, -o OWNER     only check repositories owned by this user or organization
  --warning-seconds, -w WARNING_SECONDS
                        warn when an active run is older than this
  --critical-seconds, -c CRITICAL_SECONDS
                        fail when an active run is older than this
  --verbose, -v

required arguments:
  --server, -s <FORGEJO_SERVER>
                        Forgejo hostname or HTTP(S) base URL
  --token, -t <FORGEJO_TOKEN>
                        Forgejo API token
```

`--server` accepts a hostname or a full HTTP(S) URL. `--owner` optionally limits repository discovery to one user or organization. The warning and critical thresholds are independent and optional; they apply only to running tasks. When both are supplied, warning must be lower than critical.

A failed run is always CRITICAL. Successful, cancelled, and skipped runs are healthy. Unknown statuses or API errors produce UNKNOWN. The check reads `/repos/{owner}/{repo}/actions/tasks`, workflows without tasks are ignored. The tasks endpoint does not expose workflow activation state, so disabled workflows with recorded tasks are included.


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

Add `forgejo_token` to **Protected Custom Variables**in the monitoring module of icingaweb2 so it won't show up in plaintext.
