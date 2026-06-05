---
name: Notion
description: The project's Notion access — publish markdown to its pages via the `notion` CLI (REST API): whoami, list child pages, fetch a page as markdown, replace an existing page's body+title, create under a parent, and upsert by exact title. The way this project writes its docs/reports into Notion.
---

# Notion

The project's **Notion access** — reading and publishing its pages through the `notion` CLI. Fetch a page as markdown, replace a page's body and title in place, create a page under a parent, or upsert by exact title. The local markdown's leading `# H1` is the source of truth for the page title, so one edit keeps file and page in sync.

> Template note: `<namespace>` and every page UUID fill at install. Replace this role paragraph with how *this* project actually uses Notion (which pages/parents it writes, what local docs map to them, whether publishing runs on a trigger). Keep this file **lightweight** — role + pointers; the command surface is `notion help`, not here.

## Interaction

Via the `notion` CLI on `PATH` — run `notion help` first (it is the self-documenting source of truth for the command surface, the credential cascade, the page-reference and markdown-input forms, the H1-as-title convention, and the PAT constraints), then `notion doctor` to confirm the token resolves. See [identifiers.md](identifiers.md) for the page UUIDs and token identities this project uses.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: the parent/page UUIDs it publishes to and the token-name → identity map.
- [reference.md](reference.md) — the standing home for project-specific operational context (what each page/tree means here, the upsert-vs-publish policy, title conventions). Ships empty as a self-describing scaffold; populated as context accrues.

> If this project runs an automated publishing flow (which local docs sync to which pages, on what trigger), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
