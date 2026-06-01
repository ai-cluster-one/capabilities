# Windmill script-authoring guide (`f/<namespace>/*`)

How to write a deployable Windmill script for this project. Read before authoring one. Connection values and paths referenced here live in [identifiers.md](identifiers.md); platform mechanics common to any project are in `windmill help` (run it — don't memorise the surface).

## Core principle — prefer the box's CLIs over reimplementation

A script holds its own orchestration logic freely. The one rule: **don't re-implement a capability the agent box already exposes.** The box runs the project's capabilities as CLIs. When a script needs one of them, invoke that CLI **over SSH → `docker exec`** rather than building a fresh client (raw HTTP / SDK / service token) for the same thing. The box holds every service credential; Windmill holds exactly one secret — the SSH key.

**Discover capabilities at authoring time — never hardcode the list here.** The set of CLIs on the box grows; a list baked into this guide goes stale. To find what's available, ssh in and inspect, then read the tool's own contract:

```
ssh -i <key> <user>@<host> "docker exec -i $(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1) ls /usr/local/bin"
ssh -i <key> <user>@<host> "docker exec -i $(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1) <tool> help"
```

Whatever the box can do, the script consumes through the box's CLI.

## The SSH bridge

Every script connects with the `ssh2` library, reading three folder-scoped variables — `agent_ssh_host`, `agent_ssh_user`, `agent_ssh_key` (see [identifiers.md](identifiers.md)) — then `docker exec -i`s into the container.

- **`box(inner)`** wraps a tool call as `docker exec -i $(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1) <inner>`. Resolve the container **by image**, never by name — a PaaS renames it on every redeploy.
- **`q(s)`** single-quotes a value for the host `sh -c` layer. But anything with spaces, quotes, or newlines — notes, comment bodies, an LLM prompt, JSON blobs — goes over **stdin**, never inline. Two shell layers (ssh + `sh -c`) make inline quoting a footgun.
- **No `-t`.** A TTY corrupts JSON output. Read stdout as data.

## Conventions

- **Self-contained per file.** Windmill has no cross-script imports — the SSH/quoting/`box` helpers are duplicated in each script by design. Don't factor them into a shared module.
- **Stable non-secret ids are constants at the top.** They aren't secrets, and a script can't read this repo's reference files at runtime.
- **Secrets only via Windmill variables.** Read at runtime with `await wmill.getVariable("f/<namespace>/<name>")`. Never inline a key in source.
- **Bounded step, deliberate timeout.** A worker runs one job to completion with no mid-execution yielding; pick `--timeout` at deploy to fit the job's real worst case (it hard-kills overruns). Never poll-and-wait inside a job.
- **Language is `bun`** by default — match what the script is written for at deploy.

## Deploy

```
windmill deploy f/<namespace>/<name> .capabilities/<namespace>/scripts/<name>.ts --timeout <secs> [--tag <wg>]
```

Re-deploy archives the prior hash and creates the new version at the same path (no PUT). A freshly-deployed script isn't runnable until its dependency lock resolves — poll `windmill script-get <path>` until `lock` is non-null, then run. (Both behaviours are in `windmill help`.)

## Examples

Start from [scripts/example_box_command.ts](scripts/example_box_command.ts) (the minimal skeleton) and [scripts/example_claude_job.ts](scripts/example_claude_job.ts) (driving headless Claude on the box). Compose them into a **populate → dispatch → run** triad for an autonomous loop — see [scripts/README.md](scripts/README.md).
