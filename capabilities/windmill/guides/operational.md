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

## On Windmill's own feature docs — link, don't copy

For deep usage of a specific Windmill capability beyond what `windmill help` and these guides cover, **point to Windmill's official documentation and fetch it on demand** rather than transcribing it. Copied platform docs go stale silently; a fetched-live link doesn't. A consuming project records only what is *specific to it* — values, conventions, architecture.
