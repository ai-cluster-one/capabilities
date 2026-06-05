---
name: Windmill
description: The project's orchestration substrate — the cron scheduler, the runner, and the durable run history / logs / retries behind its autonomous operations, driven via the `windmill` CLI. Holds when jobs run and that they ran; the project's systems of record hold the truth.
---

# Windmill

The project's **orchestration substrate**: the scheduler (cron), the runner, and the durable run history / logs / retries behind its autonomous operations. Windmill holds *when* jobs run and *that they ran*; this repo holds *how* (the runbooks); the project's systems of record hold the truth.

> Template note: `<namespace>` is filled at install. Replace the role paragraph below with how *this* project actually uses Windmill, and keep the sibling links pointing at this folder's real files. Keep this file **lightweight** — role + pointers; the tool's command surface is `windmill help`, not here.

## Role in the network

Cron-triggered scripts dispatch work to an agent box over SSH → `docker exec`; each run is a first-class job record with logs. Runs on a (possibly shared) Windmill instance — this project is isolated to its own `f/<namespace>/` folder. The work it triggers lands in the project's real systems of record.

## Interaction

Via the `windmill` CLI on `PATH` — run `windmill help` first (the CLI surface is self-documenting). Connection comes from `~/.config/windmill/credentials.env`, overridable per project in `.env` / `.env.local`.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — all fixed values and paths: the `f/<namespace>/` folder, the agent-box SSH target, the folder-scoped variables, script/schedule paths.
- [reference.md](reference.md) — project-specific operational context: folder-as-isolation-boundary, dedicated worker-tag routing, the link-don't-copy rule for Windmill's own docs.
- [windmill-guide.md](windmill-guide.md) — **how to write a script**: the prefer-the-box's-CLIs principle, capability discovery, the SSH bridge, self-contained-per-file rule, deploy. Sources + examples in [scripts/](scripts/).
