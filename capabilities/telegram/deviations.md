# telegram — deviations

This file's sole purpose is to hold `telegram`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults, kept apart so an audit reads them as choices, not drift (DOCTRINE — *[Deviations are allowed — and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*). Each realizes the *intent* of the pattern in MTProto's terms.

## Transport is MTProto, not HTTP

The *Resilient HTTP* retry loop is realized by Telethon's MTProto transport, which manages connection, reconnection, and request pacing itself. The rate-limit analog is `FloodWaitError`, mapped to exit `5` with the wait seconds in the message; connection/RPC failures map to `5`, auth failures (`ApiIdInvalidError`, `AuthKeyUnregisteredError`) to `2`, an unresolvable chat to `3`. The literal `httpx` retry loop applies only to the Deepgram sub-call inside `transcribe`, which maps its HTTP failures onto the same taxonomy.

The intent holds: fail fast and clearly, absorb transient rate-limits, a stable exit-code contract.

## Stateful session; the secret is not a flat token

Auth is a login *ceremony*, not a header value: `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` identify the app and a persisted **session file** holds the account login. The cascade's tiers and order are preserved (flag → project `.env` → user config → process env), but what they resolve is the app id/hash and the session *path*; the session *file* is produced by `telegram login`. Per SHEBANG's stateful-CLI guidance, `help` opens with the startup protocol and `doctor` doubles as the session-health/recovery point (`doctor` exit 2 → `telegram login`).
