# whatsapp — change log

## 2026-10-09 — One stuck session on the store no longer stops it

`whatsapp` now uses `capabilities-contract` 0.2.1. Opening the store takes no lock once its tables are in place, so a session left holding the store's schema lock no longer hangs every command and service doctor behind it. Where a lock is still needed, it waits at most 10 seconds and then reports `store_busy`; a process killed while waiting leaves no session queued behind it, and a session that holds the lock without working is ended by the store within 5 seconds.

## 2026-10-08 — The store setting is read from the family's file

`whatsapp` now uses `capabilities-contract` 0.2.0, which reads the machine's store setting from `$XDG_CONFIG_HOME/agentkit/store.json`, the file `capabilities store set` now writes, and from the manager's former files while it is absent; `AGENTKIT_STORE_URL` overrides it ahead of `CAPABILITIES_STORE_URL`. A machine whose setting is in the former files behaves as before.
