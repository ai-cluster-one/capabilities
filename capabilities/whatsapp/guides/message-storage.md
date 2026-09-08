# WhatsApp — message storage

How a WhatsApp history is kept, and what a consuming project reads.

Two places hold messages, and the split between them is the point. The **store** is where capture writes: one database per account under the user state home, holding every chat and message the account has handed over, the material that lets an attachment be fetched later, and the enrichment derived from them. The **export** is what a project reads: a folder per conversation, written on request, holding the messages a project registered and the attachments that came with them.

## The store

The store is the capability's own, git-ignored, and never edited by hand. It is user-scoped, because it is minted by one human's linked device, so several projects consuming the same account share one capture rather than each building their own.

Three properties of it are worth knowing as a consumer:

- **Raw history chunks are written to disk before anything parses them.** A parsing fault therefore costs a re-ingest and never the data.
- **Enrichment lives beside the messages, never inside them.** A transcript, a downloaded attachment's path, and the loss of one that can no longer be fetched are all keyed to the message they belong to, so nothing the protocol wrote is ever overwritten by something derived.
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
