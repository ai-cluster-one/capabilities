# windmill — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability. This is the first manifest authored, so it doubles as the **schema** every other capability's manifest follows: the section set below is the contract.

## Identity

- **Name**: `windmill`
- **Summary**: drive a Windmill instance (deploy scripts, attach cron schedules, run jobs, read run history/logs, manage secret variables and folders) over its REST API.
- **Underlying service**: a Windmill instance (Community or EE). Not bundled — the user supplies one.
- **Has authored artifacts**: yes — example scripts under `project/scripts/`.

## Dependencies

- **agent box** (soft) — the example scripts and the script authoring reference assume an "agent box" reachable over SSH that exposes other CLIs via `docker exec`. Windmill the *tool* works without it; the *script pattern* needs it. Not auto-installed; note it to the user.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/windmill/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/windmill` | `~/.capabilities/windmill/bin/windmill`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `windmill` resolves by name. |
| `stub.md` | `~/.capabilities/windmill/stub.md`, surfaced by symlinking it as `~/.claude/skills/windmill/SKILL.md` — front-matter `name` + `description` load every session, body on demand. |
| `credentials.env.example` | copied to `~/.config/windmill/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). The three connection keys are **must-confirm** — Windmill has no standard public endpoint, so the instance must be named by the user; none are guessable.

| Key | Secret? | Notes |
|---|---|---|
| `WINDMILL_URL` | no | instance base URL |
| `WINDMILL_WORKSPACE` | no | workspace id |
| `WINDMILL_API_TOKEN` | **yes** | admin API token; never commit a value |
| `WINDMILL_OPERATOR` | no | operator user email — the least-privilege identity `provision` ensures and the seat runs as (`--as`); owners derive from it |
| `WINDMILL_OPERATOR_NAME` | no | operator display name (optional) |

Per-project override: a consuming project may set these in its own `.env` / `.env.local` to point at a different instance/workspace than the global default. `WINDMILL_FOLDER` (the namespace `provision` targets) is **per-project** — it lives in the project `.env`, not the global config.

## Project artifacts

The whole `project/` template copies into `.capabilities/<namespace>/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/<namespace>/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/<namespace>/identifiers.md` |
| `project/reference.md` | `.capabilities/<namespace>/reference.md` (thin index over `reference/`) |
| `project/reference/` | `.capabilities/<namespace>/reference/` (operational context + script authoring) |
| `project/scripts/` | `.capabilities/<namespace>/scripts/` (example scripts; adapt) |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | discoverable | infer from the project (dir name / existing `.capabilities/` convention); confirm if unsure | the Windmill folder `f/<namespace>/`, `WINDMILL_FOLDER` in the project `.env`, the `.capabilities/<namespace>/` path, every script |
| `WINDMILL_URL` / `WINDMILL_WORKSPACE` / `WINDMILL_API_TOKEN` | must-confirm | ask the user | the credentials env file (global) or project `.env` |
| `WINDMILL_OPERATOR` (+ `WINDMILL_OPERATOR_NAME`) | must-confirm | ask the user | the credentials env file (global) or project `.env` — read by `provision`/`verify` |
| `<AGENT_IMAGE>` | must-confirm (if using the SSH/box scripts) | ask the user; else leave-breadcrumb | the example scripts' container filter |
| `f/<namespace>/agent_ssh_*` | leave-breadcrumb | minted/`var-set` by the consumer's **box-wiring routine** when the box leg is wired — **not** by `windmill provision` | the box-wiring routine; the scripts read them at runtime |

A capability is dysfunctional without its must-haves: here, the three connection `WINDMILL_*` keys plus `WINDMILL_OPERATOR`. Resolve those at install; the box-related variables become breadcrumbs until the user wires the agent box. The SSH var trio represents the box↔Windmill connection, so it is the box-wiring routine's domain (DOCTRINE rule 9), outside `provision`'s skeleton.

## Post-install

Schema slot (optional): a capability that needs a one-time setup action after its files land declares it here, and the installer offers to run it ([INSTALL.md](../../INSTALL.md) step 4). A capability with nothing to do omits the section.

For windmill — stand up the workspace skeleton once the project assets are in place and the must-confirm keys are filled:

```
windmill provision --scripts-dir .capabilities/<namespace>/scripts   # folder + operator + scripts
windmill verify    --scripts-dir .capabilities/<namespace>/scripts   # ready: true/false
```

Idempotent and non-destructive — safe to re-run. `provision` reads `WINDMILL_FOLDER`/`WINDMILL_OPERATOR` from config (override with `--folder`/`--operator`). **Caveat:** full readiness also needs the SSH var trio, minted later by the consumer's box-wiring routine once the agent box exists; until then the skeleton is provisioned but the box leg is unwired — expected, not a failure.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of Windmill's command surface (that's `windmill help`).
- **No connection values in markdown** — `WINDMILL_URL`/workspace/token/operator live in env, not `identifiers.md`; identifiers may point to them by key name.
- **Platform gotchas live in `windmill help`**, not transcribed into the assets (timeout control, async-lock-after-deploy, worker slot model, no-PUT, scalar bare strings).
- **Provisioning is config-driven and file-less** — no committed provisioning/instance JSON. `provision`/`verify` build the workspace skeleton from config (`WINDMILL_FOLDER`/`OPERATOR`) + the scripts dir; the SSH var trio is **not** provisioned here. A committed manifest is a recordable deviation, not the default.
- The example scripts carry **placeholders, not a real namespace / image / gids**.
