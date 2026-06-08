# Railway (railwayc)

The project's **Railway access** — controlling and inspecting its own Railway services through the `railwayc` harness, which injects this project's project-scoped token and forwards to the official `railway` CLI. Scope is enforced by the token: railwayc reaches only the one Railway project this repo's `RAILWAY_TOKEN` was minted for, never the whole account.

> Template note: `<namespace>` fills at install; the Railway project is whatever `RAILWAY_TOKEN` (in this project's `.env`) is scoped to — set in env, not here. Replace this role paragraph with how *this* project uses Railway (which project/environment, which services it watches or deploys). Keep this file **lightweight** — role + pointers; the railwayc layer is `railwayc help` and the command surface is `railway help`, not here.

## Interaction

Via the `railwayc` CLI on `PATH`. Run `railwayc help` first (the harness contract — the credential model, `doctor`, and the forward-everything-else rule), then `railway help` for the actual command surface. `railwayc doctor` confirms the token resolves and reports the project, environments, and services. Everything else (`railwayc status --json`, `railwayc logs -s …`, `railwayc variables …`, `railwayc redeploy -s …`) forwards straight to `railway`. See [identifiers.md](identifiers.md) for the project this token binds to and the services this repo refers to by hand.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: the Railway project/environment this token is scoped to, and the service names this repo references.
- [reference.md](reference.md) — the standing home for project-specific operational context (which environment is which, how this project's services relate, any deploy conventions). Ships empty as a self-describing scaffold; populated as context accrues.

> An *executable* deploy or release flow (which services to redeploy, in what order, on what trigger) is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
