# telegram — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `telegram`
- **Summary**: drive a personal Telegram account over MTProto (a full user account) — health check (`doctor`), interactive login, identity (`whoami`), list dialogs, read and search messages in a chat, send a message, export a chat's full history to JSON (downloading voice/audio + photos/stickers), and fill voice/audio transcription placeholders via Deepgram.
- **Underlying service**: **Telegram** over the **MTProto** protocol via [Telethon](https://docs.telethon.dev), authenticated as a full user account — an app `api_id`/`api_hash` from [my.telegram.org/apps](https://my.telegram.org/apps) plus a persisted login session. The optional `transcribe` path additionally calls **Deepgram**'s HTTP API.
- **Has authored artifacts**: no.
- **Config dependency**: `global` — resolves app creds + the login session from `~/.config/telegram/` (or a project `.env`); usable from any project once `telegram login` has run.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. The inline dependency (`telethon`) is resolved by `uv` on first run.
- **A Telegram app + an authorized session** (hard) — every command authenticates as a user account; the CLI is inert without `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` and a completed `telegram login`. The session is created once interactively (phone → code → optional 2FA) and reused thereafter.
- **A Deepgram API key** (soft) — only `telegram transcribe` needs `DEEPGRAM_API_KEY`; every other command runs without it.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/telegram/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/telegram` | `~/.capabilities/telegram/bin/telegram`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `telegram` resolves by name. |
| `stub.md` | `~/.capabilities/telegram/stub.md`, installed as `~/.claude/tools/telegram.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |
| `credentials.env.example` | copied to `~/.config/telegram/credentials.env` **with empty values**. |

The **session file** (default `~/.config/telegram/session.session`) is written by `telegram login` at first use, in the same user-config dir. It is auth material — it grants account access; never commit it.

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). The secret values are read by **key name** only and never travel on the command line; the one non-secret flag is `--session` (the session-file path).

| Key | Secret? | Notes |
|---|---|---|
| `TELEGRAM_API_ID` | **yes** | numeric app id from my.telegram.org/apps |
| `TELEGRAM_API_HASH` | **yes** | app hash paired with the id; identifies the app, not the account |
| `TELEGRAM_SESSION` | no | path to the Telethon session file (no extension). Optional — defaults to `~/.config/telegram/session`. Overridable per-invocation by `--session <PATH>`. |
| `DEEPGRAM_API_KEY` | **yes** | only `telegram transcribe` reads it |

The **secret in the strict sense is the session file**, not the keys: it persists the authorized login. The app id/hash gate which app connects; the session grants the account. The cascade resolves the keys and the session *path*; `telegram login` produces the session *file*.

## Project artifacts

The whole `project/` template copies into `.capabilities/<namespace>/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/<namespace>/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/<namespace>/identifiers.md` |
| `project/reference.md` | `.capabilities/<namespace>/reference.md` (self-describing scaffold; populated on demand) |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | discoverable | infer from the project (dir name / existing `.capabilities/` convention); confirm if unsure | the `.capabilities/<namespace>/` path |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | must-confirm | ask the user to create an app at my.telegram.org/apps and supply both | the credentials env file (global) or project `.env` |
| login session | must-confirm | run `telegram login` once interactively (phone → code → optional 2FA); it persists the session file | `~/.config/telegram/session.session` |
| `TELEGRAM_SESSION` | leave-breadcrumb | only when the session lives somewhere other than the default `~/.config/telegram/session` | the credentials env file / project `.env` |
| `DEEPGRAM_API_KEY` | leave-breadcrumb | only if the project uses `telegram transcribe` | the credentials env file / project `.env` |
| chat ids / handles | leave-breadcrumb | discovered live with `telegram chats` once the session resolves; pin the ones the project addresses repeatedly | `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `telegram`'s must-haves are **`TELEGRAM_API_ID` + `TELEGRAM_API_HASH` + a completed `telegram login`** — resolved at install. `DEEPGRAM_API_KEY` and any project-specific chat ids are breadcrumbs; none block install.

## Deviations

Recorded in this capability's dedicated [deviations.md](deviations.md): the MTProto-not-HTTP transport and the stateful session/login ceremony. Both realize the SHEBANG intent in MTProto's terms; an audit reads them there as choices, not drift.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `telegram help`).
- **No secret values in markdown** — `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `DEEPGRAM_API_KEY`, and the session file live in env / the user-config dir, never in `identifiers.md`; identifiers carry only non-secret chat ids/handles.
- **The operating model lives in `telegram help`** — the command surface, the credential cascade, the chat-reference forms (`@username` / `+phone` / id / title), the stateful startup protocol, the export JSON shape, the I/O envelope, and the exit codes are all project-agnostic and are not transcribed into the assets.
- **`identifiers.md` carries placeholders** — no real chat ids, usernames, or account handles baked into the public registry; those are discovered against the consuming project's own account at install. No secret can appear here.
- A **recurring read/export/transcribe flow** (which chats sync where, on what trigger) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
- **The two deviations in [deviations.md](deviations.md) are deliberate** — the MTProto-not-HTTP transport and the stateful session/login ceremony. The audit reads them there as choices, not drift.
