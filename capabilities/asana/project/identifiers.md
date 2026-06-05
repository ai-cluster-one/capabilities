# Asana — identifiers

All fixed, **non-secret structural** values for this project's Asana setup: gids, the token-name → identity map, board sections, tags. Pure lookup; the operating model and command surface are in `asana help`.

> Template note: fill these at install by discovering them against this project's own Asana workspace — `asana workspaces`, `asana projects`, `asana users`, `asana sections --project <P>`, `asana tags` (the token must resolve first). Leave the rows the project doesn't use as breadcrumbs. **No token values here** — only the name → identity map; the secrets live in env (see the connection note below).

## Workspace / projects / users

| Entity | gid |
|---|---|
| Workspace — `<name>` | `<gid>` |
| Project — `<board name / role>` | `<gid>` |
| User — `<name / role>` (e.g. default assignee) | `<gid>` |

## Token identities (values in env, never here)

The CLI resolves a PAT by env-var name; `asana … --token-env <VAR>` selects which. Values live in env (project `.env` / the asana `credentials.env`) per the [credential cascade](../../DOCTRINE.md#the-credential-cascade) — only the **name → identity** map is structural.

| Env var | Identity | Use |
|---|---|---|
| `ASANA_TOKEN` | `<the seat / default identity>` | default for every call |
| `<ALT>_TOKEN` | `<alternate identity>` | post a comment **authored as** that user (`--token-env <ALT>_TOKEN`) — only if the project acts as more than one identity |

## Sections (board columns = status)

Status is section membership. If this project models a pipeline, the **state model is a project routine** (`.routines/…`) — here, only the gids.

| Project (board) | Section | gid |
|---|---|---|
| `<board>` | `<column name>` | `<gid>` |

## Tags

Free, server-queryable status/provenance labels (`GET /tasks?tag=<gid>`). Fill the ones this project uses.

| Tag | gid | Means |
|---|---|---|
| `<label>` | `<gid>` | `<what it marks>` |

Global tool: `asana` on `PATH` (`asana help` is the source of truth for the CLI surface).
