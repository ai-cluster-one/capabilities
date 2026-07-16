# WhatsApp — message storage

How a consuming project stores WhatsApp histories exported through the `whatsapp` CLI.

The CLI reads the store root from the selected connection's `messages_dir`. That value may be absolute, or relative to the consuming project's `capabilities/whatsapp/` folder. The default project pattern is to set `messages_dir` to `messages`, which resolves to `capabilities/whatsapp/messages/`.

## Layout

Use one folder per conversation, keyed by a stable kebab-case slug of the chat name, falling back to the chat id when there is no useful name:

```text
capabilities/whatsapp/messages/
└── <slug>/
    ├── messages.json
    └── media/
```

`messages.json` is the exported history. `media/` holds attachments, one file per message id, referenced from the JSON by path and never inlined.

## Git Policy

The message store is data, not definition: personal history and media, mutable and potentially sensitive. Git-ignore exported histories and media. Commit the definition around it instead: connection wiring, identifiers, and any project reference explaining why a conversation is registered or where the store root is.

## Sync Policy

Use `whatsapp export` to sync a conversation into the store. Re-runs are idempotent: existing transcriptions and already-downloaded media are preserved. Use `whatsapp transcribe` for voice/audio after export, and `whatsapp render` when a readable markdown log is needed.
