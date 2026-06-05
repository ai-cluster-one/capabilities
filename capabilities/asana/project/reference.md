# Asana — reference

> **Purpose.** The standing home for *project-specific* operational context: prose about how **this project** uses Asana, where that's neither a value nor the command surface. What each board or section means here, how this project uses the `external` field, an @mention / escalation convention, the meaning behind a tag.
>
> Structural values (workspace/project/user/section/tag gids, the token-name → identity map) live in `identifiers.md`; how the CLI and Asana behave — the command surface, the credential cascade, the free-plan constraints (no custom fields / no Search API / no native Rules), the read-consistency rule (section-scoped reads are read-after-write consistent; the project task-list lags `tags`/`external`/`memberships`), the markdown→rich-text comment conversion, rate limits, and the no-auto-pagination caveat — lives in `asana help` (project-agnostic, never copied here).
>
> An *executable* task pipeline (sections-as-status, `external` dedup, escalation) is a **project routine** in `.routines/`, not this file; point to it from here, don't embed it.
>
> Ships empty on purpose, so the home is always labeled and no agent has to decide whether it should exist or what goes in it. Populate it only when real context accrues — an empty reference is conformant, not a gap. Replace this note with that content when it arrives.
