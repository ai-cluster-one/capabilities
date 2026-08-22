# Operating automations

Run `automations doctor` after configuration changes. It validates the config, script paths, shared records store, environment selection, and bundled runtime.

The daemon reads configuration at startup. Restart the service after changing schedules, limits, environment selectors, or script declarations.

Use `automations service run` as the foreground process under Docker Compose or another process supervisor. `automations service start` and `stop` are local conveniences. Inspect work through `automations runs`, `automations show`, and `automations logs`; cancellation and retry are explicit CLI operations.

The scheduler records due work and immediately returns to ticking. Jobs execute in separate process groups with captured output, bounded concurrency, timeouts, graceful cancellation, and retry policy. On startup, active records become `interrupted` and surviving process groups are terminated; `engine.recovery` chooses whether interrupted work remains failed for inspection or is queued as a new attempt. The capabilities store is the operational ledger and queue.

Project runtime files live under `$XDG_STATE_HOME/capabilities/projects/<project-slug>/automations` by default. `AUTOMATIONS_STATE_DIR` remains an explicit override. On the first service start after upgrading from the envelope layout, durable files are copied from `capabilities/automations/state`, historical run-log paths are repointed in the store, generated scripts and daemon files are rebuilt, and the source is preserved for operator-verified cleanup.
