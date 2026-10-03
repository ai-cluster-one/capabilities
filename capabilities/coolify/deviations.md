# coolify deviations

This file's sole purpose is to hold `coolify`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults, kept apart so an audit reads them as choices, not drift (DOCTRINE — *[Deviations are allowed — and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## `connect` writes the connection entry and the token's credentials file

The standard keeps a connection record and `credentials.env` human-written: a script never writes a secret back into a credentials file. `coolify connect` writes both, in one act: the entry, holding only non-secret values, through the contract's records adapter, and the token into the credentials file of the same scope - the project's `.env.local`, or the user's `credentials.env` - at mode 0600.

The intent the rule protects holds. The token arrives on standard input, from a file or from an env key, never on argv; it is written by an atomic writer that keeps every other line, single-quoted so the contract's parser reads it back whole, and the write is proven by reading it back through that parser; it is never printed. The alternative to writing it here is a person or an agent carrying a token through a prompt or a terminal, which is where tokens leak. A connection-write verb in the contract itself, for every capability, would replace this one; until the contract has one, the write lives here.
