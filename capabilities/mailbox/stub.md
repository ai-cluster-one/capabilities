---
name: mailbox
description: IMAP/SMTP CLI for one mailbox — list/show messages, save attachments, flag \Seen, move between folders, and send. A thin project-agnostic adapter; any drain/triage orchestration lives in the consuming project. Run `mailbox help` for the full usage contract before the first subcommand in a session.
---

IMAP/SMTP CLI for one mailbox — a thin, project-agnostic adapter that reads, fetches attachments, flags, moves, and **sends** mail over IMAP/SMTP.

- Executable: `mailbox` (on `PATH`)
- Config: a per-project `mailbox.json` holds the non-secret profiles (address, IMAP/SMTP host+port, and the name of the env var holding the app-password). The CLI self-discovers it by walking up from `$CLAUDE_PROJECT_DIR`/cwd to `.capabilities/mailbox/mailbox.json` (or honours `$MAILBOX_CONFIG`). Pick a profile with `--mailbox <id|address>`; omit when only one is defined.
- Secret: only the app-password is secret — each profile names its env var (`secret_env`); the value lives in the project's `.env` / `.env.local` (gitignored), or a host-injected env var. There is **no global credentials file**.
- Dependency: `imap-tools` (the `uv run --script` shebang installs it on first run).
- Load full reference: `mailbox help`

Use `message_id` (not the IMAP `uid`) as the idempotency key when processing. Run `mailbox help` before issuing any subcommand the first time in a session, then `mailbox doctor` to validate IMAP + SMTP login. Project-scoped mailbox profiles and routing live in each project's own config and assets, not here.
