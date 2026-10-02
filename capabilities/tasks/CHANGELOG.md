# tasks — change log

## 2026-10-02 — Bring a store behind by additive changes up to date without being asked

A store behind this version by additive changes only - a table, a column that is empty or defaulted, a trigger and the function it runs - is brought up to it by the first command that reaches it, read or write, and by the service when it starts listening and at every poll, so installing or updating `tasks` no longer leaves the store behind until someone runs `tasks migrate --apply`. It is applied once, under a lock the store holds for the schema, and reported as one `{"migrated": ...}` line on stderr and in the service log. No backup is taken, because dropping what was added restores the store. A change that is not additive is still applied only by `tasks migrate --apply`, after a backup, and nothing is applied on its own while one is pending; a connection without allow_write, the read-only switch and a role that may not create are served as before. Run `tasks help` STORE for the rule.

## 2026-10-02 — Watch the store's changes over one held connection

`tasks watch [--project ID | --all-projects]` is a read that stays: it holds one connection, listens for the store's change notification, and prints one JSON line per change to a task its scope reads - `task_changed`, `activity_added`, `run_started`, `run_ended` - each carrying the task as a `list` row, with a `ready` line first and a `counts` line after each burst. On a lost connection it reconnects with backoff, catches up what was touched meanwhile and prints `resync`. It exits 0 on SIGTERM or when its stdin closes. The notifications come from new triggers, so a store needs `tasks migrate --apply`; until then `watch` refuses with exit 5 and `doctor` reports the store behind. Run `tasks help` WATCH for the line shapes.

## 2026-10-02 — Pause and resume the service without stopping it

`tasks service pause [--reason TEXT] [<worker>...]` stops the daemon starting new turns, on every lane or on the named ones, while running turns finish and the daemon keeps running; `tasks service resume [<worker>...]` lifts it. The pause is runtime state, so it needs no reload, survives a daemon restart and leaves `service doctor` ok. `tasks service status` shows it. Run `tasks help` SERVICE for the details.
