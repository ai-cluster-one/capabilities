# automations — change log

## 2026-10-06 — The run ledger works on a PostgreSQL store

With `CAPABILITIES_STORE_URL` naming a `postgresql://` store and its driver present, the scheduler now enqueues, claims, runs, cancels and retries runs there, and `run`, `runs`, `show`, `logs`, `cancel`, `retry`, `service status` and `doctor` read and write them; before, every one of them failed on the first run query. A scheduled firing is still claimed once however many daemons share the store: its dedupe key decides it on either backend, and on PostgreSQL a dispatcher holds the pending rows it is choosing from until it has taken one. `doctor` reads the `runs` columns from the database's own catalogue on PostgreSQL. On the SQLite default nothing changes, and existing run history reads as it did.
