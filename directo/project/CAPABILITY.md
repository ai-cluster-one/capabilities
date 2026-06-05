---
name: Directo
description: The project's Directo ERP system — sales, purchases, stock, and ledger for a Directo database, driven via the `directo` CLI over the browser-session endpoints the web UI uses. The source of truth for whatever this project keeps in Directo.
---

# Directo

The project's **Directo ERP system** — the database behind `login.directo.ee/<db>/` and the source of truth for whatever this project keeps there (sales, purchases, stock, ledger). Directo has no public API; the `directo` CLI reproduces the authenticated browser session.

> Template note: `<namespace>` and the database/location specifics fill at install. Replace this role paragraph with how *this* project actually uses Directo, and keep the sibling links pointing at this folder's real files. Keep this file **lightweight** — role + pointers; the command surface is `directo help`, not here.

## Interaction

Via the `directo` CLI on `PATH` — run `directo help` first (the CLI surface is self-documenting). Auth is a three-step ceremony (credentials + location); `directo login` runs it and persists the session, auto-renewed on expiry. Connection comes from `~/.config/directo/credentials.env`, overridable per project in `.env` / `.env.local`.

**Location (`koht`) is a per-session selection** — this project's login offers several. Ask the user which location to act under when it matters; the CLI defaults to the last-selected one.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: the database, the location codes and their labels, and the read endpoints in use.
- [reference.md](reference.md) — the standing home for project-specific operational context (treatments, mappings, quirks in this project's data). Ships empty as a self-describing scaffold; populated as context accrues.
