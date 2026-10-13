# memory — change log

## 2026-10-13 — Adopt reads a folder's dates in the zone they were written in

`memory adopt` now reads a memory file's date and a section's `HH:MM` in the machine's local zone, the zone ContextKit's memory writer stamps them in, instead of as UTC, so a carried entry keeps the moment it was written rather than one shifted by the writer's offset. `--timezone ZONE` reads them in the IANA zone it names instead, each date taking that zone's offset on it, and every entry's `dated_from` names the zone it was read in. An entry already carried still answers `already`: what makes an entry the same entry is unchanged, and its stored date is not rewritten.

## 2026-10-13 — The block counts every active entry it leaves out

When the rendered block does not show every active project-wide entry, its one count line now counts all of them, those older than `window_days` included, and points to `memory list` and `memory search`: `Showing N of M active entries; ...`, where N counts the pinned entries the block carries as well. Before, it counted only the entries inside the window, so entries older than it were left out without a word. A block that shows every active entry carries no count line, as before. The render report and the sidecar add `active`, the number of active project-wide entries.

## 2026-10-13 — A project's memory as entries in its store, rendered into its context

`memory` keeps a project's memory as entries in the PostgreSQL the project resolves: each one a titled conclusion, appended and never edited, superseded or dropped with a reason, every change an event in an append-only trail the store announces on `memory_changed`. `memory render` writes the project's block into the state folder of its envelope, and the manifest declares it as the capability's context contribution, so the capabilities block of every session in the project carries the newest entries under a fixed size. Reads (`list`, `show`, `search`, `log`, `status`) answer plain lines or stable JSON, `groom` consolidates the window through one checked agent turn that can be undone, `adopt` carries existing memory folders into the store, and `export` writes everything out. The service keeps each served project's block current and runs consolidation, in one project or once per machine for the projects that join it.
