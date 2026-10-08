# askproject — change log

## 2026-10-08 — The store setting is read from the family's file

`askproject` now uses `capabilities-contract` 0.2.0, which reads the machine's store setting from `$XDG_CONFIG_HOME/agentkit/store.json`, the file `capabilities store set` now writes, and from the manager's former files while it is absent; `AGENTKIT_STORE_URL` overrides it ahead of `CAPABILITIES_STORE_URL`. A machine whose setting is in the former files behaves as before.

## 2026-10-08 — The session map lives in the machine's store

The map of which target continues which peer session now lives in the table `askproject_sessions` of the PostgreSQL the machine's store setting names, or the one `CAPABILITIES_STORE_URL` names, reached through the shared `capabilities-contract` library under its own migration ledger: one row per calling project, machine and target, so two calls at once no longer lose each other's update. The table starts empty: `capabilities/askproject/state/sessions.json` and a database-mode project's stored map are neither read nor written, so a `-c` after upgrading starts no earlier session. A caller is keyed by its project id, or by its root where it has none. With no store an ask and `targets` refuse with exit 6 `store_not_configured` and a hint naming `capabilities store set`, and exit 5 when the store cannot be reached; `doctor` reports the store and fails without one. A store that fails after the peer has answered costs the record, not the answer.
