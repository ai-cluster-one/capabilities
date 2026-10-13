# agentrooms deviations

This file's sole purpose is to hold `agentrooms`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults and the [DOCTRINE](../../DOCTRINE.md), kept apart so an audit reads them as choices, not drift (DOCTRINE - *[Deviations are allowed - and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## `register` writes the connection entry and the agent's token

The standard keeps a connection record human-written and what a login mints in state. `agentrooms register` writes both halves of a new agent in one act: the entry, holding only non-secret values, through the contract's records adapter, and the access token the registration minted into the project's `.env.local` under the env key the entry's `secret_env` names, at mode 0600.

The token is the agent's only credential - the account has no password - and the connection resolves it through `secret_env` like any other secret, so a deployed host injects the same key into its environment instead. Writing it anywhere a person would have to copy it from would carry a live token through a prompt or a terminal, which is where tokens leak. It is written by an atomic writer that keeps every other line, read back through the contract's parser, and never printed.

## `login` writes the connection entry and the token the sign-in minted

`agentrooms login` signs a person's existing account in and writes the entry and the access token in one act, as `register` does for a new agent: the entry, holding only non-secret values, through the contract's records adapter at the scope asked for, and the token into the credentials file of that scope - the project's `.env.local`, or with `--global` the user `credentials.env` - under the entry's `secret_env`, at mode 0600, by the same atomic writer, never printed.

The password the sign-in takes is read from standard input, sent once to the homeserver and kept nowhere: the token is the credential the connection resolves, and a person who would otherwise copy it out of a client would carry a live token through a terminal.

## The machine service holds each served agent's access token

A machine service loads no project's secrets: the work it starts runs as the project's child. The service this capability ships keeps one `/sync` long-poll per agent inside the one machine process, so that process holds each served agent's access token. It resolves the token inside that project's own resolution - the project's cascade, for that one key - and uses it only for that agent's requests; it loads no other value of the project's environment, and every turn still runs as a child in the project's root with the project's own context.

## `workers bind`, `workers supervisor` and the agents-map move write the project's settings

The standard keeps a setting record human-written. An agent's worker bindings and its supervisor's address - `worker`, `rooms`, `supervisor` - are the project's `binding.<connection>` setting, because they say how this project answers through that agent, and a connection held in the store is shared by every project on it. `agentrooms workers bind|unbind|supervisor` write that setting, and the service writes `worker` when it moves an earlier release's `service/settings.json` agents map into workers, through the contract's records adapter at the project scope. Where an earlier release left those keys on a connection this project declares, the write takes them off that entry and keeps every other key and its grant as they were. Neither runs under the read-only switch or inside a turn the service started.

Making a person hand-edit JSON to bind a worker would put the one write that decides who reaches an agent where a typo is silent; the verb checks that the worker exists and resolves a room alias first.
