# mailbox — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability. Section set follows the manifest schema established by `windmill/manifest.md`.

## Identity

- **Name**: `mailbox`
- **Summary**: a thin IMAP/SMTP adapter for one mailbox — list/show messages, save attachments, flag `\Seen`, move between folders, and send. It knows nothing about who owns the mailbox; any drain/triage/routing orchestration is a consuming project's operation layer that reads this tool's JSON.
- **Underlying service**: any IMAP/SMTP mail server, named per-profile in the project's `mailbox.json`. Not bundled — the user supplies the server, address, and an app-password.
- **Has authored artifacts**: a config template (`project/mailbox.json.example`); no scripts.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`.
- **imap-tools** (hard, auto) — declared in the script's inline `/// script` metadata (`imap-tools>=1.13`); `uv` installs it into the script's ephemeral environment on first run. Nothing to install by hand.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/mailbox/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/mailbox` | `~/.capabilities/mailbox/bin/mailbox`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `mailbox` resolves by name. |
| `stub.md` | `~/.capabilities/mailbox/stub.md`, surfaced by symlinking it as `~/.claude/skills/mailbox/SKILL.md` — front-matter `name` + `description` load every session, body on demand. |

There is **no `credentials.env.example`** — see Credentials. The install **skips step 2d** (global credentials) for this capability; its config and secret are both project-layer.

## Credentials

**Project-scoped, not a global file — recorded deviation from the [4-tier cascade](../../DOCTRINE.md#the-credential-cascade).** `mailbox` splits its config into two project-layer homes and has no `~/.config/mailbox/credentials.env` tier:

- **Non-secret wiring → `mailbox.json`** (committed): a JSON map of profile id → `address`, `imap_host`/`imap_port`, `smtp_host`/`smtp_port`, and `secret_env` (the *name* of the env var holding that profile's password). The CLI discovers it by walking up from `$CLAUDE_PROJECT_DIR`/cwd to `.capabilities/mailbox/mailbox.json` (preferred) or `.assets/config/mailbox.json` (legacy fallback); `$MAILBOX_CONFIG`, if set in the real environment, overrides the path and wins over everything.
- **Secret → the project's `.env` / `.env.local`** (gitignored), under the env-var key each profile names in `secret_env` (convention `MAILBOX_<ID>_APP_PASSWORD`). After locating the config, the CLI loads `.env` then `.env.local` **without overriding keys already in the environment** — so a host-injected (cron) secret takes precedence over the file. There is **no `--password` flag**.

The precedence the secret actually resolves by is therefore: **process env > project `.env(.local)`** — process env sitting *above* the file, the inverse of the standard cascade, deliberately so an injected secret wins on a deployed box. No flags tier, no user-config tier. This is a justified deviation (DOCTRINE — *Deviations are allowed — and recorded*): the secret is per-mailbox and named dynamically by `secret_env`, so a single global `credentials.env` cannot model it.

## Project artifacts

The whole `project/` template copies into `.capabilities/mailbox/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/mailbox/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/mailbox/identifiers.md` |
| `project/reference.md` | `.capabilities/mailbox/reference.md` (self-describing scaffold; populated on demand) |
| `project/mailbox.json.example` | copied to `.capabilities/mailbox/mailbox.json` and filled with the project's real profiles (non-secret wiring only); the `.example` stays as the reference template. |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | **pinned** — must be `mailbox` | the CLI hardcodes the `.capabilities/mailbox/mailbox.json` discovery path, so this capability installs under namespace **`mailbox`** (not a free name). A project that genuinely needs another namespace sets `$MAILBOX_CONFIG` to the config path instead. | the `.capabilities/mailbox/` path |
| profile `id` / `address` / `imap_host` / `imap_port` / `smtp_host` / `smtp_port` | must-confirm | ask the user per mailbox | `mailbox.json` |
| `secret_env` (the env-var *name*) | discoverable | convention `MAILBOX_<ID>_APP_PASSWORD`; confirm if unsure | `mailbox.json` (the name) **and** the project `.env` (the key, breadcrumbed empty) |
| the app-password (the *value*) | must-confirm (secret) | ask the user; never commit | the project `.env` / `.env.local` only |

A capability is dysfunctional without its must-haves: a filled `mailbox.json` profile and its app-password in `.env`. Resolve those at install; `mailbox doctor` validates IMAP + SMTP login once they're in place.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `mailbox help`).
- **No secret in any committed file** — `mailbox.json` carries only wiring and the *name* of the secret env var; the app-password value lives solely in the project's gitignored `.env` / `.env.local`. The shipped `mailbox.json.example` uses placeholder hosts/addresses (`example.com`), never a real server or account.
- **The credential model is the recorded deviation above** — project-scoped config + secret, process-env-over-`.env` precedence, no global `credentials.env`, no `--password` flag. The Credentials section is its single justification.
- **The whole operating contract lives in `mailbox help`** — command surface, discovery rules, the JSON I/O contract, exit codes, and the `message_id`-not-`uid` idempotency rule. It is project-agnostic, so `mailbox` carries **no guide**; its **reference (slot 5) ships as a self-describing scaffold**, empty until genuine project context accrues.
