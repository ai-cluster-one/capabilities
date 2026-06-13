# fathom — recorded deviations

## A connection carries two secrets, named as a pair

A `fathom` connection spans two backends — the Fathom External API and a
PostgreSQL database — so a connection entry names **two** secrets by env-key
indirection: `fathom_key_env` (the API key) and `db_password_env` (the
database password), in place of the standard's single `secret_env`. The
implicit default resolves the same pair from `FATHOM_API_KEY` and
`FATHOM_DB_PASSWORD`.

Why: a meeting and its transcript are one logical record split across the API
that produces it and the database that stores it; a connection is the natural
unit that binds one Fathom account to one store, so both secrets belong to it.

Intent preserved: no secret sits in the registry — each is named by an env key
and resolved through the cascade (project `.env(.local)` → user
`credentials.env` → process env). `fathom connections` reports both, masked.

## The canonical data store is an external database, not local state

The synced corpus — meetings, transcripts, summaries, vocabulary — lives in
the consumer's own PostgreSQL database, reached over the network. The
capability writes no bulk data to disk, so `STATE = False` and there is no
`.capabilities/fathom/state/` corpus to git-ignore. The only local artifact is
the user-level guides cache, which the standard already exempts.

Why: the store is a managed database the consumer owns and points the
capability at, not a file tree the capability materializes. `doctor` proves the
store the way it proves any backend — a cheap authenticated round-trip
(`SELECT count(*) FROM fathom`) that also confirms the schema is present.

Intent preserved: credentials still mint nothing that lands in a committed
file; the database password resolves from env like any secret, and the data it
unlocks never enters the repo.

## The store's protocol is PostgreSQL, not HTTP

The Fathom API half uses the standard resilient-HTTP retry loop (bounded
back-off, 429 honoring `Retry-After`/`RateLimit-Reset`, 5xx retried, 4xx mapped
to the exit taxonomy). The database half speaks libpq via `psycopg2`: it fails
fast and maps the connection-time outcome directly — authentication failure →
exit 2, unreachable host → exit 5 — and a missing table → exit 3 with the
`fathom schema` remediation.

Why: a SQL round-trip is not an idempotent HTTP GET; retrying a rejected login
or a missing-table query buys nothing. The right behaviour is to surface the
category immediately.

Intent preserved: this is the executable standard's explicit allowance — a CLI
over a non-HTTP protocol realizes the same intent in its protocol's terms,
keeping the stdout/stderr split and the stable exit-code contract.
