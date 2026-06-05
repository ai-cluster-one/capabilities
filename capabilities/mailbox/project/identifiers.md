# Mailbox — identifiers

All fixed, non-secret structural values for this project's mailbox setup. Pure lookup; the operating model, command surface, and JSON contract are in `mailbox help`.

> Template note: fill these at install from the project's real mailboxes. The **wiring values themselves** (addresses, hosts, ports) are not restated here — they live in `mailbox.json`, the CLI's own config (sibling file). This file holds only the at-a-glance profile roster and the secret env keys to set.

## Config + secret homes

- **Wiring** — `mailbox.json` in this folder (the `mailbox` capability's config): profile id → `address`, `imap_host`/`imap_port`, `smtp_host`/`smtp_port`, `secret_env`. The CLI reads it directly; not restated here by design.
- **Secret** — the app-password, in the project's `.env` / `.env.local` (gitignored), under the env-var key each profile names in its `secret_env`. Process env overrides the file, so a host-injected secret wins.

## Profiles

The mailbox profiles this project defines, by the id passed to `--mailbox`. Wiring is in `mailbox.json`; this is the roster + what each is for.

| Profile id | Address | Role in this project | Secret env key (set in `.env`) |
|---|---|---|---|
| `<profile-id>` | `<user@example.com>` | `<what this project uses it for>` | `MAILBOX_<PROFILE_ID>_APP_PASSWORD` |

Fill one row per profile in `mailbox.json`. The secret env key is the `secret_env` name from that profile — set its value in `.env` / `.env.local`, never here.
