# Authoring automations

Run `automations service init` once in the consuming project, then treat the generated config and scripts as ordinary versioned project files.

Each `[[automations]]` entry names a stable id and a script path relative to the project root. Declare either a numeric five-field cron `schedule`, an `every_seconds` interval, or neither for a manual-only automation. Use `environments` to keep production schedules from running on a developer machine.

Scripts inherit the service environment, including values loaded from the project's `.env` and `.env.local`, so credentials remain in environment variables rather than committed config. A script receives its run identity and project paths through the `AUTOMATION_*` variables listed by `automations help`. Exit zero for success and non-zero for failure; stdout and stderr are captured into the run log.

Use `overlap = "skip"` for polling and reconciliation work where a second occurrence adds no value. Use `overlap = "queue"` only when every occurrence must eventually execute, and set a bounded `max_pending`.
