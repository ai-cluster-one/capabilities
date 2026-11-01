# A store's connections

Use this guide to set up and judge how agents and AgentKit connect to a store: which address each connection dials, a pooler against a direct Postgres, how many connections a machine holds, which limits bind, the default AgentKit sets on Supabase, and what to change when the pool runs short.

`capabilities help` (STORE VERBS) is the contract for the store setting and its verbs, and `capabilities guide supabase` is how a store is pointed at Supabase. This guide is about sizing and checking what those set up.

## The two addresses

A store has a **transaction address**, its main and required one. Every ordinary connection goes there: each command, the records reader, a lease renewal, a service's questions, AgentKit's reads. A transaction connection carries no session state, so it works through a transaction-mode pooler, and through it alone everything works.

A store may also have a **session address**: the same store at another endpoint, used only by connections that need state kept between transactions:

- LISTEN for change notifications, held by a long-running service (tasks, automations, memory and the other services that follow the store) and by a watch. With a session address a change is noticed at once; without one it is noticed by polling about every second. A listener that cannot be opened costs only that, and is tried again every 30 seconds.
- Until 2027-01-05, the transitional session locks of tasks and memory, taken only where the store has a session address (see "The session locks end on 2027-01-05" below).

`capabilities store show` names both under `setting.addresses`; `session` is null for a transaction-only store.

## A pooler or a direct Postgres

**A direct Postgres** - your own server, a managed instance's direct endpoint - is one address that serves both kinds. Every client connection is a server connection, so `max_connections` is the only connection limit, and it must cover every connection every machine holds at once.

**A pooler** sits in front of Postgres and shares a smaller number of server connections among many clients:

- in **transaction mode** a client holds a server connection only while a transaction runs, so many idle clients cost the database nothing. This is what the transaction address is.
- in **session mode** a client holds one server connection for as long as it is connected, exactly as a direct connection does. This is what the session address is, when it is a pooler.

Supabase's shared pooler is **Supavisor**: transaction mode on port 6543, session mode on port 5432 of the same host. Its dedicated pooler and the usual self-hosted one are **PgBouncer**, which runs one mode per listener. A transaction pooler other than Supabase's, as the store's one address, is declared with `--transaction-pooler`; `capabilities guide migrate-to-database` says when it may be set. A session address for a self-hosted store can be a PgBouncer in session mode or Postgres directly.

## How many connections a machine holds

Count clients (what the pooler admits) apart from server connections (what Postgres spends).

| Holder | Transaction clients | Session clients (only with a session address) |
| --- | --- | --- |
| Each running service, per process | 1, held while it runs; a second only while one is already in use | 1 for its listener; tasks and memory at most 1 more for their lock, until 2027-01-05 |
| Each command, a capability verb or an agent turn's call | 1, for the command's life | 0, except `tasks watch` (1) |
| AgentKit | its own read pool of 2 | 0 |
| AgentKit's Database widget | 1 short probe while it checks, then closed | 0 |

**One Mac** running AgentKit and its agents' services therefore holds, as transaction clients, one per service process, 2 for AgentKit and one per command running at that moment; through a transaction pooler only the transactions running at that instant hold server connections, usually a handful. With a session address it also holds one session client per service process, each a server connection for as long as the service runs.

**One server node** holds the same per service process and per command, without AgentKit or its widget.

The session count is the one that grows: it is the number of service processes, summed over every machine that serves projects in the store, plus the tasks and memory locks until 2027-01-05. On a direct Postgres every row of the table is a server connection.

## The limits that bind

- **Postgres `max_connections`**: every server connection, from the poolers, from Supabase's own services and from anything connecting directly, counted together.
- **The pooler's client limit**: how many clients the pooler admits (Supavisor's "max clients", PgBouncer's `max_client_conn`). Transaction clients count here.
- **The pool size, per mode**: the server connections the pooler opens for one role on one database, separately for transaction mode and session mode (Supabase's `default_pool_size`, PgBouncer's `default_pool_size`). In session mode it is also the client limit, since every session client holds a server connection. The transaction pool and the session pool together must fit in `max_connections` beside everything else.

Supabase's values by compute size, as Supabase publishes them (supabase.com/docs/guides/platform/compute-and-disk; read it there for the current ones):

| Compute | Database max connections | Pooler max clients | Default pool size |
| --- | --- | --- | --- |
| Nano (free) | 60 | 200 | 15 |
| Micro | 60 | 200 | 15 |
| Small | 90 | 400 | 15 |
| Medium | 120 | 600 | 15 |
| Large | 160 | 800 | 20 |
| XL | 240 | 1,000 | 20 |
| 2XL | 380 | 1,500 | 25 |
| 4XL | 480 | 3,000 | 32 |
| 8XL | 490 | 6,000 | 64 |
| 12XL | 500 | 9,000 | 96 |
| 16XL | 500 | 12,000 | 128 |

