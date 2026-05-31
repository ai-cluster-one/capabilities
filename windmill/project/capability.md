# Windmill

The project's **orchestration substrate**: the scheduler (cron), the runner, and the durable run history / logs / retries behind its autonomous operations. Windmill holds *when* jobs run and *that they ran*; this repo holds *how* (the runbooks); the project's systems of record hold the truth.

> Template note: `<namespace>` is filled at install. Replace the role paragraph below with how *this* project actually uses Windmill, and point the sibling links at this project's real files. Keep this file **lightweight** — role + pointers; the tool's command surface is `windmill help`, not here.

## Role in the network

Cron-triggered scripts dispatch work to an agent box over SSH → `docker exec`; each run is a first-class job record with logs. Runs on a (possibly shared) Windmill instance — this project is isolated to its own `f/<namespace>/` folder. It is not a source of truth; the work it triggers lands in the project's real systems.

## Interaction

Via the `~/bin/windmill` global tool — run `windmill help` first (the CLI surface is self-documenting). Connection comes from `~/.config/windmill/credentials.env`, overridable per project in `.env` / `.env.local`.

## Operational context (load on demand)

- [.assets/<namespace>/identifiers.md](../../../.assets/<namespace>/identifiers.md) — all fixed values and paths: the `f/<namespace>/` folder, the agent-box SSH target, the folder-scoped variables, script/schedule paths.
- [.assets/<namespace>/reference.md](../../../.assets/<namespace>/reference.md) — project-specific operational context: folder-as-isolation-boundary, dedicated worker-tag routing, the link-don't-copy rule for Windmill's own docs.
- [.assets/<namespace>/windmill-guide.md](../../../.assets/<namespace>/windmill-guide.md) — **how to write a script**: the prefer-the-box's-CLIs principle, capability discovery, the SSH bridge, self-contained-per-file rule, deploy. Sources + examples in [.assets/<namespace>/scripts/](../../../.assets/<namespace>/scripts/).
