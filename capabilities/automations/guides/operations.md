# Operating automations

Run `automations doctor` after configuration changes. It validates the config, script paths, SQLite state, environment selection, and bundled runtime.

The daemon reads configuration at startup. Restart the service after changing schedules, limits, environment selectors, or script declarations.

Use `automations service run` as the foreground process under Docker Compose or another process supervisor. `automations service start` and `stop` are local conveniences. Inspect work through `automations runs`, `automations show`, and `automations logs`; cancellation and retry are explicit CLI operations.

The scheduler records due work and immediately returns to ticking. Jobs execute in separate process groups with captured output, bounded concurrency, timeouts, graceful cancellation, and retry policy. On startup, active records become `interrupted` and surviving process groups are terminated; `engine.recovery` chooses whether interrupted work remains failed for inspection or is queued as a new attempt. SQLite is the local operational ledger and queue. A future coordinated backend can replace that storage boundary without changing project scripts or schedule declarations.
