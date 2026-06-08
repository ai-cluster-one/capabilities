# CallVA — identifiers

All fixed, **non-secret structural** values for this project's CallVA setup: agent / project / phone-number / automation IDs, and the key-name → account map. Pure lookup; the operating model and command surface are in `callva help`.

> Template note: fill these at install by discovering them against this project's own CallVA account — `callva projects list`, `callva agents list`, `callva phone-numbers list`, `callva automations list` (the key must resolve first; `callva doctor` confirms it). Leave the rows the project doesn't use as breadcrumbs. **No key values here** — only the name → account map; the secrets live in env (see the connection note below).

## Projects / agents

| Entity | ID |
|---|---|
| Project — `<name / role>` | `<uuid>` |
| Agent — `<name / role>` (e.g. the default agent) | `<uuid>` |

## Phone numbers

| Number / label | ID | Assigned to |
|---|---|---|
| `<+E.164 / label>` | `<uuid>` | `<agent / project>` |

## Automations

API-only Windmill scripts this project drives. The script bodies and their trigger logic are a **project routine** / reference, not here — only the IDs.

| Automation | ID | Triggered by |
|---|---|---|
| `<name / role>` | `<uuid>` | `<webhook / schedule / api>` |

## Key identities (values in env, never here)

The CLI resolves an API key by env-var name; `callva … --key-env <VAR>` selects which. Values live in env (project `.env` / the callva `credentials.env`) per the [credential cascade](../../DOCTRINE.md#the-credential-cascade) — only the **name → account** map is structural.

| Env var | Account | Use |
|---|---|---|
| `CALLVA_API_KEY` | `<the default account>` | default for every call |
| `<ALT>_API_KEY` | `<alternate account>` | act as that account (`--key-env <ALT>_API_KEY`) — only if the project drives more than one CallVA account |

Global tool: `callva` on `PATH` (`callva help` is the source of truth for the CLI surface).
