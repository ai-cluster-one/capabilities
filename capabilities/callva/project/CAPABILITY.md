# CallVA

The project's **CallVA access** — reading and driving its voice-AI platform through the `callva` CLI. Voice agents and their prompts, call records and transcripts, recordings, call analytics, custom fields, webhook schedules, automations (Windmill scripts) and their runs, webhook logs, variables, settings, projects, phone numbers, and providers.

> Template note: `<namespace>` and every resource ID fill at install. Replace this role paragraph with how *this* project actually uses CallVA (which agents/projects, what it reads vs. writes, which automations it drives, how calls flow). Keep this file **lightweight** — role + pointers; the command surface is `callva help`, not here.

## Interaction

Via the `callva` CLI on `PATH` — run `callva help` first (the CLI surface is self-documenting), and `callva doctor` to confirm the key resolves here. It authenticates with a bearer API key resolved by the cascade (project `.env(.local)` > `~/.config/callva/credentials.env` > process env); `--key-env <KEY>` acts as a different CallVA account by naming the key that holds its value. See [identifiers.md](identifiers.md) for the IDs this project uses.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: agent / project / phone-number / automation IDs, and the key-name → account map.
- [reference.md](reference.md) — the standing home for project-specific operational context (what each agent is for here, how this project routes calls and automations, what custom fields mean). Ships empty as a self-describing scaffold; populated as context accrues.

> If this project runs an automated call-handling or automation pipeline over CallVA (what triggers which script, how transcripts feed downstream, escalation to a human), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
