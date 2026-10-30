# coolify deviations

This file's sole purpose is to hold `coolify`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults, kept apart so an audit reads them as choices, not drift (DOCTRINE — *[Deviations are allowed — and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## `connect` writes the connection entry and the token's credentials file

The standard keeps a connection record and `credentials.env` human-written: a script never writes a secret back into a credentials file. `coolify connect` writes both, in one act: the entry, holding only non-secret values, through the contract's records adapter, and the token into the credentials file of the same scope - the project's `.env.local`, or the user's `credentials.env` - at mode 0600.

The intent the rule protects holds. The token arrives on standard input, from a file or from an env key, never on argv; it is written by an atomic writer that keeps every other line, single-quoted so the contract's parser reads it back whole, and the write is proven by reading it back through that parser; it is never printed. The alternative to writing it here is a person or an agent carrying a token through a prompt or a terminal, which is where tokens leak. The contract now has that verb for every capability, `coolify connection add`, and the doctrine counts a person's own write through it as human-written. `coolify connect` stays beside it, for the doctor probe it runs on the new connection and for the callers that already use it.

## A caller may lend one process a connection the registry does not declare

The standard resolves every connection from a registry and lets a project use one only once a grant enables it there (DOCTRINE rule 18). `coolify` also takes a connection lent to the one process it runs in, through `COOLIFY_LENT_BASE_URL` and `COOLIFY_LENT_TOKEN` in that process's environment: while both are set they are the run's only connection, read from no registry and asked of no grant.

An application that keeps a Coolify connection of its own - not any agent's - reaches an agent's server nodes through it, and the agent is granted nothing for that, so no grant could carry it and no agent's vault holds its token; `deployment node --coolify-stdin` lends it to each `coolify` it runs. The intent the rule protects holds: the token is never on argv, printed or written; a lent connection never sits beside a selected one (`--connection` with it is refused); the write gate and the process-wide read-only switch apply to it as to any connection; and a process nobody lent one to resolves exactly as before.
