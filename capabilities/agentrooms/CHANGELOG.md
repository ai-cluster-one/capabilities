# agentrooms — change log

## 2026-10-11 — A message posted before the agent joined wakes it once it joins

A mention, a direct message or a message in a marked room posted after an agent was invited but before it joined now wakes the agent once, when the service sees its join. The homeserver starts the first timeline after a join at the join itself, so the service reads the room back from there to the agent's invite and wakes on what it finds; nothing from before the invite wakes, and a message found both there and in the timeline wakes once.

## 2026-10-11 — The room prompt rides in the turn's system prompt

The service hands the filled room prompt (the bundled one, or the project's `capabilities/agentrooms/service/room.md`) to the harness as its system-prompt append on every turn - claude's appended system prompt, codex's developer instructions - after any `append_system_prompt` the agent's profile already carries. The turn's message now holds only the `=== This turn ===` block and the room's messages, so a resumed room session no longer gathers one copy of the room prompt per turn. The bundled room prompt says that each turn's message carries what woke the agent and the room's messages.
