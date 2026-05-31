# windmill

Windmill CLI (REST API over an API token) — drive a Windmill instance: deploy scripts, attach cron schedules, run jobs, read run history + logs, manage secret variables and folders.

- Script: `~/bin/windmill`
- Credentials: `~/.config/windmill/credentials.env` (`WINDMILL_URL`, `WINDMILL_API_TOKEN`, `WINDMILL_WORKSPACE`; also honors the same env vars and `--url`/`--token`/`--workspace` flags, flags > env > file).
- Load full reference: `windmill help`

When the user mentions Windmill, run `windmill help` before issuing any subcommand the first time in a session. Project-scoped workspace / folder paths live in each project's own reference file, not here.
