---
name: asana
description: Asana CLI — drive Asana projects & tasks over the REST API (workspaces, projects, users, tasks/subtasks, comments with @mentions, tags, sections/board columns, dependencies, the API-only `external` machine-state field, attachments, and the deliverable request-pair). Run `asana help` for the full command surface before the first subcommand in a session.
---

Asana CLI — drive Asana projects & tasks over the REST API.

- Executable: `asana` (on `PATH`)
- Credentials: `~/.config/asana/credentials.env` (`ASANA_TOKEN` = an Asana Personal Access Token). Resolved by the standard cascade — project `.env(.local)` > user config > process env. Act as another identity with `--token-env <KEY>` (names the env/cred key holding an alternate PAT; the value never touches the command line).
- Load full reference: `asana help`

The free ("Personal") plan has no custom fields, no Search API, and no native Rules — the CLI works within that: tags + the API-only `external` field carry machine metadata, and section membership models status. Run `asana help` before issuing any subcommand the first time in a session. Project-scoped workspace / project / user / section gids live in each project's own identifiers, not here.
