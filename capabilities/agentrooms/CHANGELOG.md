# agentrooms — change log

## 2026-10-11 — The room prompt rides in the turn's system prompt

The service hands the filled room prompt (the bundled one, or the project's `capabilities/agentrooms/service/room.md`) to the harness as its system-prompt append on every turn - claude's appended system prompt, codex's developer instructions - after any `append_system_prompt` the agent's profile already carries. The turn's message now holds only the `=== This turn ===` block and the room's messages, so a resumed room session no longer gathers one copy of the room prompt per turn. The bundled room prompt says that each turn's message carries what woke the agent and the room's messages.