## The default AgentKit sets on Supabase

When AgentKit connects a Supabase database it sets the pool size to 40% of `max_connections` for each mode - 40% for the transaction pool, 40% for the session pool - and leaves 20% to Supabase's own services (Auth, PostgREST, Realtime, the dashboard). On Micro that is 24 and 24 of 60, with 12 left.

Supabase's own guidance is to stay at or below 40% of `max_connections` for a pool where its REST API is used heavily, and up to 80% otherwise. A store that also serves an application through Supabase's REST API keeps the default or lowers it; a store that serves only agents may raise the session pool when its session clients need it.

## Checking a store

```sh
capabilities store show     # which level is in force, and the addresses each kind of connection dials
capabilities store doctor   # proves TLS, the schema, the transaction address and the session address in use
```

`store doctor` exits 0 when every check holds and 7 when one fails, naming it under `checks`; a transaction-only store skips the session check and says it needs none.

On a session connection or a direct Postgres, Postgres shows who holds what:

```sql
show max_connections;
select application_name, state, count(*) from pg_stat_activity group by 1, 2 order by 3 desc;
```

Behind a transaction pooler, Postgres sees the pooler's server connections rather than the processes, so count clients from the table above.

On Supabase, the pool and the limit are read and changed with the Management API, using the person's Supabase access token. **Ask the person** for the token, and before any change:

```sh
# the pooler: default_pool_size and max_client_conn
curl -s -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" https://api.supabase.com/v1/projects/<project-ref>/config/database/pooler

# set the pool size
curl -s -X PATCH -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" -H "Content-Type: application/json" -d '{"default_pool_size": <n>}' https://api.supabase.com/v1/projects/<project-ref>/config/database/pooler

# Postgres: max_connections among its settings
curl -s -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" https://api.supabase.com/v1/projects/<project-ref>/config/database/postgres

# set max_connections; it takes effect when the database restarts
curl -s -X PUT -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" -H "Content-Type: application/json" -d '{"max_connections": <n>, "restart_database": true}' https://api.supabase.com/v1/projects/<project-ref>/config/database/postgres
```

Restarting the database drops every connection on every machine; services reconnect on their own. Raising `max_connections` beyond the compute's recommended value spends memory the compute may not have, so the lasting answer to a store that outgrows it is a larger compute.

## When the pool runs short

| What you see | What it means | What to change |
| --- | --- | --- |
| `EMAXCONNSESSION`, "max clients reached in session mode - max clients are limited to pool_size" | More session clients than the session pool: too many service processes across machines for it | Raise `default_pool_size` within `max_connections`, or drop the session address (`capabilities store set` without `--session-*`), after which changes are polled every second |
| `EMAXCONN`, "max client connections reached" | The pooler's client limit is reached | Find the machines holding the clients; a larger compute raises the limit |
| `ECHECKOUTTIMEOUT`; commands that wait and then fail | Every pooled server connection stayed busy past the pooler's wait | Raise the pool size within `max_connections`; look for long transactions in `pg_stat_activity` |
| `sorry, too many clients already`, `remaining connection slots are reserved` | Postgres `max_connections` is spent: pools sized past it, or direct connections beside them | Lower a pool, or raise `max_connections` |
| `ECIRCUITBREAKER`, "too many authentication failures, new connections are temporarily blocked" | Repeated failed logins from one client address - in Supavisor's current source, 10 within 150 seconds - block that address for about two minutes, so every process on that machine fails at once | Correct the login on that machine (the user is `postgres.<project-ref>`; a rotated password is set again with `capabilities store set ... --password-stdin`), then wait out the block; retrying only extends it |
| PgBouncer: `no more connections allowed (max_client_conn)`, `query_wait_timeout` | Its client limit, or its pool, as above | Its `max_client_conn` or `default_pool_size` |

Before dropping a session address, check that `tasks doctor` and `memory doctor` name no machine still running an earlier release that relies on the session locks.

## The session locks end on 2027-01-05

Until 2027-01-05, tasks and memory still take transitional session locks where the store has a session address, because earlier releases of them read those locks. On that date the locks are retired. Until then, `tasks doctor` names a machine where an earlier tasks service still serves against the store, and `memory doctor` one where an earlier memory is grooming; update those machines before 2027-01-05.
