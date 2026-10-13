# agentrooms — change log

## 2026-10-13 — An agent registers on a homeserver whose registration is open, without a token

`agentrooms register` now asks the homeserver which registration flows it offers before it looks for a registration token. A homeserver whose registration is open registers the agent at once or through `m.login.dummy`, and `AGENTROOMS_REGISTRATION_TOKEN` need not be set. Only a homeserver that asks for `m.login.registration_token` needs the token, and it registers exactly as before; without the token it still exits 2 `registration_token_missing`, now after the handshake, which creates no account. A homeserver offering neither flow exits 6 `registration_unsupported` as before.

## 2026-10-13 — A worker binds to an agent whose connection is held in the store

`agentrooms workers bind|unbind|supervisor` now write the agent's binding - `worker`, `rooms`, `supervisor` - into the project's `binding.<connection>` setting in `capabilities/agentrooms/service/settings.json`, and the service reads it from there. A project whose connections are held in the store could not bind at all: the binding was written onto the connection, which the store shares between projects, and was refused as `connection_not_in_project`. A binding an earlier release wrote onto a connection the project declares still holds, and the next bind, unbind or supervisor write moves it into the setting and takes it off the connection. Moving an agents map into workers binds each worker the same way, so the settings file stays, holding the bindings.

## 2026-10-13 — Sessions and leases are held under the node's declared name

The node the service records its sessions and leases under is the node's name as the contract gives it: the value of `AGENT_NODE_NAME` where the machine declares one, and its host name as before where it declares none. A container node that declares its name holds the same leases after a redeploy, when its host name, the container id, changes.

## 2026-10-13 — A person's sign-in, a live stream, each room's owners, and the session behind an answer

`agentrooms login USER_ID` signs in to an existing account with its password, read from standard input, and writes the connection and the access token it minted - into the project, or with `--global` into `~/.config/agentrooms/` as the machine's connection. The password is sent once and never stored or printed. Outside any project `connections`, `doctor`, `rooms`, `read` and `watch` are now machine reads: they use the machine's connections read-only, and posting as that account still needs a project that grants the connection writes.

`agentrooms watch [--room ROOM ...]` follows the rooms a connection sees from the moment it starts and prints one JSON line per message, invite, membership change, read receipt, change of the connection's mark, and joined or changed room, until it is stopped; a dropped connection is resumed where it stopped.

`rooms`, and `watch`'s room lines, name each joined room's `owners` and what that rests on, `owner_basis`: its creators in room version 12 and later, else the users holding the highest power level.

The service now records, on each turn, the message it posted and the harness session that wrote it (store step `0008-turn-sessions`, applied when the store is next opened). `agentrooms turn EVENT` reads the turn behind a message, and `agentrooms sessions [--room ROOM]` the room-to-session map of the selected agent. A message answered before this version has no record, and `turn` answers null for it.

## 2026-10-11 — A store behind Supabase's transaction pooler is refused at connect

`agentrooms` now uses `capabilities-contract` 0.4.1. A transaction-mode pooler keeps no session between transactions, so the schema binding, prepared statements and notifications a connection relies on are lost through one, and it is not supported. A database setting that points at Supabase's transaction pooler - a host under `pooler.supabase.com` on port 6543 - is refused before anything is sent to it, as `transaction_pooler`, with a hint naming the session pooler: the same host on port 5432. Every other database, the session pooler included, is reached as before.

## 2026-10-11 — Workers admit and limit an agent's turns

An agent's turns are now admitted by a worker: a Markdown document - front matter settings, body the role - kept in the project's store as versions (`agentrooms workers save|show|list|history|remove`) and bound to the agent's connection, or to one room, with `workers bind`. The worker names the profile, who may wake and invite the agent, which signals wake it, a debounce, a turn timeout, turn limits per agent, per sender and per room (a room's count resets after a quiet period), and `before`/`after` hooks on the tasks hook contract. Its body reaches the turn after the capability's room frame in the system prompt. Profiles resolve from the store tasks reads, then the library's and this capability's shipped ones. An agent no worker is bound to, and a sender no worker admits, get the shipped `default` worker's fixed answer - contact the agent's supervisor (`workers supervisor`) - once per sender per room per day, with no harness turn and a mark the service never wakes on. Every refusal is logged with its reason.

This replaces the `service/settings.json` agents map: the service serves every connection the project declares and grants writes, and moves an existing map into one worker per agent that admits every sender, bound on its connection, removing the file (`workers import` shows the move). `service init` no longer writes project settings, a project's `room.md` no longer replaces the room frame, and profile files in the project's `service/profiles/` are no longer read.

## 2026-10-11 — A message posted before the agent joined wakes it once it joins

A mention, a direct message or a message in a marked room posted after an agent was invited but before it joined now wakes the agent once, when the service sees its join. The homeserver starts the first timeline after a join at the join itself, so the service reads the room back from there to the agent's invite and wakes on what it finds; nothing from before the invite wakes, and a message found both there and in the timeline wakes once.

## 2026-10-11 — The room prompt rides in the turn's system prompt

The service hands the filled room prompt (the bundled one, or the project's `capabilities/agentrooms/service/room.md`) to the harness as its system-prompt append on every turn - claude's appended system prompt, codex's developer instructions - after any `append_system_prompt` the agent's profile already carries. The turn's message now holds only the `=== This turn ===` block and the room's messages, so a resumed room session no longer gathers one copy of the room prompt per turn. The bundled room prompt says that each turn's message carries what woke the agent and the room's messages.
