# whatsapp — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `whatsapp`
- **Summary**: read a WhatsApp account through a self-hosted **WAHA** (WhatsApp HTTP API) bridge — onboard / link a session via the GOWS phone-number pairing code (`pair` — pass a number, enter the code in WhatsApp, no QR), profile readiness check (`doctor`), instance/infrastructure readiness check (`health` — server up, engine in the expected mode, session linked with an account), human-readable session view (`status`), list chats, fetch and search a chat's messages, look up a contact, export a chat's full history to JSON (downloading voice/audio + photos/stickers, idempotently), fill voice/audio transcription placeholders via Deepgram, and render an exported JSON as a markdown log. Sending is gated on a `mode: "send"` profile and is a documented placeholder today.
- **Underlying service**: a **WAHA** instance (the WhatsApp HTTP API, self-hosted) over HTTP, authenticated by an `X-Api-Key` (and optional dashboard basic-auth). The link to WhatsApp is a WAHA **session**, paired out-of-band on the instance. The optional `transcribe` path additionally calls **Deepgram**'s HTTP API.
- **Has authored artifacts**: a config template (`project/whatsapp.json.example`) and a message-store `.gitignore` (`project/.gitignore`); no scripts.
- **Config dependency**: `global` — a single WAHA instance resolves from `~/.config/whatsapp/` (or a project `.env`), so reads are usable from any project once the connection keys (see Credentials) are set and the session is paired. A project-side `whatsapp.json` is the **expansion**: multi-identity profiles and a project-local message store (`messages_dir`).

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. The inline dependency (`httpx`) is resolved by `uv` on first run.
- **A reachable WAHA instance with a paired session** (hard) — every read authenticates against WAHA and addresses a session linked to a WhatsApp account. The CLI is inert without `WAHA_BASE_URL` + a key, and reads fail until the session is paired. `whatsapp pair` performs the linking from the CLI (GOWS phone-number pairing code: create/start the session, return an `XXXX-XXXX` code to enter in WhatsApp → Linked Devices); `whatsapp doctor` reports profile readiness, and `whatsapp health` reports the instance's infrastructure readiness (server up, engine/tier mode, session + account). All ride the **same `X-Api-Key`** — no extra credential for pairing or validation (the dashboard/swagger basic-auth gates UI surfaces only, not the JSON API).
- **The WAHA server's default engine set to GOWS** (hard, server-side) — the engine is a server env (`WHATSAPP_DEFAULT_ENGINE=GOWS`) captured by a session at creation, not a per-session field. The phone-number pairing code and deep history sync are GOWS properties; `whatsapp pair`/`health` **assert** the live engine but cannot set it — an instance on another engine is fixed on the server, then the session is recreated.
- **A Deepgram API key** (soft) — only `whatsapp transcribe` needs `DEEPGRAM_API_KEY`; every other command runs without it.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/whatsapp/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/whatsapp` | `~/.capabilities/whatsapp/bin/whatsapp`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `whatsapp` resolves by name. |
| `stub.md` | `~/.capabilities/whatsapp/stub.md`, installed as `~/.claude/tools/whatsapp.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |
| `credentials.env.example` | copied to `~/.config/whatsapp/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) in this capability's **profile-indirect** form — see [deviations.md](deviations.md). The secret is read by **key name** (the active profile's `secret_env`, or the single-instance `WAHA_WHATSAPP_API_KEY`) and never travels on the command line; there is no secret flag.

| Key | Secret? | Notes |
|---|---|---|
| `WAHA_BASE_URL` | no | the WAHA instance URL. Single-instance: this env key. Profile mode: `base_url` in `whatsapp.json`. A connection value — lives in env/config, never baked into the CLI. |
| `WAHA_WHATSAPP_API_KEY` | **yes** | the instance `X-Api-Key`. In profile mode the holding key is named by the profile's `secret_env` (this is the conventional name). |
| `WAHA_DASHBOARD_USERNAME` | no | optional — dashboard basic-auth user, if the instance fronts the API with it. |
| `WAHA_DASHBOARD_PASSWORD` | **yes** | optional — dashboard basic-auth password, paired with the username. |
| `DEEPGRAM_API_KEY` | **yes** | only `whatsapp transcribe` reads it. |

In **profile mode**, `whatsapp.json` (committed, non-secret wiring) maps each identity to its `base_url`, `session`, `mode`, optional `number`, and the `secret_env` *name*; the key's *value* lives in the project `.env`(`.local`) or `~/.config/whatsapp/credentials.env`. The **session itself** (the link to a WhatsApp account) is not a CLI credential — it is paired on the WAHA instance and persists server-side; `whatsapp doctor` reports whether it is linked.

## Project artifacts

The whole `project/` template copies into `.capabilities/whatsapp/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/whatsapp/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/whatsapp/identifiers.md` |
| `project/reference.md` | `.capabilities/whatsapp/reference.md` (self-describing scaffold; populated on demand) |
| `project/whatsapp.json.example` | copied to `.capabilities/whatsapp/whatsapp.json` and filled with the project's real profiles (non-secret wiring only) **only when profile mode is used**; the `.example` stays as the reference template. |
| `project/.gitignore` | `.capabilities/whatsapp/.gitignore` — keeps the synced message store (`messages/`) out of version control. |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | **pinned** — must be `whatsapp` | the CLI hardcodes the `.capabilities/whatsapp/whatsapp.json` discovery path, so this capability installs under namespace **`whatsapp`** (not a free name). A project that genuinely needs another namespace sets `$WHATSAPP_CONFIG` to the config path instead. | the `.capabilities/whatsapp/` path |
| `WAHA_BASE_URL` (or a profile's `base_url`) | must-confirm | ask the user for the WAHA instance URL | the credentials env file (global) or `whatsapp.json` (profile) |
| `WAHA_WHATSAPP_API_KEY` (the *value*) | must-confirm (secret) | ask the user for the instance `X-Api-Key`; never commit | the credentials env file / project `.env` |
| a paired session | must-confirm | pair once on the WAHA instance (dashboard → Linked Devices → pairing code or QR); persists server-side | the WAHA instance (not a file) |
| `WAHA_DASHBOARD_USERNAME` / `WAHA_DASHBOARD_PASSWORD` | leave-breadcrumb | only if the instance fronts its API with dashboard basic auth | the credentials env file / project `.env` |
| `DEEPGRAM_API_KEY` | leave-breadcrumb | only if the project uses `whatsapp transcribe` | the credentials env file / project `.env` |
| profiles (`whatsapp.json`) | leave-breadcrumb | only when the project needs multiple identities or a project-local message store; one entry per identity (`base_url`, `session`, `mode`, `secret_env`, optional `number`/`messages_dir`) | `.capabilities/whatsapp/whatsapp.json` |
| expected `engine` / `tier` (per profile) | leave-breadcrumb | the WAHA mode `whatsapp health` validates the live instance against; `engine` defaults to `GOWS`, `tier` is skipped unless pinned. Confirm against the live instance (`whatsapp health` reports the actual engine/tier) | the profile in `whatsapp.json` |
| chat ids / handles | leave-breadcrumb | discovered live with `whatsapp chats` once a session resolves; pin the ones the project addresses repeatedly | `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `whatsapp`'s must-haves are **`WAHA_BASE_URL` + an API key (or dashboard basic-auth) + a paired session** — resolved at install. `DEEPGRAM_API_KEY`, dashboard creds, profiles, and project-specific chat ids are breadcrumbs; none block install. `whatsapp doctor` validates the chain (creds resolve, WAHA reachable, session linked) once the must-haves are in place.

