# agentrooms — change log

## 2026-10-11 — Workers admit and limit an agent's turns

An agent's turns are now admitted by a worker: a Markdown document - front matter settings, body the role - kept in the project's store as versions (`agentrooms workers save|show|list|history|remove`) and bound to the agent's connection, or to one room, with `workers bind`. The worker names the profile, who may wake and invite the agent, which signals wake it, a debounce, a turn timeout, turn limits per agent, per sender and per room (a room's count resets after a quiet period), and `before`/`after` hooks on the tasks hook contract. Its body reaches the turn after the capability's room frame in the system prompt. Profiles resolve from the store tasks reads, then the library's and this capability's shipped ones. An agent no worker is bound to, and a sender no worker admits, get the shipped `default` worker's fixed answer - contact the agent's supervisor (`workers supervisor`) - once per sender per room per day, with no harness turn and a mark the service never wakes on. Every refusal is logged with its reason.

This replaces the `service/settings.json` agents map: the service serves every connection the project declares and grants writes, and moves an existing map into one worker per agent that admits every sender, bound on its connection, removing the file (`workers import` shows the move). `service init` no longer writes project settings, a project's `room.md` no longer replaces the room frame, and profile files in the project's `service/profiles/` are no longer read.

## 2026-10-11 — A message posted before the agent joined wakes it once it joins

A mention, a direct message or a message in a marked room posted after an agent was invited but before it joined now wakes the agent once, when the service sees its join. The homeserver starts the first timeline after a join at the join itself, so the service reads the room back from there to the agent's invite and wakes on what it finds; nothing from before the invite wakes, and a message found both there and in the timeline wakes once.

## 2026-10-11 — The room prompt rides in the turn's system prompt

The service hands the filled room prompt (the bundled one, or the project's `capabilities/agentrooms/service/room.md`) to the harness as its system-prompt append on every turn - claude's appended system prompt, codex's developer instructions - after any `append_system_prompt` the agent's profile already carries. The turn's message now holds only the `=== This turn ===` block and the room's messages, so a resumed room session no longer gathers one copy of the room prompt per turn. The bundled room prompt says that each turn's message carries what woke the agent and the room's messages.
