# whatsapp — change log

## 2026-10-27 — One WhatsApp account is reached through one connection

`whatsapp connection add` and `whatsapp pair` refuse a connection whose `number` or `expected_account_id` (or the number `pair` is given or asked for) is an account another connection already links on this machine, before anything is written, with exit 6 `number_linked`; the error carries `account` and `holder`, `{connection, owner}`, where `owner` is the project whose own connection it is and null for one the machine shares, and its message and hint say to ask for access to that connection. The account's home records the connection that linked it in `linked-by.json`, written when `pair` links and, for a home linked before this release, by the first connection that opens it. `pair` on the connection that linked the account still answers `already_paired`. **For consumers:** two connection names for one account are no longer supported; a project that reaches a shared account under a name of its own keeps reading it, but cannot add or pair such a name again, and moves to a grant on the connection that linked the account.

## 2026-10-27 — `service harness` says which harness sign-ins the service's turns need

`whatsapp service harness [--json]` prints `{harness, profiles, unresolved}`: `harness` is the sorted, distinct harness tools (`claude`, `codex`) of every profile a turn is configured to run on now, and `profiles` lists each such profile with its harness and where it is named - `settings` (each position the settings reach, the defaults included, as `service doctor` reads them), `chats` (each chat whose `/set profile` names it) and `jobs` (each open job: draft, waiting, running, or stopped by a spent quota, which the runner resumes; a job naming no profile runs on the job default). The chats and jobs are those of this project and its environment. Each name is resolved and fitted as the listener resolves it, with no turn run; one that does not resolve or fit is listed under `unresolved` with why, and needs nothing. Settings that admit nobody run no dialogue and no job, so the answer is empty with `dialogue` false. It reads the store on one read-only connection, migrates nothing and writes nothing; a table the store does not hold yet holds no chats or jobs. A project that resolves no store answers an empty list with `store` `not_configured`. The manifest's `service` declares `sign_ins`, `{"harness": ["whatsapp", "service", "harness", "--json"]}`, and its `verbs` gain `harness`. Help says so.

## 2026-10-27 — `whatsapp connections` says whether each connection's session is on this machine

Every connection `whatsapp connections` reports carries `session`, `{"live": <bool or null>, "account": <text or null>}`, read from local files alone. For an in-house connection `live` is true when the engine's session in the account's home holds a linked device and the service's health there does not record it `logged_out`; the home is found as the other verbs find it, without creating or adopting one. `account` is the account the device is linked as, else the identity the connection records, else its `expected_account_id`. A waha connection's session lives on the WAHA server, not this machine, so its `live` is null with `reason` `waha_server`, and nothing is asked of the server. The manifest's `connections` block declares `device_bound: true`: a connection is a device linked to the account, not a value that moves to another machine.

## 2026-10-26 — The service follows queued messages by their version, and holds no listening connection

A queued message raises its account's version of the `whatsapp_outbox` channel in the store's change versions, in the same transaction as its row (store step `0022-outbox-versions`, schema 1.4), through a trigger on `whatsapp_messages`, so a row written by any release moves it. The same trigger sends the `whatsapp_outbox` notification, so a listener of an earlier release on another machine still wakes as before; `send` no longer sends it itself. The assistant service no longer listens: it polls the account's version on its own store connection at most once a second and wakes the sender when it moved, so a row queued by another process goes out within about a second, with the half-second poll behind it, and the service holds no session connection for it. A row queued in the service's own process still wakes the sender at once.

## 2026-10-25 — The Deepgram key is kept once for the machine

The setup field `DEEPGRAM_API_KEY` is declared `shared`, so `whatsapp setup` lists it under `shared`, apart from `fields`, and `whatsapp setup set` writes it to `~/.config/capabilities/shared.env`, where telegram reads the same value. `transcribe`, the assistant service's voice notes and the connections report ask that file before `~/.config/whatsapp/credentials.env`, so a key already kept there still works; a project's `.env.local`/`.env` still wins. `whatsapp setup unset DEEPGRAM_API_KEY` removes it from both files. The script declares `capabilities-contract>=0.22,<1`.

## 2026-10-24 — Turns run on callva-harness-runner 0.10.1

The runner is pinned at 0.10.1 in place of 0.9.0. Claude turns run through claude-agent-sdk 0.2.174, and a claude CLI up to 2.1.310 runs without the runner's out-of-range warning; codex is unchanged. The runner's 0.10.0 additions are new profile knobs only, so a turn runs as before.

## 2026-10-23 — The service declares how many nodes run it at once

