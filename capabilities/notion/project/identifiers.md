# Notion — identifiers

All fixed, **non-secret structural** values for this project's Notion setup: the page/parent UUIDs it publishes to and the token-name → identity map. Pure lookup; the operating model and command surface are in `notion help`.

> Template note: fill these at install by discovering them against this project's own Notion workspace. PATs can't search workspace-wide, so start from a known root URL and walk down with `notion children <parent>` (the token must resolve first). Leave the rows the project doesn't use as breadcrumbs. **No token values here** — only the name → identity map; the secrets live in env (see the connection note below).

## Pages / parents

A `<page>`/`<parent>` is a bare UUID (dashed or 32-char) or a full Notion URL. Record the durable role each one plays for this project.

| Role | UUID / URL |
|---|---|
| Parent — `<where new pages are created>` | `<uuid>` |
| Page — `<the page this project keeps in sync>` | `<uuid>` |

## Token identities (values in env, never here)

The CLI resolves a PAT by env-var name; `notion … --token-env <VAR>` selects which. Values live in env (project `.env` / the notion `credentials.env`) per the credential cascade `notion help` documents — only the **name → identity** map is structural.

| Env var | Identity | Use |
|---|---|---|
| `NOTION_TOKEN` | `<the integration / default identity>` | default for every call |
| `<ALT>_TOKEN` | `<alternate integration / workspace>` | publish **as** that integration (`--token-env <ALT>_TOKEN`) — only if the project acts as more than one identity |

> Access is per-page: each integration above must be **shared into** every page/parent it appears against, or calls fail with `restricted_resource`.

Global tool: `notion` on `PATH` (`notion help` is the source of truth for the CLI surface).
