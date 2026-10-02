# tasks — change log

## 2026-10-02 — Watch the store's changes over one held connection

`tasks watch [--project ID | --all-projects]` is a read that stays: it holds one connection, listens for the store's change notification, and prints one JSON line per change to a task its scope reads - `task_changed`, `activity_added`, `run_started`, `run_ended` - each carrying the task as a `list` row, with a `ready` line first and a `counts` line after each burst. On a lost connection it reconnects with backoff, catches up what was touched meanwhile and prints `resync`. It exits 0 on SIGTERM or when its stdin closes. The notifications come from new triggers, so a store needs `tasks migrate --apply`; until then `watch` refuses with exit 5 and `doctor` reports the store behind. Run `tasks help` WATCH for the line shapes.

## 2026-10-02 — Pause and resume the service without stopping it

`tasks service pause [--reason TEXT] [<worker>...]` stops the daemon starting new turns, on every lane or on the named ones, while running turns finish and the daemon keeps running; `tasks service resume [<worker>...]` lifts it. The pause is runtime state, so it needs no reload, survives a daemon restart and leaves `service doctor` ok. `tasks service status` shows it. Run `tasks help` SERVICE for the details.
