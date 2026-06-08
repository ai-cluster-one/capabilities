# Railway (railwayc) — identifiers

All fixed, **non-secret structural** values for this project's Railway setup. Pure lookup; the harness model is in `railwayc help`, the command surface in `railway help`.

> Template note: most Railway ids (environment, service, deployment, domain) are read live by `railwayc status --json` and need no record here. Fill only what this project refers to by hand; leave the rest as breadcrumbs. **No token here** — it lives in this project's `.env` (see the connection note below).

## Project & environment

The Railway project is whatever `RAILWAY_TOKEN` authorizes; a project token is pinned to one environment. Record how this project selects it — `railwayc doctor` prints the live name, environments, and services.

| Role | Value |
|---|---|
| Railway project this repo deploys | `<project name — also set as RAILWAYC_EXPECT_PROJECT>` |
| Environment the token is scoped to | `<production \| staging \| …>` |

## Services (optional breadcrumbs)

Service names are read live by `railwayc status`. List one only if this project refers to it by hand repeatedly (e.g. passes it as `-s <service>`).

| Role | Service name |
|---|---|
| `<e.g. the app service this repo deploys>` | `<service name>` |

## Token (value in env, never here)

The harness resolves the token from this project's `.env`(.local) per the reduced cascade `railwayc help` documents — only the structural fact that the project follows the token is recorded here.

| Env var | Holds | Use |
|---|---|---|
| `RAILWAY_TOKEN` | this project's Railway **project** token | every command; selects + scopes the project |
| `RAILWAYC_EXPECT_PROJECT` | the expected project (name or id) | optional — `railwayc doctor` asserts the token points here |

Global tool: `railwayc` on `PATH` (`railwayc help` for the harness; `railway help` for the command surface).
