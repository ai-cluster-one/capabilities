---
name: Mailbox
description: The project's IMAP/SMTP mailbox access — read incoming mail, save attachments, flag/move messages, and send, via the `mailbox` CLI. A thin adapter; this project's own drain/triage/routing lives in its operations layer, not in the tool. Profiles in mailbox.json, the app-password in .env.
---

# Mailbox

The project's **IMAP/SMTP mailbox access** — reading incoming mail (and its attachments), flagging and moving messages, and **sending** — through the `mailbox` CLI. A thin adapter: the tool itself is project-agnostic, so any drain / triage / invoice-routing this project does lives in its **own operations layer** that reads the tool's JSON, never in the tool.

> Template note: fill the profile specifics at install. This capability installs under the **`mailbox`** namespace (the CLI discovers `.capabilities/mailbox/mailbox.json` by that path). Replace this role paragraph with how *this* project actually uses the mailbox (which profiles, what it reads and sends), and keep the sibling links pointing at this folder's real files. Keep this file **lightweight** — role + pointers; the command surface is `mailbox help`, not here.

## Interaction

Via the `mailbox` CLI on `PATH` — run `mailbox help` first (the CLI surface is self-documenting), then `mailbox doctor` to validate IMAP + SMTP login. Pick a profile with `--mailbox <id|address>`; omit it when the config defines exactly one.

**Config + secret are project-scoped, in two homes:**

- Non-secret wiring (address, IMAP/SMTP host+port, the secret env-var name) lives in [mailbox.json](mailbox.json) in this folder — the CLI discovers it here.
- The app-password is the only secret: each profile names its env var (`secret_env`); set the value in the project's `.env` / `.env.local` (gitignored), never in `mailbox.json`.

Use `message_id` (not the IMAP `uid`) as the idempotency key when processing — uids reset if the folder's `UIDVALIDITY` changes.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: which profiles exist and what each is for, plus the secret env-var key each one needs set in `.env`. The wiring itself lives in [mailbox.json](mailbox.json).
- [reference.md](reference.md) — the standing home for project-specific operational context (what a folder means here, sending conventions, routing quirks in this project's mail). Ships empty as a self-describing scaffold; populated as context accrues.
