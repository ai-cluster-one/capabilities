# Telegram — identifiers

All fixed, **non-secret structural** values for this project's Telegram setup: the chats it addresses, by id and handle. Pure lookup; the operating model and command surface are in `telegram help`.

> Template note: fill these at install by discovering them against this project's own account. With the session resolved, `telegram chats --limit N` lists dialogs with their numeric ids; record the durable ones this project reads, posts to, or exports. Leave the rows the project doesn't use as breadcrumbs. **No secrets here** — the app id/hash, the Deepgram key, and the session file live in env / the user-config dir, never in this file.

## Chats

A `<chat>` is addressable by `@username`, `+phone`, numeric id, or a title substring (`telegram help` documents the forms). Record the durable role each one plays for this project; prefer the numeric id for stability (a username can change).

| Role | Handle / id |
|---|---|
| `<the chat this project reads>` | `<@username or numeric id>` |
| `<the chat this project posts to>` | `<@username or numeric id>` |
| `<the chat this project exports>` | `<numeric id>` |

## Connection (values in env, never here)

The app credentials and Deepgram key resolve through the credential cascade `telegram help` documents; the login session is the file Telethon writes from the `TELEGRAM_SESSION` path (default, no extension: `~/.config/telegram/session`). Only non-secret chat ids/handles are structural and belong in this file.

| Env var | Holds | Where |
|---|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | the Telegram app credentials | project `.env` or `~/.config/telegram/credentials.env` |
| `DEEPGRAM_API_KEY` | Deepgram key (only for `telegram transcribe`) | project `.env` or `~/.config/telegram/credentials.env` |
| `TELEGRAM_SESSION` | session-file path (optional; defaults to `~/.config/telegram/session`) | project `.env` or `~/.config/telegram/credentials.env` |

Global tool: `telegram` on `PATH` (`telegram help` is the source of truth for the CLI surface).
