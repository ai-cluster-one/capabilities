# WhatsApp — message storage

How a WhatsApp history is kept, and what a consuming project reads.

Two places hold messages, and the split between them is the point. The **store** is where capture writes: the project's Postgres database, holding every chat and message each account has handed over, the material that lets an attachment be fetched later, and the enrichment derived from them. The **export** is what a project reads: a folder per conversation, written on request, holding the messages a project registered and the attachments that came with them.

## The store

The store is the Postgres database the project resolves: the `AGENTKIT_DB_*` keys in the project's `.env.local` / `.env`, else those in the process environment, else the machine's store setting (`capabilities store set` writes it); `capabilities store show` shows which one answers. The capability keeps its capture there in tables of its own, every one named `whatsapp_*` and created on first use, and they are never edited by hand. Without a database there is nowhere to capture to, so every verb that reads or writes the capture refuses as a configuration error; `help` and `contract` still answer.

The capture is keyed by the account, not by the project or the connection, because it is minted by one human's linked device: several projects on one database consuming the same account share one capture rather than each building their own, and two accounts never mix.

The user state home keeps what is not rows: the engine's own login session, the raw history chunks, and the attachments fetched so far, in a folder named for the account.

An account captured before the store moved to Postgres has its earlier history in a `messages.db` file in that same folder, which no verb reads. `whatsapp import-legacy` carries it into the account's rows once: chats, messages, identities, enrichment, chunk records and meta. It opens the file read-only and leaves it, and the raw chunks beside it, exactly where they are. Where the store already holds a row, the store's row stands, because it is the newer record; identities and enrichment are merged, filling only what the store lacks. Running it again imports nothing, so a second run is harmless, but there is no reason for one: after the first, the file is a historical copy.

A message the account sends through this capability is a row there too, written before it is sent and carrying its delivery (`pending`, `sending`, `sent` or `failed`); the sending guide holds that model. Reads report what WhatsApp has, so a row not yet sent, or never sent, is left out of them.

Three properties of the store are worth knowing as a consumer:

- **Raw history chunks are written to disk before anything parses them.** A parsing fault therefore costs a re-ingest from them and never the data.
- **Enrichment lives beside the messages, never inside them.** A transcript, a downloaded attachment's path, and the loss of one that can no longer be fetched are all keyed to the message they belong to, so nothing the protocol wrote is ever overwritten by something derived. `whatsapp transcribe` and the assistant service's voice notes write it in one shape, a failed transcription recorded as the transcript's error, and the read verbs and exports show it as the message's `transcription`.
- **Later news never erases earlier capture.** Reaching back covers ground already held, so a field is filled in where it was empty and left alone where it was not.

## The export

An export is one folder per conversation, keyed by a stable kebab-case slug of the chat name and falling back to the chat id when there is no useful name:

```text
<messages_dir>/
└── <slug>/
    ├── messages.json
    └── media/
```

`messages.json` holds the conversation and its metadata; `media/` holds the attachments, one file per message id, referenced from the JSON by path and never inlined. The export folder is self-contained: an attachment cached by the store is copied beside the JSON, so the folder can be moved or read on its own.

The message envelope inside it is the capability's published output contract, identical to what the read verbs emit; run `whatsapp help` for its shape.

Where an export root sits is a per-connection choice: absolute, or relative to the consuming project's own capability folder. Only the root moves; the structure beneath it is the same wherever it points.

## Git policy

An export is data, not definition: personal history and media, mutable and potentially sensitive. Git-ignore the exported conversations and their media. Commit the definition around them instead — the connection wiring, the identifiers, and any project reference explaining why a conversation is registered or where its root points.

## Keeping an export current

Re-running an export over a conversation is idempotent: transcriptions already paid for are adopted into the store rather than redone, attachments already fetched are not fetched again, and one already established as gone is not retried. A conversation the store cannot yet cover is deepened as part of the same read, so a wider window is asked for rather than arranged.

Messages an export holds that the store does not are carried forward rather than dropped, so an export never loses history it once had.
