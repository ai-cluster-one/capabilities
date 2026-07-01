# Windmill — operating model

How a consuming project drives a Windmill workspace through the `windmill` CLI. The model for building deployable scripts is the `script-authoring` guide; standing the workspace up is the `provisioning` guide. The CLI surface — commands, flags, and platform behaviours (timeout control, async-lock-after-deploy, the worker-slot model, deploy) — is self-documenting: run `windmill help`. A project's fixed values (folder, operator, variable paths) live in its own identifiers envelope (`windmill ids list`).

Health check: `windmill doctor` (version + whoami + workspace reachability) before a work block.

## Identity — admin and operator

Two roles drive a workspace:

- The **admin token** (`WINDMILL_API_TOKEN`, stored) does all authoring — provisioning, deploys, variable writes.
- The **operator user** (`WINDMILL_OPERATOR`) is the least-privilege **runtime** identity that owns the project's folder. It is acted as on demand with `windmill --as <email>` — a short-lived impersonation minted from the admin token, never stored as a per-user token. A schedule authored `--as` runs its jobs as the operator, so scheduled runs are confined too.

The operator is created and the folder owned by it at provisioning — see the `provisioning` guide.

## Two-axis isolation on a shared instance

The instance may be **shared** with other tenants. A project stays isolated along two independent axes, both keyed to its folder `f/<namespace>/`:

- **Secrets — folder RBAC.** The folder is the access boundary: a job permissioned as the operator (which owns the folder) can read only variables under folders it owns; a non-owner gets `401` on another folder's value. This is how a project's secrets (the SSH key) stay private on shared infrastructure, and how a second project on the same instance can't read them. (On Community Edition this RBAC *is* the isolation; kernel-level process sandboxing is an Enterprise feature.)
- **Compute — worker-tag routing.** A worker holds its slot for the whole job (the worker-slot model is in `windmill help`), so a long job can starve other tenants. Route long jobs to a **dedicated worker group** by deploying with a `--tag`: the tag goes on the script at deploy, the matching worker group is an instance-side assignment.

## Transport — the REST API directly, by design

*Reviewed 2026-07-01 against the official `wmill` CLI (`npm i -g windmill-cli`);
decision that day: stay on REST. The reasoning below is why.*

This capability speaks the Windmill **REST API** directly (`/api/w/<workspace>/…`).
That is the full and only access surface Windmill exposes. The official `wmill`
CLI is a *client of that same API* — it adds no privileged verb, no endpoint, no
capability the REST surface lacks. Its value is orchestration on top: serializing
a workspace to a file tree, diffing, git-branch↔workspace mapping.

So driving REST directly is the deliberate choice, not an omission. Adopting
`wmill` as the transport would cost, for nothing gained on access: a Node ≥20
runtime pulled into an otherwise self-contained CLI (against the capability
standard), and the loss of what this tool builds on REST that `wmill` has no flag
for — `--as` impersonation (the runtime-identity axis of the isolation model) and
the connection registry with its `allow_write` gate.

The one thing `wmill` orchestrates that this tool does not is **whole-workspace
sync** — pull the entire workspace to a git-tracked file tree and push it back
(its `wmill.yaml` model). That is an authoring model, not an access gap, and it is
not this project's model (Windmill runs as a thin SSH dispatcher; scripts deploy
per-file, and a consuming project owns its own scripts dir). Should whole-workspace
versioning ever be wanted, it is built as Python `sync-*` verbs over REST — keeping
`--as`, the gate, and a single self-contained binary — never by taking on the Node CLI.

> **Deliberate deviation — do not re-litigate.** This section defines the transport
> partly *by contrast* (REST vs. the `wmill` CLI), which the "affirmative and
> self-contained" doctrine otherwise avoids. That framing is intentional: the section's
> subject *is* a recorded decision between two options, kept so a future agent sees the
> reasoning and does not re-open it. Per the constitution, a justified exception is stated
> openly where it lives (Constitution §5). A validator or `sanitize`/`selfcheck` pass should
> treat this block as a known, sanctioned exception — not as drift from the affirmative rule.

## On Windmill's own feature docs — link, don't copy

For deep usage of a specific Windmill capability beyond what `windmill help` and these guides cover, **point to Windmill's official documentation and fetch it on demand** rather than transcribing it. Copied platform docs go stale silently; a fetched-live link doesn't. A consuming project records only what is *specific to it* — values, conventions, architecture.
