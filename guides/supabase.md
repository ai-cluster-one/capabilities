# A store on Supabase

Use this guide to point a machine's store setting at a Supabase database: which of its poolers is the store's address, which is optional, and the one command that sets either.

`capabilities help` (STORE VERBS) is the contract for every verb named here, and SHEBANG.md's "The store setting" is the format they write.

## The transaction pooler is enough

Supabase offers two poolers on one host, `aws-0-<region>.pooler.supabase.com`, both reached as the user `postgres.<project-ref>` on the database `postgres`:

- the **transaction pooler**, port 6543, is the store's main address. From the manager release that carries `capabilities-contract` 0.33 on, every capability works through it alone.
- the **session pooler**, port 5432, is optional. Named as the store's session address, it lets a long-running service notice a change at once; without it, changes are noticed by polling about every second. Nothing else needs it.

## Setting it

The transaction pooler alone:

```sh
capabilities store set --host aws-0-<region>.pooler.supabase.com --port 6543 --database postgres --user postgres.<project-ref> --password-stdin
```

With the session pooler as the optional session address:

```sh
capabilities store set --host aws-0-<region>.pooler.supabase.com --port 6543 --session-port 5432 --database postgres --user postgres.<project-ref> --password-stdin
```

Each command replaces the whole setting. `capabilities store show` names the two as `setting.addresses` (`session` is null without one), and `capabilities store doctor` proves each address in use and says plainly when the store has no session address.

The earlier two-address setup - the session pooler as `--host`/`--port` and the transaction pooler as `--transaction-port 6543` - writes the same file as the second command and keeps working; it is no longer needed.

Supabase's transaction pooler is recognised by its host and port 6543, so it needs no declaration. A transaction pooler of any other kind as the store's one address - a self-hosted connection pooler in transaction mode, or another provider's transaction-mode pool - is declared with `--transaction-pooler` beside `--host` and `--port`, and the store is then transaction-only as it is here; see `capabilities guide migrate-to-database` for when it may be set.

## The session locks end on 2027-01-05

Until 2027-01-05, tasks and memory still take transitional session locks where the store has a session address, because earlier releases of them read those locks. On that date the locks are retired. Until then, `tasks doctor` names a machine where an earlier tasks service still serves against the store, and `memory doctor` one where an earlier memory is grooming; update those machines before 2027-01-05.
