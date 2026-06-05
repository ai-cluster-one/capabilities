# Windmill — identifiers

All fixed values and paths for this project's Windmill setup. Pure lookup; the context that explains them — including the script-authoring model — is in [reference.md](reference.md).

> Template note: `<namespace>` and `<AGENT_IMAGE>` are filled at install; the SSH host is wired as a Windmill variable, not stored here.

## Connection & identity

Connection and identity config is **env, not repo** — referenced here by key name, not value:

| Key | Home | Purpose |
|---|---|---|
| `WINDMILL_URL` / `WINDMILL_WORKSPACE` / `WINDMILL_API_TOKEN` | user config / project `.env` | instance URL, workspace, admin token (flags > project env > user config > process env) |
| `WINDMILL_OPERATOR` (+ `WINDMILL_OPERATOR_NAME`) | user config / project `.env` | operator user the seat runs as (`--as`); owners derive from it |
| `WINDMILL_FOLDER` | project `.env` | the namespace `provision`/`verify` target (= `f/<namespace>/` below) |

Not restated by value here by design. Instance edition/version comes from `windmill doctor`, not a pinned value.

## Folder (the permission boundary)

| Key | Value |
|---|---|
| Project folder | `f/<namespace>/` |

Everything this project creates is `f/<namespace>/<name>` — scripts, variables, schedules. The folder is the access boundary: variables under it are only readable by scripts/users with folder access.

## Agent box (SSH target)

| Key | Value | Notes |
|---|---|---|
| Container image | `<AGENT_IMAGE>` | resolve container by `ancestor=<image>`, never by name |
| Box host | *(in the `agent_ssh_host` variable below)* | a connection value — kept in env/variable, not here |

## Windmill variables (folder-scoped)

The scripts read these at runtime (`wmill.getVariable`). They are wired by the consumer's **box-wiring routine**, not by `windmill provision` — the trio is the box↔Windmill connection (see [reference/provisioning.md](reference/provisioning.md)).

| Path | Secret? | Value / purpose |
|---|---|---|
| `f/<namespace>/agent_ssh_host` | no | SSH host of the agent box |
| `f/<namespace>/agent_ssh_user` | no | SSH user |
| `f/<namespace>/agent_ssh_key` | **yes** | private key into the box |

## Script & schedule paths

| Path | Role |
|---|---|
| `f/<namespace>/<name>` | a deployed script |
| `f/<namespace>/<name>_schedule` | a cron schedule attached to a script |

List what this project actually deploys here as it grows.
