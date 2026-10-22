# askproject — change log

## 2026-10-22 — A peer resolves its target's own project

A peer is started without the caller's handed-down `CAPABILITIES_PROJECT_ID` and `CAPABILITIES_PROJECT_ID_ROOT`, as it already was without the envelope pair, so a target bound to no id resolves as itself rather than as the caller's project. Declares `capabilities-contract>=0.12,<1`.

## 2026-10-21 — A connection whose network flow died fails in seconds

`askproject` uses `capabilities-contract` 0.7.2. Its store connections give a network flow up once data sent on it has gone unacknowledged for 20 seconds (`tcp_user_timeout`), which keepalives cannot notice since none is sent while data waits, and send keepalives after 15 seconds idle, every 5 seconds, 3 probes: a connection whose flow stopped carrying packets without a FIN or a reset fails in about 20 seconds as a lost connection instead of waiting until the store ends the session, and a plain read that loses it that way runs once more on a new connection. A connection lost where `askproject` catches nothing ends the command as `store_unreachable`, exit 5, as before.

## 2026-10-20 — A read is a transaction of its own in one round trip

`askproject` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-18 — One working connection per process

`askproject` uses `capabilities-contract` 0.5.1. Every connection it opens on the transaction path is the process's one working connection, which the library lends to each operation and keeps until the process exits, so a command connects once rather than once per read; a connect that fails is tried again, and a working connection the store or the network lost is replaced, a statement that lost it before anything of its transaction reached the store running once more on the new one and a commit never sent twice.

## 2026-10-18 — Connections go to the store's transaction address

`askproject` uses `capabilities-contract` 0.5.0. Its session map and its commands connect on transaction connections, at the store's transaction address where one is named. With one address everything reaches it as before.

## 2026-10-16 — A moved agent's profiles are read from the store

For an agent whose records moved into the store, a profile name `<name>` resolves to its own harness profile `askproject-<name>` at the agent's scope first, then the machine folder and the shipped set; `profiles` lists only those, by the name asked for, with `source: store`, and `profiles show` prints its text. Harness profiles other capabilities keep at the agent's scope are neither listed nor resolved, as the folder held only askproject's. No profiles folder is read for it. Every other project reads its folder as before.

## 2026-10-16 — `migrate files` moves the profiles folder into the store for `capabilities migrate`

Its mover saves each profile `<name>` in the project's `askproject/profiles/` folder into the store as the harness profile `askproject-<name>` at the project's scope, all of them or none, refusing one the store holds differently, and leaves the files for the manager to archive and remove. The harness runner is pinned at 0.9.0, the release that keeps profiles in a store.

## 2026-10-13 — A caller's sessions are kept under the node's declared name

The host a caller's peer sessions are recorded under is the node's name as the contract gives it: the value of `AGENT_NODE_NAME` where the machine declares one, and its host name as before where it declares none. A container node that declares its name resumes the sessions it recorded before a redeploy instead of starting new ones; a store-mode grant naming the node is matched against the same name.

## 2026-10-11 — A store behind Supabase's transaction pooler is refused at connect

`askproject` now uses `capabilities-contract` 0.4.1. A transaction-mode pooler keeps no session between transactions, so the schema binding, prepared statements and notifications a connection relies on are lost through one, and it is not supported. A database setting that points at Supabase's transaction pooler - a host under `pooler.supabase.com` on port 6543 - is refused before anything is sent to it, as `transaction_pooler`, with a hint naming the session pooler: the same host on port 5432. Every other database, the session pooler included, is reached as before.

## 2026-10-09 — The session map follows the calling project's database

Breaking. `askproject` now uses `capabilities-contract` 0.4.0 and keeps its session map in the database the calling project resolves: the `AGENTKIT_DB_*` keys in the project's `.env.local` and `.env`, then the process environment, then the machine's store setting; `capabilities store show` shows which answers. `CAPABILITIES_STORE_URL` is no longer read. One ask reads and records its session on the same database even when the peer runs for an hour. `doctor`'s `store` is now the setting in force with its secrets redacted - `level`, `sources`, `schema` and where the database is - in place of the label `CAPABILITIES_STORE_URL` or `setting`.

## 2026-10-09 — `askproject migrate` brings the session map's tables up to date when asked

`askproject migrate` applies the pending steps to the session map's tables in the machine's store, and `askproject migrate status` reports where they stand without a lock and changes nothing. The manager runs `askproject migrate` when it installs or updates askproject; every other call takes no lock and migrates only when it finds its tables missing or older than its code. `askproject` now uses `capabilities-contract` 0.3.0: a store that does not answer a connect within 10 seconds is reported as `store_unreachable`, and both ends of a connection send TCP keepalives, so the store drops a client that died without closing.

## 2026-10-09 — One stuck session on the store no longer stops it

`askproject` now uses `capabilities-contract` 0.2.1. Opening the store takes no lock once its tables are in place, so a session left holding the store's schema lock no longer hangs every command and service doctor behind it. Where a lock is still needed, it waits at most 10 seconds and then reports `store_busy`; a process killed while waiting leaves no session queued behind it, and a session that holds the lock without working is ended by the store within 5 seconds.

## 2026-10-08 — The store setting is read from the family's file

`askproject` now uses `capabilities-contract` 0.2.0, which reads the machine's store setting from `$XDG_CONFIG_HOME/agentkit/store.json`, the file `capabilities store set` now writes, and from the manager's former files while it is absent; `AGENTKIT_STORE_URL` overrides it ahead of `CAPABILITIES_STORE_URL`. A machine whose setting is in the former files behaves as before.

## 2026-10-08 — The session map lives in the machine's store

The map of which target continues which peer session now lives in the table `askproject_sessions` of the PostgreSQL the machine's store setting names, or the one `CAPABILITIES_STORE_URL` names, reached through the shared `capabilities-contract` library under its own migration ledger: one row per calling project, machine and target, so two calls at once no longer lose each other's update. The table starts empty: `capabilities/askproject/state/sessions.json` and a database-mode project's stored map are neither read nor written, so a `-c` after upgrading starts no earlier session. A caller is keyed by its project id, or by its root where it has none. With no store an ask and `targets` refuse with exit 6 `store_not_configured` and a hint naming `capabilities store set`, and exit 5 when the store cannot be reached; `doctor` reports the store and fails without one. A store that fails after the peer has answered costs the record, not the answer.
