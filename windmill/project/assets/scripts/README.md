# Example scripts

These are **adaptable examples**, not production scripts — they teach the one pattern this capability is about: *Windmill as a thin SSH dispatcher*. Windmill holds only the SSH key; the **agent box** holds every credential and exposes the real capabilities as CLIs. A script SSHes into the box and `docker exec`s a tool. It never reimplements a capability (no raw HTTP, no SDKs, no service tokens).

Adapt points in every example (the installer fills these from the manifest's template variables):

- `<AGENT_IMAGE>` — the box's docker image; the container is resolved by `ancestor=<image>`, never by name (a PaaS renames it on redeploy).
- `f/<namespace>/agent_ssh_host` · `…_user` · `…_key` — the three folder-scoped Windmill variables holding the SSH target (the key is secret).
- the prompt / tool / timeout — what *your* job actually does.

## The examples

- [example_box_command.ts](example_box_command.ts) — the minimal skeleton. One SSH connection, one box CLI command, parsed output. Every script copies this shape.
- [example_claude_job.ts](example_claude_job.ts) — the interesting one: drive **headless Claude on the box** (prompt on stdin, JSON out, hard budget cap, resumable session). This is how Windmill schedules autonomous LLM work without holding any model credential itself.

## Beyond these — the autonomous loop

Compose the primitives into a **populate → dispatch → run** triad: a cron *populator* enqueues work (e.g. as tasks on a board), a *runner* claims and dispatches each, and a *worker* runs **one bounded step** per task (a worker holds its slot for the whole job, and Windmill hard-kills at `--timeout`, so never poll-and-wait inside one). Build it from the two examples above.

## Deploy

```
windmill deploy f/<namespace>/<name> scripts/<name>.ts --timeout <secs> [--tag <worker-group>]
```

Each script is self-contained — Windmill has no cross-script imports, so the SSH helpers are duplicated per file by design. See [../<name>-guide.md](../windmill-guide.md) for the full authoring rules.
