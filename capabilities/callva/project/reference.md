# CallVA — reference

> **Purpose.** The standing home for *project-specific* operational context: prose about how **this project** uses CallVA, where that's neither a value nor the command surface. What each agent is for here, how this project routes calls and triggers automations, what a custom field means, how transcripts feed downstream work.
>
> Structural values (agent / project / phone-number / automation IDs, the key-name → account map) live in `identifiers.md`; how the CLI and the platform behave — the command surface, the credential cascade, JSON-by-default + `--full` slimming, the `-f key=value` filter convention, and the exit-code taxonomy — lives in `callva help` (project-agnostic, never copied here).
>
> An *executable* call-handling or automation pipeline (what triggers which script, how transcripts feed downstream, escalation to a human) is a **project routine** in `.routines/`, not this file; point to it from here, don't embed it.
>
> Ships empty on purpose, so the home is always labeled and no agent has to decide whether it should exist or what goes in it. Populate it only when real context accrues — an empty reference is conformant, not a gap. Replace this note with that content when it arrives.
