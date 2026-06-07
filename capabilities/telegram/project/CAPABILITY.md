---
name: Telegram
description: The project's Telegram access — read, search, send, export, and transcribe over a personal MTProto account via the `telegram` CLI. How this project reaches its Telegram chats: list dialogs, pull or search a chat's messages, send a message, dump a chat's full history to JSON, and transcribe voice/audio.
---

# Telegram

The project's **Telegram access** — reaching its chats through the `telegram` CLI on a personal MTProto account (a full user account). List dialogs, read or search a chat, send a message, export a chat's full history to JSON with voice/audio + photos/stickers downloaded, and fill voice/audio transcription placeholders via Deepgram.

> Template note: `<namespace>` fills at install. Replace this role paragraph with how *this* project actually uses Telegram (which chats it reads or posts to, what it exports and where, whether transcription runs). Keep this file **lightweight** — role + pointers; the command surface is `telegram help`, not here.

## Interaction

Via the `telegram` CLI on `PATH`. This is a **stateful** CLI — it holds a login session — so run `telegram doctor` first (exit 2 means the session is missing or expired; recover with `telegram login`), then `telegram help` (the self-documenting source of truth for the command surface, the credential cascade, the chat-reference forms, the export JSON shape, and the exit codes). See [identifiers.md](identifiers.md) for the chat ids and handles this project addresses.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: the chat ids / usernames it reads, posts to, or exports.
- [reference.md](reference.md) — the standing home for project-specific operational context (what each chat means here, export/transcription conventions, retention notes). Ships empty as a self-describing scaffold; populated as context accrues.

> If this project runs an automated read/export/transcribe flow (which chats sync where, on what trigger), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
