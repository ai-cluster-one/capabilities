# Setup — standing up the journal store

`journal` binds one backend: a PostgreSQL database holding the entries a
capture pipeline writes and the enrichment an agent adds. This guide takes it
from zero to a green `journal doctor` — provisioning the database, declaring
the connection, creating the tables, and proving the chain.

## What the store is

Any PostgreSQL database the consumer owns — a managed instance from any
provider, or a self-hosted / local Postgres. `journal` needs five connection
facts, one password, and two tables, and it creates the tables itself. It holds
no opinion about where the database runs, and it never writes entries: they
arrive from whatever pipeline captures them (a bot, a form, an import). This
CLI reads, enriches, chunks, and searches what is already there.

## 1. Provision a PostgreSQL database

Stand up a Postgres instance — your managed provider's create-database flow, or
a local install (see the official PostgreSQL documentation). Then collect the
**connection facts**:

| fact     | connection field | env-key fallback      | note                |
|----------|------------------|-----------------------|---------------------|
| host     | `db_host`        | `JOURNAL_DB_HOST`     | the server hostname |
| port     | `db_port`        | `JOURNAL_DB_PORT`     | default 5432        |
| user     | `db_user`        | `JOURNAL_DB_USER`     | default postgres    |
| database | `db_name`        | `JOURNAL_DB_NAME`     | default postgres    |
| SSL mode | `db_sslmode`     | `JOURNAL_DB_SSLMODE`  | default require     |
| password | `db_password_env`| the key it names      | secret, never literal |

Most providers show these as a single **connection string**
(`postgresql://user:pass@host:port/db`) in a connection panel — read the parts
off it. Two things bite most often:

- **TLS.** A managed Postgres expects an encrypted connection — leave
  `db_sslmode` at its `require` default. A **local** Postgres without TLS needs
  `"db_sslmode": "disable"`.
- **Connection poolers.** If your provider fronts the database with a pooler,
  the **host** and the **user** are the pooler's namespaced forms (a user like
  `postgres.<project-ref>`, a pooler hostname), not the direct-database ones.
  Use whichever host/user pair the panel gives for the *pooled* connection.

## 2. Declare the connection

`journal` requires an explicit registry — one connection is still declared, not
implied. For a personal store the home is
`~/.config/journal/connections.json`; a project that owns its own store puts
the same envelope at `capabilities/journal/connections.json` (project first,
first found authoritative, never merged):

```json
{
  "default": "personal",
  "connections": {
    "personal": {
      "db_host": "<host>",
      "db_port": "5432",
      "db_user": "<user>",
      "db_name": "<database>",
      "db_sslmode": "require",
      "db_password_env": "JOURNAL_DB_PASSWORD",
      "user_id": "<author id>",
      "allow_write": true
    }
  }
}
```

Non-secret wiring sits literally in the entry. The password is named by
env-key indirection — `db_password_env` holds the *name* of the key, never the
value.

**`user_id`** is optional and worth setting when one pipeline writes on behalf
of one author: every listing and search then scopes to that author by default,
and `--all-users` lifts the scope for the occasional cross-author look. Leave
it out for a single-author store.

**`allow_write`** governs the mutating verbs (`update`, `chunk`, `schema`).
Set it `false` on a connection that should stay a read-only source.

## 3. Wire the password

The key `db_password_env` names resolves through the cascade (`journal help`
has the full order). For a personal store the home is
`~/.config/journal/credentials.env` — clean `KEY=VALUE` lines, no trailing
inline comments:

    JOURNAL_DB_PASSWORD=<password>

Then confirm what resolved, and from where, without touching the network:

    journal connections

Every required value should read `"set": true` with the `tier` and `source` you
expect — `connection` for the literal wiring, `user` or `env` for the password.

## 4. Create the tables

    journal schema

Idempotent: `CREATE TABLE IF NOT EXISTS` for `journal` and `journal_chunks`
plus `CREATE INDEX IF NOT EXISTS` for their indexes. Additive only — run it
against a store that already holds entries and it changes no existing column
and no row. This is the step that makes the store *ready*, not merely
reachable.

## 5. Prove the whole chain

    journal doctor

`"ok": true` means the database is reachable and authenticated, both tables
exist, and the counts come back — it reports how many entries the store holds,
how many carry a summary, how many are enriched, and how many chunks exist.
That is the single readiness oracle: green means the capability works here.

## If it is not green — the failure ladder

`doctor` names the next action in each connection's `hint`; this is the map:

| exit | reads as                | do                                                                      |
|------|-------------------------|-------------------------------------------------------------------------|
| 6    | `connections_required`  | no registry — write one (step 2)                                        |
| 6    | `no_secret_env`         | the entry names no `db_password_env` — add it (step 2)                  |
| 6    | `incomplete_connection` | a required field resolves nowhere — usually `db_host` (step 2)          |
| 2    | `missing_secret`        | the named env key is empty — fill it in `credentials.env` (step 3)      |
| 2    | `db_auth`               | host reached, login rejected — check `db_user` and the password (pooler user form? step 1) |
| 5    | `db_unreachable`        | no connection — check `db_host` / `db_port` / `db_sslmode`, and that the database admits your IP |
| 3    | `schema` missing        | connected but tables absent — run `journal schema` (step 4)             |

## 6. First pass

With `doctor` green:

    journal list --has-summary false

is the enrichment worklist. From there `read`, `update`, `chunk`, the three
search levels, and the `enrichment` guide take over.
