---
name: telegram
description: Telegram CLI — drive a personal Telegram account over MTProto (a full user account): list dialogs, read and search a chat, send messages, export a chat's full history to JSON (downloading voice/audio + photos/stickers), and transcribe voice/audio via Deepgram. Stateful — holds a login session. Run `telegram doctor` first, then `telegram help` for the full command surface before the first subcommand in a session.
---

Telegram CLI — read, search, send, export, and transcribe over a personal MTProto account.

- Executable: `telegram` (on `PATH`)
- Credentials: `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` (app from my.telegram.org/apps) in `~/.config/telegram/credentials.env` or a project `.env`, plus a login session created once by `telegram login`. `DEEPGRAM_API_KEY` only for `telegram transcribe`.
- Stateful: this CLI holds a login session. Run `telegram doctor` first — exit 2 means the session is missing or expired, recover with `telegram login`. Load the full reference with `telegram help`.

Run `telegram doctor` before issuing any subcommand the first time in a session, then `telegram help` — the command surface, the credential cascade, the chat-reference forms, the export JSON shape, and the exit codes all live there. Project-scoped chat ids / handles live in each project's own identifiers, not here.
