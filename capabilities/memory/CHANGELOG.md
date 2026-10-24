# memory — change log

## 2026-10-24 — Turns run on callva-harness-runner 0.10.1

The runner is pinned at 0.10.1 in place of 0.9.0. Claude turns run through claude-agent-sdk 0.2.174, and a claude CLI up to 2.1.310 runs without the runner's out-of-range warning; codex is unchanged. The runner's 0.10.0 additions are new profile knobs only, so a turn runs as before.

## 2026-10-23 — The service declares how many nodes run it at once

The service descriptor declares `service.placement` `shared`: the service runs on every node it is placed on (`capabilities place memory --node <node>`), and the deployment runner takes no lease for it. A project that had it placed on more than one node, where the lease ran it on one, runs it on each of them.

## 2026-10-22 — A project without project.json joins the service; its slug is its agent's row

`service join` and the service's state take the project's slug from `_project_slug` (its agent's row in the store, else an older `project.json`'s, else its id) and need only its id. Declares `capabilities-contract>=0.12,<1`.

## 2026-10-21 — A connection whose network flow died fails in seconds

`memory` uses `capabilities-contract` 0.7.2. Its store connections give a network flow up once data sent on it has gone unacknowledged for 20 seconds (`tcp_user_timeout`), which keepalives cannot notice since none is sent while data waits, and send keepalives after 15 seconds idle, every 5 seconds, 3 probes: a connection whose flow stopped carrying packets without a FIN or a reset fails in about 20 seconds as a lost connection instead of waiting until the store ends the session, and a plain read that loses it that way runs once more on a new connection. A connection lost where `memory` catches nothing ends the command as `store_unreachable`, exit 5, as before.

## 2026-10-20 — A read is a transaction of its own in one round trip

`memory` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-19 — A render that changes nothing leaves the block as written

`memory render`, the service and every write that renders leave `state/context.md` as it is, its `_Rendered` line included, when the block they render differs from the one already written only in that line, so a context built from it does not change when nothing in it did. A block whose content changed is written with a new render time as before. `context.json` is written on every render as before, naming that render and the event it is current to, so `memory doctor` and `memory status` judge the block current or stale as they did. The block's format and the render report are unchanged.

## 2026-10-18 — The service keeps one working connection beside its listener

`memory` uses `capabilities-contract` 0.5.1. Every connection it opens on the transaction path is the process's one working connection, which the library lends to each operation and keeps until the process exits, so a command connects once rather than once per read; a connect that fails is tried again, and a working connection the store or the network lost is replaced, a statement that lost it before anything of its transaction reached the store running once more on the new one and a commit never sent twice. The service's steps run on its working connection, kept between steps beside the listener. `add`, `supersede`, `drop`, `pin` and `unpin` read the settings the block is rendered with before they open the store, so the command runs on one connection.

## 2026-10-18 — The service holds only its listener between steps

`memory` uses `capabilities-contract` 0.5.0. The service listens on a session connection and puts each step's questions on a transaction connection opened when the step first asks one and closed when it ends; the tables are migrated once, when it first connects. Every command connects on a transaction connection, at the store's transaction address where one is named, except `groom`, whose pass holds its lock on a session connection. A container is handed `AGENTKIT_DB_TRANSACTION_HOST` and `AGENTKIT_DB_TRANSACTION_PORT`. With one address everything reaches it as before.

## 2026-10-16 — `service init` writes a moved agent's settings as rows

For an agent whose records moved into the store, `service init` writes `{"version": 1}` as its settings rows, refusing as `settings_exist` (naming the store) where they exist already, and the doctor and settings errors name the store. Every other project writes service/settings.json as before.

## 2026-10-13 — `memory` declares what a person is asked for, and writes it through the contract

Its settings (project `service/settings.json`, global `service/settings.json`, machine `service/config.json`) are declared under `settings`, with the keys a panel shows as controls, and `memory settings` reads every document whole while `settings get|set|unset|put` write one key or one document, checked as memory reads it. Bare `memory help` ends with a section rendered from these declarations. Nothing else changes.

## 2026-10-13 — The block carries no entry: it says how to record and search, and how many entries there are

The block `memory render` writes into context no longer shows any entry, title or index line, pinned or not. In this order it carries a directive that this memory takes the place of the harness's built-in memory, how to record with `memory add` and what belongs in an entry, how to search with `memory search`, how to search deeply through a subagent that returns only its conclusion, the count of active project-wide entries with the moment the newest was written and the `memory list --since` command that lists it and anything newer, and `memory show` and `memory help`, then the provenance line. Its size no longer grows with the entries.

A pin now only keeps an entry out of grooming passes. If you relied on a pin to put a decision in front of every session, promote that decision into your project's doctrine, where every session reads it, and drop the entry naming where it went; `memory list --pinned` lists what to look at.

The render report, `context.json` and `memory status`'s `context` lose `shown`, `full`, `index`, `pinned_full`, `pinned_index` and `cap`, and add `newest` and `window_chars`; the service's project row reports `active` in place of `shown`. `window_days`, `min_entries`, `max_entries`, `block_chars` and `entry_chars` keep their names and bounds and now measure only the window, the unpinned recent entries: it fits while it holds at most `max_entries` and, each entry written out at `entry_chars`, at most `block_chars` characters, and the service grooms a window that no longer fits as before. Pinned entries no longer count against it.

## 2026-10-13 — Adopt reads a folder's dates in the zone they were written in

`memory adopt` now reads a memory file's date and a section's `HH:MM` in the machine's local zone, the zone ContextKit's memory writer stamps them in, instead of as UTC, so a carried entry keeps the moment it was written rather than one shifted by the writer's offset. `--timezone ZONE` reads them in the IANA zone it names instead, each date taking that zone's offset on it, and every entry's `dated_from` names the zone it was read in. An entry already carried still answers `already`: what makes an entry the same entry is unchanged, and its stored date is not rewritten.

## 2026-10-13 — The block counts every active entry it leaves out

When the rendered block does not show every active project-wide entry, its one count line now counts all of them, those older than `window_days` included, and points to `memory list` and `memory search`: `Showing N of M active entries; ...`, where N counts the pinned entries the block carries as well. Before, it counted only the entries inside the window, so entries older than it were left out without a word. A block that shows every active entry carries no count line, as before. The render report and the sidecar add `active`, the number of active project-wide entries.

## 2026-10-13 — A project's memory as entries in its store, rendered into its context

`memory` keeps a project's memory as entries in the PostgreSQL the project resolves: each one a titled conclusion, appended and never edited, superseded or dropped with a reason, every change an event in an append-only trail the store announces on `memory_changed`. `memory render` writes the project's block into the state folder of its envelope, and the manifest declares it as the capability's context contribution, so the capabilities block of every session in the project carries the newest entries under a fixed size. Reads (`list`, `show`, `search`, `log`, `status`) answer plain lines or stable JSON, `groom` consolidates the window through one checked agent turn that can be undone, `adopt` carries existing memory folders into the store, and `export` writes everything out. The service keeps each served project's block current and runs consolidation, in one project or once per machine for the projects that join it.