The service descriptor declares `service.placement` `exclusive`: one node runs the service at a time, the deployment runner's lease holds it there, and `capabilities place whatsapp --node <node>` moves it, leaving `whatsapp` usable on the node it left.

## 2026-10-22 — A connection is linked by QR before it names an account, and a first link that fails leaves nothing

`whatsapp pair --qr` links an in-house connection that has neither `number` nor `expected_account_id`, where it refused as `no_account_identity` before the QR was shown: it pairs in a home named for the connection, keys the store by the account once the pairing names it, writes that account to the connection as `expected_account_id` (`identity_bound` in the result) at the moment the phone links, with the account's name already leading to the pairing home so every other verb finds the session during the history burst, and moves the home under the account once the engine lets go of it. `pair --events` on such a connection asks `phone_needed` for a pairing code. Under `--events`, a connection's first link - it binds no account and has no state home - that ends in a refusal, an error, closed input or a cancellation (SIGTERM, SIGINT, SIGHUP, refused as `cancelled`, exit 6) removes the connection through `connection remove` and the home it made; a connection that was ever linked is never touched, and without `--events` the verb is unchanged.

## 2026-10-22 — A project without project.json runs the service

`service init` accepts an envelope of a project bound to its id. Declares `capabilities-contract>=0.12,<1`.

## 2026-10-22 — A reconnect no longer kills the assistant service later

Each session the assistant service opens gives the engine a client of its own, where every session on one device used to share one; a closed session's client, once collected, then stopped the live one and the next send or account lookup ended the process with a nil-pointer panic in `main.GetMe`, losing the message it was sending. An account lookup on a session that is already closed is refused as `not_connected`, exit 5, instead of reaching the engine.

## 2026-10-21 — A connection whose network flow died fails in seconds

`whatsapp` uses `capabilities-contract` 0.7.2. Its store connections give a network flow up once data sent on it has gone unacknowledged for 20 seconds (`tcp_user_timeout`), which keepalives cannot notice since none is sent while data waits, and send keepalives after 15 seconds idle, every 5 seconds, 3 probes: a connection whose flow stopped carrying packets without a FIN or a reset fails in about 20 seconds as a lost connection instead of waiting until the store ends the session, and a plain read that loses it that way runs once more on a new connection. A connection lost where `whatsapp` catches nothing ends the command as `store_unreachable`, exit 5, as before.

## 2026-10-20 — A read is a transaction of its own in one round trip

`whatsapp` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-18 — One working connection per process

`whatsapp` uses `capabilities-contract` 0.5.1. Every connection it opens on the transaction path is the process's one working connection, which the library lends to each operation and keeps until the process exits, so a command connects once rather than once per read; a connect that fails is tried again, and a working connection the store or the network lost is replaced, a statement that lost it before anything of its transaction reached the store running once more on the new one and a commit never sent twice.

## 2026-10-18 — Connections go to the store's transaction address

`whatsapp` uses `capabilities-contract` 0.5.0. The capture store, the profile lookups and `migrate files` connect on transaction connections, at the store's transaction address where one is named; the outbox listener keeps its session connection. With one address everything reaches it as before.

## 2026-10-16 — `migrate files` moves profile files for `capabilities migrate`

Its mover saves the project's profile files into the store at the project's scope, as `profiles import` does, without removing them; the manager archives and removes them once the whole move is verified. `profiles import` is unchanged.

## 2026-10-13 — Outside any project, `whatsapp pair` links a connection declared globally

Run outside any project, `whatsapp pair` (with or without `--events`) uses the machine's own connections as `connections` and `doctor` do, so a connection added with `whatsapp connection add <id> --global` is linked on the machine, where `pair` used to refuse with exit 4 `connection_not_granted`. The verb a `connections.link` method runs is the one that is lent them; no project and no agent is granted anything, a connection whose grant is switched off is still refused, the connection stays read-only there, and the read-only switch still refuses `pair`. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — Outside any project, `whatsapp connections` and `whatsapp doctor` read the machine's own connections

`whatsapp` declares `connections` and `doctor` as machine reads. Run outside any project, `whatsapp connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `whatsapp doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `whatsapp` declares what a person is asked for, and writes it through the contract

