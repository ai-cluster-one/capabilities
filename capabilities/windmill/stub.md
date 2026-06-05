---
name: windmill
description: Windmill CLI — drive a Windmill instance (deploy scripts, attach cron schedules, run jobs, read run history/logs, manage secret variables and folders) over its REST API. Run `windmill help` for the full command surface before the first subcommand in a session.
---

Windmill CLI — drive a Windmill instance over its REST API.

- Executable: `windmill` (on `PATH`)
- Credentials: `~/.config/windmill/credentials.env` (`WINDMILL_URL`, `WINDMILL_API_TOKEN`, `WINDMILL_WORKSPACE`; also honored as env vars and `--url` / `--token` / `--workspace` flags — flags > env > file).
- Load full reference: `windmill help`

Run `windmill help` before issuing any subcommand the first time in a session. Project-scoped workspace / folder paths live in each project's own reference, not here.
