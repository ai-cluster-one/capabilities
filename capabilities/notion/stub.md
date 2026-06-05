---
name: notion
description: Notion CLI — publish markdown to Notion pages over the REST API (whoami, list child pages, fetch a page as markdown, replace an existing page's body+title, create a page under a parent, upsert by exact title). The local markdown H1 is the source of truth for the page title. Run `notion help` for the full command surface before the first subcommand in a session.
---

Notion CLI — publish markdown to Notion pages over the REST API.

- Executable: `notion` (on `PATH`)
- Credentials: `NOTION_TOKEN` = a Notion internal-integration token (PAT), in `~/.config/notion/credentials.env` or a project `.env`. Act as another identity with `--token-env <KEY>`.
- Load full reference: `notion help` — the command surface, the credential cascade, the page-reference and markdown-input forms, the H1-as-title convention, and the PAT constraints all live there.

Run `notion help` before issuing any subcommand the first time in a session, and `notion doctor` to confirm the token resolves. Project-scoped page / parent UUIDs live in each project's own identifiers, not here.
