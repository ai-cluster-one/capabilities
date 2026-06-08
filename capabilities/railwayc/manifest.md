# railwayc — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `railwayc`
- **Summary**: a credential harness around the official `railway` CLI. It resolves a project-scoped `RAILWAY_TOKEN`, injects it, and forwards the command verbatim to `railway` — `doctor` proves the token resolves and reports the project; `help` prints the contract; every other word passes straight through to `railway` with its stdout/stderr/exit code untouched. It owns auth and readiness, not the command surface (that stays `railway`'s, reached by `railway help`).
- **Underlying service**: **Railway** (the PaaS), driven through its own `railway` CLI — railwayc shells out to it; it does not call Railway's API directly. The CLI is a hard external dependency, not bundled.
- **Has authored artifacts**: no.
- **Config dependency**: `project-required` — the token is project-scoped, so railwayc is globally *aware* the moment it is installed but only *ready* in a project whose `.env` carries a `RAILWAY_TOKEN`. `railwayc doctor` reports the difference.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang; `uv` must be on `PATH`. It declares **no** inline dependencies (pure stdlib + `subprocess`).
- **the `railway` CLI** (hard, external) — railwayc forwards every command to `railway`, so `railway` must be on `PATH` (`brew install railway` or `npm i -g @railway/cli`). `doctor` and every forwarded command fail with exit 5 (`railway_not_found`) without it.
- **a Railway project token** (hard) — every command resolves `RAILWAY_TOKEN`; the CLI is inert without one (only `help` runs tokenless). See Credentials.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/railwayc/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/railwayc` | `~/.capabilities/railwayc/bin/railwayc`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `railwayc` resolves by name. |
| `stub.md` | `~/.capabilities/railwayc/stub.md`, installed as `~/.claude/tools/railwayc.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |

There is **no global credentials file**. Because the token is project-scoped, `credentials.env.example` is a snippet for a *consuming project's* `.env`, not a global config to populate — railwayc reads no `~/.config/railwayc/` tier.

## Credentials

Resolved by a reduced cascade — **project `.env(.local)` > process env** (first non-empty wins). The flag tier and the user-config tier are deliberately dropped (see [deviations.md](deviations.md)). The token never touches the command line.

| Key | Secret? | Notes |
|---|---|---|
| `RAILWAY_TOKEN` | **yes** | a Railway **project** token (project → Settings → Tokens), scoped to one project + one environment; lives in the consuming project's `.env`; never commit a value |
| `RAILWAYC_EXPECT_PROJECT` | no | optional — the project (name or id) this `.env`'s token is meant for; `doctor` asserts the match and exits 7 on mismatch |

Per-project by construction: each consuming project sets its own `RAILWAY_TOKEN` in its own `.env`, so each is scoped to only its own Railway project. There is no account-wide token and no fallback to the machine's account session.

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
| `RAILWAY_TOKEN` | must-confirm | ask the user for a Railway **project** token (project → Settings → Tokens) for the project this repo deploys | the consuming project's `.env` (never a global file) |
| `RAILWAYC_EXPECT_PROJECT` | discoverable | once the token works, `railwayc doctor` reports the project name — write it back as the readiness assertion | the consuming project's `.env` |

A capability is dysfunctional without its must-haves. `railwayc`'s only must-have is **`RAILWAY_TOKEN`** in the consuming project — resolved at install. The project's environments and services are read live by `railwayc status`; none are configured and none block install.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md), reading [deviations.md](deviations.md) first:

- **It is a passthrough harness, not a surface wrapper** — railwayc must never map, rename, or enumerate `railway`'s commands. The command surface is `railway help`; `railwayc help` documents only railwayc's own layer (auth, `doctor`, forwarding model) and points at `railway help`.
- **Transparent forwarding** — for any forwarded command, railwayc passes `railway`'s stdout/stderr and exit code through unchanged; the `_emit`/`_die` envelope and the exit-code taxonomy apply only to railwayc's own layer (token resolution, `doctor`, `railway`-not-found). Recorded in [deviations.md](deviations.md).
- **Scoping is enforced, not assumed** — railwayc requires `RAILWAY_TOKEN` and must not fall back to interactive login or the machine's account session; the refusal is the project-scoping guarantee.
- **No token in markdown** — `RAILWAY_TOKEN` lives in the consuming project's `.env`, never in `identifiers.md` or any committed file.
- **`doctor` uses `railway status --json`, not `whoami`** — a project token cannot query the user/`me` object; health is the project-status round-trip. Exit code 7 (wrong project) is railwayc's one domain addition and is named in `railwayc help`.
- **`identifiers.md` carries placeholders** — no real project/environment/service ids or names baked into the public registry; the project is selected by the token in env.
- **Deploy/ops orchestration is the consumer's** (DOCTRINE rule 9) — which services this project deploys, on what trigger, how a redeploy is sequenced is a **project routine** ([ROUTINES.md](../../ROUTINES.md)), not part of this capability; the assets may point to it but must not embed it.