## Deviations

Recorded in this capability's dedicated [deviations.md](deviations.md): the **profile-indirect credential model** (the secret is named by the active profile's `secret_env`, connection values come from `whatsapp.json`, and the env-file load is no-override so process env wins — modeled on the mailbox capability, with a single-instance env fallback) and the **planned, mode-gated send** (a documented placeholder; `mode: "send"` structurally keeps a read identity from transmitting). Both realize the SHEBANG intent; an audit reads them there as choices, not drift.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `whatsapp help`).
- **No secret values in markdown** — the API key, dashboard password, and Deepgram key live in env / the user-config dir, never in `identifiers.md` or `whatsapp.json`. The shipped `whatsapp.json.example` carries only wiring with placeholder hosts (`waha.example.com`), the *name* of the secret env var, and a placeholder number — never a real instance, account, or key.
- **The operating model lives in `whatsapp help`** — the command surface, the credential cascade, the profile-discovery and `$WHATSAPP_CONFIG`/`$WHATSAPP_PROFILE` overrides, the chat-id forms, the export JSON shape and the default `messages_dir` store, the I/O envelope, and the exit codes are all project-agnostic and are not transcribed into the assets.
- **`identifiers.md` carries placeholders** — no real chat ids, numbers, or instance hosts baked into the public registry; those are discovered against the consuming project's own account at install. No secret can appear here.
- **The message store is data, not definition** — synced conversations and media land under `messages_dir` (default `messages/`) and are git-ignored by the shipped `project/.gitignore`; only the wiring and docs are committed.
- A **recurring read/export/transcribe flow** (which chats sync where, on what trigger) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
- **The two deviations in [deviations.md](deviations.md) are deliberate** — the profile-indirect credential model and the planned, mode-gated send. The audit reads them there as choices, not drift.
