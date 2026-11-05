# telegrambot — change log

## 2026-11-05 — History reads a supergroup the bot is in

`history`, and a turn's read back, refused every supergroup with `chat_unreachable (ChannelInvalidError)`: the read named the channel with access hash 0, which `channels.getMessages` does not accept. The reader now asks `channels.getChannels` for the channel first, which accepts hash 0 from a bot that is a member, takes the access hash Telegram answers with, and keeps it for as long as the reader lives, so a long-running service asks once per supergroup. A supergroup the bot is not in is still refused as `chat_unreachable`; private chats and basic groups read as before.

## 2026-11-04 — An agent's own Telegram channel through a bot

New capability. One connection is one bot and holds everything that bot needs - the BotFather token, the App api_id and api_hash that let it read history, and the Deepgram key for voice - each as a value sealed into the agent's vault or a reference to a key held there, so a connection is whole on any machine. A new connection refers to the machine's shared `DEEPGRAM_API_KEY` where the machine holds one; nothing else stands in for a key the connection does not name.

New messages arrive and every write leaves over the HTTP Bot API; history is read back from Telegram by message number through an MTProto session the bot's own token signs in. Nothing keeps a copy of the conversation. `doctor` reports the bot, its privacy mode, a webhook that would stop polling, history readiness and voice readiness, and names what a connection lacks. `seen` lists the chats, topics and people the bot has seen; `history`, `download`, `send` and `react` act on one chat; `prompts` keeps the agent's own prompt over the shipped standard; `profiles` and `jobs` reach the harness profiles and the background work.

The bundled service runs once per machine for every project that joins it, or in one project: one getUpdates poll per bot, direct chats, groups and forum topics, one person's messages within the debounce as one turn, roles for commands and for tools, harness profiles, background jobs, the reply gate in shadow or live mode, voice transcription with a per-chat echo switch, `/status`, `/stop`, `/stop N`, `/profiles`, `/reload` and `/set`, and a bound on turns other bots start in a row, on by default. Its state is the project's store: `telegrambot_channels`, `telegrambot_seen`, `telegrambot_prompts` and the job tables.