`whatsapp manifest` now declares a connection's fields under `connections.fields` (`engine`, `number`, `expected_account_id`, `device_name`, `initial_sync_days`, `messages_dir`, `base_url`, `secret_env`, `dashboard_user_env`, `dashboard_pass_env`, `session`, `waha_engine`), each with its label, help, type, whether it is required and whether it is a secret, and `whatsapp connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `whatsapp connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`DEEPGRAM_API_KEY`) are declared under `connections.setup`, and `whatsapp setup` reports them while `whatsapp setup set` writes them to `credentials.env`, a secret only from stdin. Its settings (project `service/settings.json`, global `service/settings.json`) are declared under `settings`, with the keys a panel shows as controls, and `whatsapp settings` reads every document whole while `settings get|set|unset|put` write one key or one document, checked as whatsapp reads it. `whatsapp pair --events` speaks JSON lines on stdout - each QR reference as a `qr` event as it arrives, `pairing_code`, `phone_needed` answered by one line on stdin when no number is known, `linked`, `progress`, then `done` or `error` - and is declared under `connections.link`. Without `--events`, `pair` prints and paints its QR on stderr exactly as before. Bare `whatsapp help` ends with a section rendered from these declarations. Nothing else changes.

## 2026-10-12 — Harness profiles are kept in the store

Breaking. The assistant service resolves every profile its settings name - `profile` and `job_profile` at a person, a group and `defaults` - from the store, as agentrooms and tasks do: the project's active version in the `harness_profiles` table, then the global one, then callva-harness-runner's shipped profiles, then the four this bundle ships. The runner is now 0.9.0. A project's profile files in `capabilities/whatsapp/service/profiles/` are no longer read: while any is there, `service start` and `service doctor` refuse with exit 6 `profiles_not_imported`. Run `whatsapp profiles import` to see what moves, then `whatsapp profiles import --apply`, which saves each file at the project's scope and removes it. A profile is saved from then on with `tasks profiles save`. `whatsapp profiles list` shows each profile as it resolves, and `service doctor` names the layer each came from. Fit is checked as before, now on the resolved version.

## 2026-10-11 — A store behind Supabase's transaction pooler is refused at connect

`whatsapp` now uses `capabilities-contract` 0.4.1. A transaction-mode pooler keeps no session between transactions, so the schema binding, prepared statements and notifications a connection relies on are lost through one, and it is not supported. A database setting that points at Supabase's transaction pooler - a host under `pooler.supabase.com` on port 6543 - is refused before anything is sent to it, as `transaction_pooler`, with a hint naming the session pooler: the same host on port 5432. Every other database, the session pooler included, is reached as before.

## 2026-10-09 — The capture follows the project's database

Breaking. `whatsapp` now uses `capabilities-contract` 0.4.0 and keeps its capture in the database the project resolves: the `AGENTKIT_DB_*` keys in the project's `.env.local` and `.env`, then the process environment, then the machine's store setting; `capabilities store show` shows which answers. `CAPABILITIES_STORE_URL` and `CAPABILITIES_STORE_PASSWORD` are no longer read, and the service's deploy descriptor no longer declares them: a deployed node takes `AGENTKIT_DB_*` from its server's environment. The assistant service resolves its database once at launch and keeps it for its whole run, so a rebuilt session, the job runner, a reconnect and the outbox listener all stay on the database it started on. A project that names no database, with none in the environment or on the machine, is refused with exit 6 `store_not_configured` as before. `service/store.py` is no longer shipped; nothing read it.

## 2026-10-09 — `whatsapp migrate` brings its tables up to date when asked

`whatsapp migrate` applies the pending steps to its tables in the machine's store, and `whatsapp migrate status` reports where they stand without a lock and changes nothing. The manager runs `whatsapp migrate` when it installs or updates whatsapp, and the service runs it when it starts; every other call takes no lock and migrates only when it finds its tables missing or older than its code. `whatsapp` now uses `capabilities-contract` 0.3.0: a store that does not answer a connect within 10 seconds is reported as `store_unreachable`, and both ends of a connection send TCP keepalives, so the store drops a client that died without closing.

## 2026-10-09 — One stuck session on the store no longer stops it

`whatsapp` now uses `capabilities-contract` 0.2.1. Opening the store takes no lock once its tables are in place, so a session left holding the store's schema lock no longer hangs every command and service doctor behind it. Where a lock is still needed, it waits at most 10 seconds and then reports `store_busy`; a process killed while waiting leaves no session queued behind it, and a session that holds the lock without working is ended by the store within 5 seconds.

## 2026-10-08 — The store setting is read from the family's file

`whatsapp` now uses `capabilities-contract` 0.2.0, which reads the machine's store setting from `$XDG_CONFIG_HOME/agentkit/store.json`, the file `capabilities store set` now writes, and from the manager's former files while it is absent; `AGENTKIT_STORE_URL` overrides it ahead of `CAPABILITIES_STORE_URL`. A machine whose setting is in the former files behaves as before.
