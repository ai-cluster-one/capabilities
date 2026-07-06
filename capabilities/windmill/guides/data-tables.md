# Windmill — Data Tables

How a consuming project uses Windmill Data Tables as agent-managed structured storage.

Windmill Data Tables are Postgres-backed stores hosted inside the Windmill instance. They are legitimate persistent state in the external system, not mirrored repo files. A project should keep only store labels and table ownership conventions in its identifiers/reference; SQL schema lives in migrations or the scripts that manage the tables.

## Access Paths

There are two access paths:

- **Outside a run — CLI verbs.** `windmill datatable run "<sql>"` executes SQL as an ephemeral preview job. `windmill datatables`, `windmill datatable tables`, and `windmill datatable schema <table>` inspect stores and schemas. Use this path for bootstrap, migrations, ad-hoc inspection, and operator checks. Mutating SQL is write-gated by the connection.
- **Inside a run — Windmill client.** A running script can use the in-script Windmill client to read/write the table from the worker. Use this for runtime CRUD. Avoid shelling back out to the CLI from inside a job unless the script explicitly needs an operator-facing CLI behaviour.

## SQL Parameter Convention

When passing `--arg name=value` to SQL, bind parameters by Windmill's header convention:

```sql
-- $1 name (text)
select $1::text;
```

A bare `$1` without the header can bind as null. Inline literals need no header, but dynamic values should use arguments instead of string interpolation.

## DDL And Store Lifecycle

Idempotent DDL is the normal pattern: `CREATE TABLE IF NOT EXISTS`, then inspect before writing. A script may provision its own table before using it when that keeps the workflow self-contained.

`windmill datatable-db list|create|drop` operates on the underlying instance databases and requires appropriate instance privileges. `create` and `drop` are mutating; `drop` is irreversible and should be treated as a destructive operation.
