# Windmill

The project's **orchestration substrate**: the scheduler (cron), the runner, and the durable run history / logs / retries behind its autonomous operations. Windmill holds *when* jobs run and *that they ran*; this repo holds *how* (the runbooks); the project's systems of record hold the truth.

> Template note: `<namespace>` is filled at install. Replace the role paragraph below with how *this* project actually uses Windmill, and keep the sibling links pointing at this folder's real files. Keep this file **lightweight** — role + pointers; the tool's command surface is `windmill help`, not here.

## Role in the network

Cron-triggered scripts dispatch work to an agent box over SSH → `docker exec`; each run is a first-class job record with logs. Runs on a (possibly shared) Windmill instance — this project is isolated to its own `f/<namespace>/` folder. The work it triggers lands in the project's real systems of record.

## Interaction

Via the `windmill` CLI on `PATH` — run `windmill help` first (the CLI surface is self-documenting). Connection config lives in env — see the identifiers below.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — all fixed values and paths: the connection env keys, the `f/<namespace>/` folder, the agent-box SSH target, the folder-scoped variables, script/schedule paths.
- [reference.md](reference.md) — project-specific operational context (folder-as-isolation-boundary, worker-tag routing, link-don't-copy) **and the script-authoring model**: prefer-the-box's-CLIs, capability discovery, the SSH bridge, conventions. Sources + examples in [scripts/](scripts/).
