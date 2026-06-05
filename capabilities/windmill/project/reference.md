# Windmill — operational reference

Project-specific operational context for driving Windmill via the `windmill` CLI. Role + routing live in the capability file ([CAPABILITY.md](CAPABILITY.md)). Fixed values and paths are in [identifiers.md](identifiers.md); how to write scripts is in [windmill-guide.md](windmill-guide.md).

The CLI surface (commands, flags, platform behaviours, gotchas) is **self-documenting** — run `windmill help`. Don't restate it here; this file holds only what's specific to this project.

Health check: `windmill doctor` (version + whoami + workspace reachability) before a work block.

## Folder = the isolation boundary

The instance may be **shared**. This project is isolated to its own folder, `f/<namespace>/`, and that folder is the access boundary: variables and scripts under it are only reachable by principals with folder access. This is how the project's secrets (the SSH key) stay private on shared infrastructure. Create the folder once with `windmill folder-create <namespace> --owner u/<user>`.

## Dedicated worker-tag routing

A worker runs **one job at a time for its whole duration**, so a multi-minute job ties up a slot. On a shared instance, that can starve other tenants' jobs (and vice versa). Route long jobs to a **dedicated worker group** by deploying with a `--tag`. The tag goes on the script at deploy; the matching worker group is an instance-side assignment.

## On Windmill's own feature docs — link, don't copy

For deep usage of a specific Windmill capability beyond what `windmill help` and these files cover, **point to Windmill's official documentation and fetch it on demand** rather than transcribing it here. Copied platform docs go stale silently; a fetched-live link doesn't. Capture in the repo only what is *specific to this project* — values, conventions, architecture.
