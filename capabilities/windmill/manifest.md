# windmill — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability. This is the first manifest authored, so it doubles as the **schema** every other capability's manifest follows: the section set below is the contract.

## Identity

- **Name**: `windmill`
- **Summary**: drive a Windmill instance (deploy scripts, attach cron schedules, run jobs, read run history/logs, manage secret variables and folders) over its REST API.
- **Underlying service**: a Windmill instance (Community or EE). Not bundled — the user supplies one.
- **Has authored artifacts**: yes — example scripts under `project/scripts/`.

## Dependencies

- **agent box** (soft) — the example scripts and the `script-guide` assume an "agent box" reachable over SSH that exposes other CLIs via `docker exec`. Windmill the *tool* works without it; the *script pattern* needs it. Not auto-installed; note it to the user.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/windmill/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/windmill` | `~/.capabilities/windmill/bin/windmill`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `windmill` resolves by name. |
| `stub.md` | `~/.capabilities/windmill/stub.md`, surfaced by symlinking it as `~/.claude/skills/windmill/SKILL.md` — front-matter `name` + `description` load every session, body on demand. |
| `credentials.env.example` | copied to `~/.config/windmill/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). All **must-confirm** — Windmill has no standard public endpoint, so the instance must be named by the user; none are guessable.

| Key | Secret? | Notes |
|---|---|---|
| `WINDMILL_URL` | no | instance base URL |
| `WINDMILL_WORKSPACE` | no | workspace id |
| `WINDMILL_API_TOKEN` | **yes** | API token; never commit a value |

Per-project override: a consuming project may set these in its own `.env` / `.env.local` to point at a different instance/workspace than the global default.

## Project artifacts

The whole `project/` template copies into `.capabilities/<namespace>/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/<namespace>/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/<namespace>/identifiers.md` |
| `project/reference.md` | `.capabilities/<namespace>/reference.md` |
| `project/windmill-guide.md` | `.capabilities/<namespace>/windmill-guide.md` |
| `project/scripts/` | `.capabilities/<namespace>/scripts/` (example scripts; adapt) |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | discoverable | infer from the project (dir name / existing `.capabilities/` convention); confirm if unsure | the Windmill folder `f/<namespace>/`, the `.capabilities/<namespace>/` path, every script |
| `WINDMILL_URL` / `WINDMILL_WORKSPACE` / `WINDMILL_API_TOKEN` | must-confirm | ask the user | the credentials env file (global) or project `.env` |
| `<AGENT_IMAGE>` | must-confirm (if using the SSH/box scripts) | ask the user; else leave-breadcrumb | the example scripts' container filter |
| `f/<namespace>/agent_ssh_*` | leave-breadcrumb | created as Windmill variables when the box leg is wired | the example scripts |

A capability is dysfunctional without its must-haves: here, the three `WINDMILL_*` keys. Resolve those at install; the box-related variables become breadcrumbs until the user wires the agent box.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of Windmill's command surface (that's `windmill help`).
- **No connection values in markdown** — `WINDMILL_URL`/workspace/token live in env, not `identifiers.md`.
- **Platform gotchas live in `windmill help`**, not transcribed into the assets (timeout control, async-lock-after-deploy, worker slot model, no-PUT, scalar bare strings).
- The example scripts carry **placeholders, not a real namespace / image / gids**.
