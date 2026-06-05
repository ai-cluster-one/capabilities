# Directo — identifiers

All fixed, non-secret structural values for this project's Directo setup. Pure lookup; the operating model and command surface are in `directo help`.

> Template note: fill these at install from the project's real Directo database. Connection + secrets are **not** here — they live in env (next section).

## Connection

Connection config is **env, not repo**: login name, password, database, and the live session cookie live in `~/.config/directo/credentials.env` (keys `DIRECTO_USERNAME`, `DIRECTO_PASSWORD`, `DIRECTO_DB`, `DIRECTO_KOHT`, `DIRECTO_COOKIE`; flags > project `.env` > user config > process env), overridable per project in `.env` / `.env.local`. Not restated here by design.

## Database

| Key | Value |
|---|---|
| Database segment | `<db>` — the path segment in `login.directo.ee/<db>/` (held in `DIRECTO_DB`) |

## Locations (koht)

The locations this project's login offers, selected per session (`directo login --koht <code>`). One is the session default.

| Code | Label | Notes |
|---|---|---|
| `<code>` | `<label>` | fill each location this login presents (`directo kohts` lists the codes) |

## Read endpoints in use

The `*.asp` endpoints this project reads, relative to `login.directo.ee/<db>/`.

| Path | Action / purpose |
|---|---|
| `components_api.asp` | `action=<name>` — the web UI's JSON component API (e.g. `calendar_info`) |

List what this project actually reads here as it grows.
