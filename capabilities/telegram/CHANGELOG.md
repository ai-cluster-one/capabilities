# telegram — change log

## 2026-10-11 — A store behind Supabase's transaction pooler is refused at connect

`telegram` now uses `capabilities-contract` 0.4.1. A transaction-mode pooler keeps no session between transactions, so the schema binding, prepared statements and notifications a connection relies on are lost through one, and it is not supported. A database setting that points at Supabase's transaction pooler - a host under `pooler.supabase.com` on port 6543 - is refused before anything is sent to it, as `transaction_pooler`, with a hint naming the session pooler: the same host on port 5432. Every other database, the session pooler included, is reached as before.

## 2026-10-11 — The service reopens the job register's store connection after losing it

When the store or the network ends the job register's database connection while the Telegram session stays up, the service's next job tick opens a new connection and the queue keeps draining, rather than retrying the dead connection every tick until the Telegram session ends or the service restarts. The log says `jobs: lost the store connection, reopening it on the next tick` for it, and a session that loses its connection to Telegram is logged as `session lost its connection to Telegram` rather than as likely protocol drift.

## 2026-10-09 — The job register follows the project's database, and the service reads its settings from files

Breaking. `telegram` now uses `capabilities-contract` 0.4.0 and keeps its job register in the database the project resolves: the `AGENTKIT_DB_*` keys in the project's `.env.local` and `.env`, then the process environment, then the machine's store setting; `capabilities store show` shows which answers. `CAPABILITIES_STORE_URL` is no longer read, and the service's deploy descriptor no longer declares it: a deployed node takes `AGENTKIT_DB_*` from its server's environment. The daemon resolves the database for its own project once at launch and keeps it. `service doctor`'s `store` reports `level` and `sources` - which level of the cascade answered and the files or variables it came from - in place of `source`. The daemon reads its settings and documents straight from the project's `capabilities/telegram/` and then the config home's, with the same values as before, and `service/store.py` is no longer shipped.

## 2026-10-09 — `telegram migrate` brings the job register's tables up to date when asked

`telegram migrate` applies the pending steps to the job register's tables in the machine's store, and `telegram migrate status` reports where they stand without a lock and changes nothing. The manager runs `telegram migrate` when it installs or updates telegram, and the service runs it when it starts; every other call takes no lock and migrates only when it finds its tables missing or older than its code. `telegram` now uses `capabilities-contract` 0.3.0: a store that does not answer a connect within 10 seconds is reported as `store_unreachable`, and both ends of a connection send TCP keepalives, so the store drops a client that died without closing.

## 2026-10-09 — One stuck session on the store no longer stops it

`telegram` now uses `capabilities-contract` 0.2.1. Opening the store takes no lock once its tables are in place, so a session left holding the store's schema lock no longer hangs every command and service doctor behind it. Where a lock is still needed, it waits at most 10 seconds and then reports `store_busy`; a process killed while waiting leaves no session queued behind it, and a session that holds the lock without working is ended by the store within 5 seconds.

## 2026-10-08 — The store setting is read from the family's file

`telegram` now uses `capabilities-contract` 0.2.0, which reads the machine's store setting from `$XDG_CONFIG_HOME/agentkit/store.json`, the file `capabilities store set` now writes, and from the manager's former files while it is absent; `AGENTKIT_STORE_URL` overrides it ahead of `CAPABILITIES_STORE_URL`. A machine whose setting is in the former files behaves as before.
