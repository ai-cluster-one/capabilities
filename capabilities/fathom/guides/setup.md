# Setup — standing up the storage engine

`fathom` binds two backends: the Fathom account it reads from, and a
PostgreSQL database it syncs into. The Fathom side needs only an API key; this
guide is the **storage engine** — provisioning the database, wiring the
connection, creating the tables, and proving the chain. From zero to a green
`fathom doctor`.

## What the store is

Any PostgreSQL database the consumer owns — a managed instance from any
provider, or a self-hosted / local Postgres. `fathom` needs five connection
facts and two tables, and it creates the tables itself. It holds no opinion
about where the database runs.

## 1. Provision a PostgreSQL database

Stand up a Postgres instance — your managed provider's create-database flow, or
a local install (see the official PostgreSQL documentation). Then collect the
**connection facts**, each of which maps to one credential key:

| fact     | credential key       | note                       |
|----------|----------------------|----------------------------|
| host     | `FATHOM_DB_HOST`     | the server hostname        |
| port     | `FATHOM_DB_PORT`     | default 5432               |
| user     | `FATHOM_DB_USER`     | default postgres           |
| password | `FATHOM_DB_PASSWORD` | secret                     |
| database | `FATHOM_DB_NAME`     | default postgres           |
| SSL mode | `FATHOM_DB_SSLMODE`  | default require            |

Most providers show these as a single **connection string**
(`postgresql://user:pass@host:port/db`) in a connection panel — read the parts
off it. Two things bite most often:

- **TLS.** A managed Postgres expects an encrypted connection — leave
  `FATHOM_DB_SSLMODE` at its `require` default. A **local** Postgres without TLS
  needs `FATHOM_DB_SSLMODE=disable`.
- **Connection poolers.** If your provider fronts the database with a pooler,
  the **host** and the **user** are the pooler's namespaced forms (a user like
  `postgres.<project-ref>`, a pooler hostname), not the direct-database ones.
  Use whichever host/user pair the panel gives for the *pooled* connection.

## 2. Wire the credentials

`fathom` resolves credentials through the cascade (`fathom help` has the full
order). For a single account the home is `~/.config/fathom/credentials.env` —
clean `KEY=VALUE` lines, no trailing inline comments:

    FATHOM_API_KEY=<your Fathom External API key>
    FATHOM_DB_HOST=<host>
    FATHOM_DB_PASSWORD=<password>
    FATHOM_DB_PORT=5432
    FATHOM_DB_USER=<user>
    FATHOM_DB_NAME=<database>
    # FATHOM_DB_SSLMODE=disable   # uncomment only for a local non-TLS Postgres

Then confirm what resolved, and from where, without touching the network:

    fathom connections

Every required key should read `"set": true` with the `tier` and `source` you
expect.

## 3. Create the tables

    fathom schema

Idempotent — `CREATE TABLE IF NOT EXISTS` for `fathom` and `vocabulary`, safe to
re-run. This is the step that makes the engine *ready*, not merely reachable.

## 4. Prove the whole chain

    fathom doctor

`"ok": true` means the API key is accepted, the database is reachable and
authenticated, and the schema is present. That is the single readiness oracle:
green means the capability works here.

## If it is not green — the failure ladder

`doctor` names the next action in each connection's `hint`; this is the map:

| exit | reads as            | do                                                                       |
|------|---------------------|--------------------------------------------------------------------------|
| 2    | `unset: …`          | a required key is empty — fill it in `credentials.env` (step 2)           |
| 2    | `db_auth`           | host reached, login rejected — check `FATHOM_DB_USER` / `FATHOM_DB_PASSWORD` (pooler user form? step 1) |
| 5    | `db_unreachable`    | no connection — check `FATHOM_DB_HOST` / `FATHOM_DB_PORT`, the SSL mode, and that the database admits your IP |
| 3    | `schema` missing    | connected but tables absent — run `fathom schema` (step 3)               |
| 2    | Fathom rejected key | the API key is bad — check `FATHOM_API_KEY`                               |

## 5. First sync

With `doctor` green:

    fathom sync

pulls every meeting with its transcript into the store. From there `list`,
`read`, `search` / `scan` / `grep`, and the `enrichment` guide take over.
