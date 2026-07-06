# Telegram — message storage and media exports

How a consuming project stores Telegram history exported through the `telegram` CLI.

Registered chats belong in the consuming project's Telegram identifiers envelope: a stable label, chat id, slug, title, and role. Exported history is data, not definition, and should live under the Telegram capability envelope, not in a general assets bucket.

## Layout

Use one folder per chat, keyed by a stable slug:

```text
.capabilities/telegram/messages/
└── <slug>/
    ├── messages.json
    └── media/
```

`messages.json` is the exported history. `media/` holds downloaded voice/audio and any selected media files, referenced from the JSON by path.

The message store is mutable, personal, and potentially large or sensitive. Git-ignore the exported JSON and media. Commit only the connection wiring, identifiers, and any project reference that explains why a specific chat is registered.

## Export And Transcription

Use `telegram export` to materialize a chat history into the chosen folder. Use `telegram transcribe` on that JSON when voice/audio needs text. The CLI owns the export schema and the transcription placeholders; project docs should not duplicate that schema.

For a one-off attachment during live triage, prefer `telegram download <chat> <msgid>` over exporting a whole chat. It materializes one message's media to the deterministic per-connection cache and prints the local path.

## Live Triage Tail

A Telegram door may pass a recent chat tail to a reasoning turn as text plus resolved sender identity. If it marks media as `[attachment: <name> | msg <id>]`, the producer uses the message id with `telegram download` only when that attachment is part of the work item. The daemon does not need to pre-download every attachment.
