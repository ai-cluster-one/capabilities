# Setting up an agent's Telegram bot

How to give an agent its own Telegram channel through a bot: make the bot, give the agent a connection that holds everything the bot needs, say who it answers, and start the service. Every command, flag and setting is in `telegrambot help`; this guide is the order to do things in and why.

## What you get

The agent answers in Telegram as a bot: in a direct chat, in groups and in forum topics. Each answer is a fresh harness turn on the agent's project, which reads the recent conversation back from Telegram, so nothing keeps a copy of the chat. Longer work can be handed to a background job that reports back. Voice messages are transcribed. `/status`, `/stop`, `/profiles`, `/reload` and `/set` work in the chat for the people whose role allows them.

## 1. Make the bot

1. In Telegram, open BotFather and send `/newbot`. Choose a display name and a username ending in `bot`, and copy the token BotFather gives you.
2. For groups, decide whether the bot should see every message there or only what is meant for it. With privacy mode on (BotFather's default) a group delivers only commands, replies to the bot and mentions of it; the bot then answers only when addressed and reads no history in that group. To let it follow the whole conversation, send BotFather `/setprivacy`, choose the bot, choose Disable, and then remove the bot from each group and add it again: the change reaches a group only when the bot joins it. Making the bot an admin of a group has the same effect there.
3. For conversations between bots, turn on Bot-to-Bot in BotFather's Mini App, for both bots. Nothing can read this setting back, so `doctor` reports it as unknown.
4. Use a bot that nothing else polls. One token is polled by one process; a bot that already feeds a notification channel or a webhook cannot be served here.

## 2. Get an app id for history (once)

History is read through Telegram's MTProto with the bot's own token, which needs an app id and hash of your own. Sign in at my.telegram.org with your phone number, open API development tools, create an application (any title and short name), and copy App api_id and App api_hash.

Without them the bot still works: it answers from the message in front of it and the message it replies to, and says plainly in `doctor` and in every turn that it reads no history.

## 3. Give the agent a connection

One connection is one bot and holds everything that bot needs: the token, the api_id, the api_hash and the Deepgram key for voice. Each is a value, sealed into the agent's vault under the connection's own name, or a reference to a key the vault (or the machine) already holds. In the agent's project:

```sh
capabilities enable telegrambot --project
telegrambot connection add <id> --stdin <<< '{"secret_env": "<token>", "api_id_env": "<api_id>", "api_hash_env": "<api_hash>"}'
```

For voice, either give the Deepgram key the same way (`"deepgram_env": "<key>"`), or refer to the machine's shared one with `--env deepgram_env=DEEPGRAM_API_KEY`. A new connection is given that reference automatically where the machine holds a shared key, and the answer says so under `prefilled`. A connection that names no Deepgram key has none: no machine value stands in for it later.

The bot answers by sending, so it needs a write grant: `capabilities set telegrambot grant <id> '{"allow_write": true}'`.

Then check it:

```sh
telegrambot doctor
```

`doctor` asks Telegram for the bot and says whether privacy mode is on, whether a webhook is set (then the service cannot poll it), whether history works (the app id signs in), whether voice works (Deepgram accepts the key), and lists under `missing` what the connection lacks.

## 4. Say who the bot answers

Out of the box the bot answers nobody. A person who writes to it is ignored and recorded, so you can find them:

```sh
telegrambot seen --kind person
telegrambot seen --kind chat
```

Decide by id: a name can be anyone's. Then write the settings, at least the people the bot talks to directly and the groups it serves:

```sh
telegrambot settings set allowed_users '{"<user id>": {"name": "<name>", "role": "supervisor"}}'
telegrambot settings set allowed_groups '{"-100<chat id>": {"name": "<group>"}}'
```

`direct_messages.mode` set to `anyone` lets anybody write to the bot directly. In a group the bot answers when it is mentioned, called by one of `aliases` (or `assistant_name`), or replied to; `require_reference: false` answers every message the bot sees, and the reply gate (`gate`) lets a light model decide when an unaddressed message deserves an answer, a reaction, or nothing, first in `shadow` mode, which only logs.

Roles decide three things: which chat commands a person may run (`control.roles`), which capabilities their turn may use (`authority.roles`), and which harness profile their turn runs on (`roles.<role>.profile`). `supervisor` may run every command by default; everyone else may run `/status`, `/profiles` and `/help`. A role that names a profile runs its senders' turns on it in place of the chat's or topic's profile, and a chat's `/set profile` does not move them off it; a role that names none runs on the chat's or topic's profile. What the turn may then read, write or run is the profile's own sandbox and tools, so a read-only profile kept for ordinary group members gives their turns read-only hands while the supervisor's turns keep their own:

```sh
telegrambot settings set roles '{"group_member": {"profile": "telegrambot-<read-only profile>"}}'
```

## 5. Give the agent its voice

The bot ships a standard prompt, read fresh on every turn, so improvements to it reach every bot. The agent adds one prompt of its own over it:

```sh
telegrambot prompts put bot < bot-prompt.md
```

A group or a topic can add its own context (`context` in its settings, or `context_prompt` naming a stored `context.<name>`). `context_mode: exclusive` on a group sets aside the standard behaviour and the agent's prompt for that group; the mechanics of the channel are never set aside.

To let turns hand longer work to background jobs, set `defaults.delegation` to `allowed`, or allow it per person or group.

## 6. Start it

On a machine that runs several agents, one process serves every bot of every project that joins it:

```sh
capabilities service start telegrambot
```

That places the agent's service on this node in shared mode; the node manager adds it to the machine service's list. For a single agent, `telegrambot service run` in its project serves it alone. Check it:

```sh
telegrambot service doctor
telegrambot service status
telegrambot service logs --tail 50
```

After changing settings, connections or profiles, run `telegrambot service reload` (or send `/reload` in the chat). Prompts need nothing: they are read every turn.

## Things worth knowing

- History reaches back by message number, and a bot's direct chats share one numbering. A busy bot therefore reaches back less far for the same number of messages; each turn's channel state says how many it read and why it stopped.
- Telegram keeps a bot's undelivered updates for 24 hours. A service that was down longer misses older messages; they still show up in the history a later turn reads.
- Other bots may start at most four turns in a row in a chat before a person writes (`agent_dialogue.max_turns`), so two bots cannot talk to each other forever. A message ending in `#noreply` is never answered; a request ending in `#external` is answered with `#noreply` on everything sent for it.
- The service needs the project's store (the `AGENTKIT_DB_*` keys or `capabilities store set`): it keeps there whom the bot has seen, the chats' `/set` overrides, the agent's prompts and the background jobs - never the messages.
