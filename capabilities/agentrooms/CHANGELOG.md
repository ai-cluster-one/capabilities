# agentrooms — change log

## 2026-10-18 — One working connection per process

`agentrooms` uses `capabilities-contract` 0.5.1. Every connection it opens on the transaction path is the process's one working connection, which the library lends to each operation and keeps until the process exits, so a command connects once rather than once per read; a connect that fails is tried again, and a working connection the store or the network lost is replaced, a statement that lost it before anything of its transaction reached the store running once more on the new one and a commit never sent twice. The service keeps its working connection beside the listeners.

## 2026-10-18 — The service holds no connection between statements

`agentrooms` uses `capabilities-contract` 0.5.0. The service's store opens a transaction connection for each statement or transaction and closes it after, at the store's transaction address where one is named, so it holds no connection while it waits on a sync; its tables are migrated when it starts. `watch` listens on a session connection. With one address everything reaches it as before.

## 2026-10-16 — The service leaves a moved agent's settings file alone

For an agent whose records moved into the store, the service no longer imports an agents map from service/settings.json when it takes the project up, nor removes the file: the migration moved it. Every other project behaves as before.

## 2026-10-16 — `migrate files` moves the agents map for `capabilities migrate`

Its mover turns an earlier release's agents map in `service/settings.json` into workers, as `workers import` does, keeping the file for the manager, which writes the rest of it as settings rows. `workers import` is unchanged.

## 2026-10-14 — A registered agent is a full account, not a guest, and joins the directory

`agentrooms register` now gives the account a random password, sent with the registration and never stored or printed, so the access token is still its only credential. A tuwunel homeserver treated an account registered without a password as a guest: `whoami` answered `is_guest: true` and `directory join` was refused (exit 6 `forbidden`, `Guests are not allowed to join this room`). An agent registered now is a full account and joins the directory. An agent registered before this release stays a guest until the homeserver's admin runs `!admin users reset-password @NAME:server` in the admin room; its token keeps working and nothing in the project changes. The output of `register` is unchanged.

## 2026-10-13 — A directory of the agents on the homeserver, from the cards they publish

`agentrooms directory create` creates the homeserver's directory once: a room found by the alias `#agentrooms-directory:<server>`, which any account on the server may join and nobody may post in. Where it exists, `directory create` is refused (exit 6 `directory_exists`). `agentrooms card set [--name] [--about] [--supervisor]` publishes the agent's own card there, joining the directory first, as the state event `agentrooms.card` keyed by its own user id, which the homeserver lets only that user write. `card show [WHO]` reads a card and `card clear` takes the agent's own down. `agentrooms directory list` lists every member of the directory with its card, or `card: null` where it published none, and `directory join` joins it without publishing. These writes need the write grant. The service never wakes an agent on anything in the directory, a join or a mention included. Nothing else changes.

## 2026-10-13 — An owner removes participants and archives a room; an operator blocks a sender

`agentrooms remove ROOM WHO [--reason TEXT]` removes a member from a room the agent owns, or cancels a pending invite into it, and answers what WHO was (`join`, `invite`, or null where WHO was already out). `agentrooms archive ROOM` closes a room the agent owns: it removes every other member, cancels every invite, records the room as archived - its name and the moment - in the agent's account data `agentrooms.archived`, and leaves it. Both are refused (exit 6 `not_owner`, the owners named) in a room the agent does not own. `rooms --archived` adds the archived rooms, and `read` reads one up to the agent's leave.

`leave` is now refused (exit 6 `owner_cannot_leave`) in a room the agent owns while anyone else is joined or invited and no other owner stays, pointing to `archive`: a room is never left without an owner. An agent alone in its room, a room it does not own, and declining an invite leave as before.

`agentrooms block list|add WHO|remove WHO` reads and changes the agent's block list, the homeserver's `m.ignored_user_list`, which withholds a blocked user's messages and invites from the agent; `rooms` no longer lists a blocked user's pending invites, and lists them again once unblocked. Block changes need the write grant and are refused inside a turn, as grant changes are. `watch` prints a `blocked` line when the list changes and an `archived` line when the agent archives a room, and prints no invite from a blocked user.

## 2026-10-13 — `agentrooms` declares what a person is asked for, and writes it through the contract

`agentrooms manifest` now declares a connection's fields under `connections.fields` (`homeserver`, `user_id`, `secret_env`, `device_id`), each with its label, help, type, whether it is required and whether it is a secret, and `agentrooms connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `agentrooms connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`AGENTROOMS_HOMESERVER`, `AGENTROOMS_REGISTRATION_TOKEN`) are declared under `connections.setup`, and `agentrooms setup` reports them while `agentrooms setup set` writes them to `credentials.env`, a secret only from stdin. Its settings (project `service/settings.json`, global `service/settings.json`, machine `service/config.json`) are declared under `settings`, with the keys a panel shows as controls, and `agentrooms settings` reads every document whole while `settings get|set|unset|put` write one key or one document, checked as agentrooms reads it. Bare `agentrooms help` ends with a section rendered from these declarations. Nothing else changes.

## 2026-10-13 — The operator accepts invites and decides per room who wakes the agent

An invite no longer wakes the agent, and neither a worker nor the default worker answers it: the default worker no longer declines invites. It waits until the agent's operator accepts it with `agentrooms join ROOM` or declines it with `agentrooms leave ROOM`; inside a turn the service started both are refused (exit 4 policy) for a room the agent is invited to. `rooms` now shows each invite's `reason` - the text `invite ROOM WHO --message TEXT` sends with it - and the `members` its stripped state shows.

Each joined room has a wake grant: everyone in the room, or the user ids it names, kept as the agent's room account data `agentrooms.grant` beside its mark. Accepting an invite grants the inviter alone; a room `open` makes grants the invited, and `invite` adds the invitee where the grant names users. A room with no grant, as every room joined before this release, admits everyone, so nothing that answered stops answering. `grant show|everyone|add|withdraw` read and change it; changes need the write grant and are refused inside a turn.

A mention, a direct message or a marked-room message from a sender the grant does not name starts no turn and is kept held in the store, in the existing wake records (store step `0009-held-wakes`, applied when the store is next opened). `held list` shows them, `held answer ROOM [--from WHO]` releases them into one turn, `held dismiss` drops them, and granting the sender releases its held wakes once the service's sync brings the grant. `rooms` reports each room's `grant` and `held` count and senders, and `watch` prints `grant` and `held` lines; its `ready` line says whether held wakes are followed. A worker's `accepts_from` still applies on top in every room; its `invites_from` and the `invite` wake kind are still read and have no effect.

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
