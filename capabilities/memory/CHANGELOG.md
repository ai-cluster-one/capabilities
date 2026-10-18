# memory — change log

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
