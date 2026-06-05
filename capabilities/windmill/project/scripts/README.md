# Example scripts

These are **adaptable examples**, not production scripts — they demonstrate the one pattern this capability is about: *Windmill as a thin SSH dispatcher*. The full model — why it works this way and the conventions to follow — is the script authoring reference ([../reference.md](../reference.md)); these files show it in code.

Adapt points in every example (the installer fills these from the manifest's template variables):

- `<AGENT_IMAGE>` — the box's docker image; the container is resolved by image, never by name.
- `f/<namespace>/agent_ssh_host` · `…_user` · `…_key` — the three folder-scoped Windmill variables holding the SSH target (the key is secret).
- the prompt / tool / timeout — what *your* job actually does.

## The examples

- [example_box_command.ts](example_box_command.ts) — the minimal skeleton. One SSH connection, one box CLI command, parsed output. Every script copies this shape.
- [example_claude_job.ts](example_claude_job.ts) — the interesting one: drive **headless Claude on the box** (prompt on stdin, JSON out, hard budget cap, resumable session). This is how Windmill schedules autonomous LLM work without holding any model credential itself.

## Beyond these — the autonomous loop

Compose the primitives into a **populate → dispatch → run** triad: a cron *populator* enqueues work (e.g. as tasks on a board), a *runner* claims and dispatches each, and a *worker* runs **one bounded step** per task (a worker holds its slot for the whole job, and Windmill hard-kills at `--timeout`, so never poll-and-wait inside one). Build it from the two examples above.

## Deploy

Deploy with `windmill deploy` — run `windmill help` for the flags. Each script is self-contained: Windmill has no cross-script imports, so the SSH helpers are duplicated per file by design. The full script authoring model is in [../reference.md](../reference.md).
