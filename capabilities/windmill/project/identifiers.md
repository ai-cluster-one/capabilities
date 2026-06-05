# Windmill — identifiers

All fixed values and paths for this project's Windmill setup. Pure lookup; the context that explains them is in [reference.md](reference.md), the authoring rules in [windmill-guide.md](windmill-guide.md).

> Template note: `<namespace>` and `<AGENT_IMAGE>` are filled at install; the SSH host is wired as a Windmill variable, not stored here.

## Connection

Connection config is **env, not repo**: instance URL, workspace, and token live in `~/.config/windmill/credentials.env` (keys `WINDMILL_URL`, `WINDMILL_WORKSPACE`, `WINDMILL_API_TOKEN`; flags > env > file), overridable per project in `.env` / `.env.local`. Not restated here by design. Instance edition/version comes from `windmill doctor`, not a pinned value.

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
