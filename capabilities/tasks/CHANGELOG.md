# tasks — change log

## 2026-10-02 — Pause and resume the service without stopping it

`tasks service pause [--reason TEXT] [<worker>...]` stops the daemon starting new turns, on every lane or on the named ones, while running turns finish and the daemon keeps running; `tasks service resume [<worker>...]` lifts it. The pause is runtime state, so it needs no reload, survives a daemon restart and leaves `service doctor` ok. `tasks service status` shows it. Run `tasks help` SERVICE for the details.
