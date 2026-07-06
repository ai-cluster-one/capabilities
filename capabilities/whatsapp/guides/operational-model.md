# WhatsApp — WAHA/GOWS operational model

How the WhatsApp capability consumes a self-hosted WAHA bridge.

A connection represents one WhatsApp identity: a WAHA instance URL, session name, optional number, expected engine/tier, message-store root, and secret-env name for the X-Api-Key. Reads run through that identity. Sending is off by default and remains gated by `allow_write`; the current CLI surface treats sending as planned.

## GOWS Engine

The preferred engine is GOWS: WAHA backed by whatsmeow, not a browser session. Credentials persist server-side, so a linked session reconnects without a QR scan in the normal path. A fresh link can request history sync, bounded by the configured days limit and by what the phone still retains.

History depth is a property of the linked session and its backfill. It is not something each read can extend on demand. To reach further back than a session currently holds, re-pair with a larger days limit; there is no live "fetch older" call in the CLI.

## Pairing And Re-Sync

To link or re-link:

1. Ensure the WAHA instance default engine is GOWS.
2. Create/start the session.
3. Pair with a phone-number code, or with the QR path when that is explicitly chosen.
4. Wait until the session is linked/working, then run `whatsapp doctor`.

Phone-number pairing is entered on the phone under Linked Devices -> Link with phone number. Recreating a session is destructive for that session's current server-side state, so use `--recreate --yes` only when re-linking is intentional.

## Scaling Identities

The connection abstraction is identity-agnostic. A second WhatsApp identity can be another WAHA session on the same instance when the WAHA tier supports concurrent sessions, or a separate WAHA instance when isolation or free-tier limits make that cleaner. Adding one identity is a connection-registry edit plus the matching secret in the env cascade.
