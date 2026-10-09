# whatsapp — change log

## 2026-10-09 — The capture follows the project's database

Breaking. `whatsapp` now uses `capabilities-contract` 0.4.0 and keeps its capture in the database the project resolves: the `AGENTKIT_DB_*` keys in the project's `.env.local` and `.env`, then the process environment, then the machine's store setting; `capabilities store show` shows which answers. `CAPABILITIES_STORE_URL` and `CAPABILITIES_STORE_PASSWORD` are no longer read, and the service's deploy descriptor no longer declares them: a deployed node takes `AGENTKIT_DB_*` from its server's environment. The assistant service resolves its database once at launch and keeps it for its whole run, so a rebuilt session, the job runner, a reconnect and the outbox listener all stay on the database it started on. A project that names no database, with none in the environment or on the machine, is refused with exit 6 `store_not_configured` as before. `service/store.py` is no longer shipped; nothing read it.

## 2026-10-09 — `whatsapp migrate` brings its tables up to date when asked

`whatsapp migrate` applies the pending steps to its tables in the machine's store, and `whatsapp migrate status` reports where they stand without a lock and changes nothing. The manager runs `whatsapp migrate` when it installs or updates whatsapp, and the service runs it when it starts; every other call takes no lock and migrates only when it finds its tables missing or older than its code. `whatsapp` now uses `capabilities-contract` 0.3.0: a store that does not answer a connect within 10 seconds is reported as `store_unreachable`, and both ends of a connection send TCP keepalives, so the store drops a client that died without closing.

## 2026-10-09 — One stuck session on the store no longer stops it

`whatsapp` now uses `capabilities-contract` 0.2.1. Opening the store takes no lock once its tables are in place, so a session left holding the store's schema lock no longer hangs every command and service doctor behind it. Where a lock is still needed, it waits at most 10 seconds and then reports `store_busy`; a process killed while waiting leaves no session queued behind it, and a session that holds the lock without working is ended by the store within 5 seconds.

## 2026-10-08 — The store setting is read from the family's file

`whatsapp` now uses `capabilities-contract` 0.2.0, which reads the machine's store setting from `$XDG_CONFIG_HOME/agentkit/store.json`, the file `capabilities store set` now writes, and from the manager's former files while it is absent; `AGENTKIT_STORE_URL` overrides it ahead of `CAPABILITIES_STORE_URL`. A machine whose setting is in the former files behaves as before.
