# Notion — reference

> **Purpose.** The standing home for *project-specific* operational context: prose about how **this project** uses Notion, where that's neither a value nor the command surface. What each page or tree means here, the upsert-vs-publish policy, a title convention, which local docs map to which pages.
>
> Structural values (page/parent UUIDs, the token-name → identity map) live in `identifiers.md`; how the CLI and Notion behave — the command surface, the credential cascade, the H1-as-title source-of-truth convention, the page-reference forms (UUID or URL), the markdown stdin/file/`-` contract, the I/O envelope + exit codes, and the PAT constraints (no `POST /v1/search` so no workspace-wide name lookup; `upsert` matches direct children only; access is per shared page) — lives in `notion help` (project-agnostic, never copied here).
>
> An *executable* publishing flow (which docs sync to which pages, on what trigger) is a **project routine** in `.routines/`, not this file; point to it from here, don't embed it.
>
> Ships empty on purpose, so the home is always labeled and no agent has to decide whether it should exist or what goes in it. Populate it only when real context accrues — an empty reference is conformant, not a gap. Replace this note with that content when it arrives.
