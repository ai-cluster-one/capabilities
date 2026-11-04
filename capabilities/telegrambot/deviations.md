# telegrambot deviations

This file's sole purpose is to hold `telegrambot`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults and the [DOCTRINE](../../DOCTRINE.md), kept apart so an audit reads them as choices, not drift (DOCTRINE - *[Deviations are allowed - and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## The machine service holds each served bot's secrets

A machine service loads no project's secrets: the work it starts runs as the project's child. The service this capability ships keeps one getUpdates poll per bot inside the one machine process, and reads each bot's history through an MTProto session in that process, so the process holds each served bot's token, its api_id and api_hash, and its Deepgram key. It resolves them inside that project's own resolution - the project's cascade, for the keys that bot's connection names - and uses them only for that bot; it loads no other value of the project's environment, and every turn and job still runs as a child in the project's root with the project's own context.

A bot has no other way to be served: Telegram delivers a bot's updates to one poller, and a poller per project would be the process per agent the machine mode exists to replace.
