---
name: directo
description: Directo ERP CLI — drive a Directo instance (login.directo.ee) over the browser-session endpoints its web UI uses. Auth is a three-step ceremony (credentials + location), auto-renewed on expiry. Run `directo help` for the full command surface before the first subcommand in a session.
---

Directo ERP CLI — drive a Directo instance over the browser-session endpoints its web UI uses (Directo has no public API).

- Executable: `directo` (on `PATH`)
- Credentials: `~/.config/directo/credentials.env` (`DIRECTO_USERNAME`, `DIRECTO_PASSWORD`, `DIRECTO_DB`, `DIRECTO_KOHT`, `DIRECTO_COOKIE`; also honored as env vars and `--username` / `--password` / `--db` / `--koht` flags — flags > project `.env` > user config > process env).
- Load full reference: `directo help`

Auth is a three-step ceremony (GET login → POST credentials → POST location); `directo login` runs it and persists the session, which auto-renews on expiry. Location (`koht`) is a per-session selection — ask the user which one when it matters. Run `directo help` before issuing any subcommand the first time in a session. Project-scoped database, locations, and entity taxonomy live in each project's own assets, not here.
