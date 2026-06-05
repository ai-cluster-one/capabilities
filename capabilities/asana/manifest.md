# asana — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability. Section set follows the manifest schema established by `windmill/manifest.md`.

## Identity

- **Name**: `asana`
- **Summary**: drive Asana projects & tasks over the REST API — workspaces, projects, users, tasks/subtasks, comments with @mentions, tags, sections (board columns), dependencies, the API-only `external` machine-state field, attachments, and the atomic deliverable request-pair.
- **Underlying service**: **Asana** (the SaaS), over its public REST API authenticated by a Personal Access Token. Not bundled — the user supplies an Asana account and PAT.
- **Has authored artifacts**: no.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. Inline script dependencies are resolved by `uv` on first run.
- **An Asana PAT** (hard) — every call authenticates with a token; the CLI is inert without one. See Credentials.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/asana/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/asana` | `~/.capabilities/asana/bin/asana`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `asana` resolves by name. |
| `stub.md` | `~/.capabilities/asana/stub.md`, surfaced by symlinking it as `~/.claude/skills/asana/SKILL.md` — front-matter `name` + `description` load every session, body on demand. |
| `credentials.env.example` | copied to `~/.config/asana/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). The CLI reads the token by **key name** — `--token-env <KEY>` selects which key holds the PAT (default `ASANA_TOKEN`); the value never touches the command line.

| Key | Secret? | Notes |
|---|---|---|
| `ASANA_TOKEN` | **yes** | the default identity's Personal Access Token; never commit a value |
| `<ALT>_TOKEN` | **yes** | optional alternate-identity PAT, named on the call via `--token-env`; one per identity the project acts as |

Per-project override: a consuming project may set `ASANA_TOKEN` (or alternate keys) in its own `.env` / `.env.local` to act as a different identity than the global default. The **name → identity** map is structural and lives in the project's `identifiers.md`; only the token values live in env.

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
| `ASANA_TOKEN` | must-confirm | ask the user for a PAT (mint at app.asana.com → My Apps) | the credentials env file (global) or project `.env` |
| workspace / project / user gids | discoverable / leave-breadcrumb | `asana workspaces`, `asana projects`, `asana users` once the token resolves; fill the ones this project uses, breadcrumb the rest | `project/identifiers.md` |
| section gids | leave-breadcrumb | `asana sections --project <P>` when the project models status as board columns | `project/identifiers.md` |
| tag gids | leave-breadcrumb | `asana tags` when the project uses tags for status/provenance | `project/identifiers.md` |
| `<ALT>_TOKEN` | leave-breadcrumb | only if the project posts as more than one identity | the credentials env file / project `.env`; mapped in `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `asana`'s only must-have is **`ASANA_TOKEN`** — resolved at install. Every gid is discovered against the live workspace once the token works, so they are filled when the project actually uses them; none block install.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `asana help`).
- **No token values in markdown** — `ASANA_TOKEN` and any alternate-identity PATs live in env, not `identifiers.md`; identifiers carry only the **name → identity** map, never a value.
- **Platform model lives in `asana help`** — the command surface, the cascade, the free-plan constraints (no custom fields / no Search API / no native Rules), the read-consistency rule (section-scoped reads are reliable; the project task-list lags `tags`/`external`/`memberships`), the markdown→rich-text comment conversion, rate limits, and the no-auto-pagination caveat are all project-agnostic and are not transcribed into the assets.
- **`identifiers.md` carries placeholders** — no real workspace/project/user/section gids baked into the public registry; those are discovered against the consuming project's own Asana workspace at install. No secret can appear here.
- A task **pipeline / state model** (sections-as-status, the `external` dedup convention, escalation) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
