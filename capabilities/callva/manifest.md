# callva — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `callva`
- **Summary**: drive the CallVA voice-AI platform over its External API — voice agents, assets (prompts), call records, transcripts, recordings, call analytics, custom fields and field groups, webhook schedules, automations (Windmill scripts) and their runs, webhook logs, project variables, settings, projects, phone numbers, and providers.
- **Underlying service**: **CallVA** (the voice-AI SaaS), over its External REST API authenticated by a bearer API key. Not bundled — the user supplies a CallVA account and API key.
- **Has authored artifacts**: no.
- **Config dependency**: `global` — resolves an API key from `~/.config/callva/` (or a project `.env`); usable from any project once installed. Project identifiers only pin the agent / phone-number / automation / project IDs that project drives.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. Inline script dependencies are resolved by `uv` on first run.
- **A CallVA API key** (hard) — every call authenticates with a bearer key; the CLI is inert without one. See Credentials.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/callva/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/callva` | `~/.capabilities/callva/bin/callva`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `callva` resolves by name. |
| `stub.md` | `~/.capabilities/callva/stub.md`, installed as `~/.claude/tools/callva.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |
| `credentials.env.example` | copied to `~/.config/callva/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). The CLI reads the key by **name** — `--key-env <KEY>` selects which key holds the API key (default `CALLVA_API_KEY`); the value never touches the command line.

| Key | Secret? | Notes |
|---|---|---|
| `CALLVA_API_KEY` | **yes** | the default identity's bearer API key; never commit a value |
| `CALLVA_API_URL` | no | optional base-URL override; defaults to `https://api.callva.one/api/v1` |
| `<ALT>_API_KEY` | **yes** | optional alternate-account key, named on the call via `--key-env`; one per account the project acts as |

Per-project override: a consuming project may set `CALLVA_API_KEY` (or alternate keys) in its own `.env` / `.env.local` to act as a different account than the global default. The **name → account** map is structural and lives in the project's `identifiers.md`; only the key values live in env.

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
| `CALLVA_API_KEY` | must-confirm | ask the user for a CallVA API key (mint in the CallVA dashboard) | the credentials env file (global) or project `.env` |
| `CALLVA_API_URL` | leave-breadcrumb | only if the project targets a staging / self-hosted deployment; otherwise the default holds | the credentials env file / project `.env` |
| agent / project / phone-number / automation IDs | discoverable / leave-breadcrumb | `callva agents list`, `callva projects list`, `callva phone-numbers list`, `callva automations list` once the key resolves; fill the ones this project uses, breadcrumb the rest | `project/identifiers.md` |
| `<ALT>_API_KEY` | leave-breadcrumb | only if the project acts as more than one CallVA account | the credentials env file / project `.env`; mapped in `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `callva`'s only must-have is **`CALLVA_API_KEY`** — resolved at install. Every resource ID is discovered against the live account once the key works, so they are filled when the project actually uses them; none block install.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `callva help`).
- **No key values in markdown** — `CALLVA_API_KEY` and any alternate-account keys live in env, not `identifiers.md`; identifiers carry only the **name → account** map, never a value.
- **Platform model lives in `callva help`** — the command surface, the cascade, JSON-by-default + `--full` slimming, the `-f key=value` filter convention, the exit-code taxonomy, and the per-resource flags are all project-agnostic and are not transcribed into the assets.
- **`identifiers.md` carries placeholders** — no real agent / project / phone-number / automation IDs baked into the public registry; those are discovered against the consuming project's own CallVA account at install. No secret can appear here.
- A **call-handling pipeline / automation workflow** (what triggers which automation, how transcripts feed downstream, escalation) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
