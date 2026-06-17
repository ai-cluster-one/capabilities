# telegram — deviations

This file's sole purpose is to hold `telegram`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults, kept apart so an audit reads them as choices, not drift (DOCTRINE — *[Deviations are allowed — and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*). Each realizes the *intent* of the pattern in MTProto's terms.

## Transport is MTProto, not HTTP

The *Resilient HTTP* retry loop is realized by Telethon's MTProto transport, which manages connection, reconnection, and request pacing itself. The rate-limit analog is `FloodWaitError`, mapped to exit `5` with the wait seconds in the message; connection/RPC failures map to `5`, auth failures (`ApiIdInvalidError`, `AuthKeyUnregisteredError`) to `2`, an unresolvable chat to `3`. The literal `httpx` retry loop applies only to the Deepgram sub-call inside `transcribe`, which maps its HTTP failures onto the same taxonomy.

The intent holds: fail fast and clearly, absorb transient rate-limits, a stable exit-code contract.

## Stateful session; the secret is not a flat token

Auth is a login *ceremony*, not a header value. A connection identifies the app — `api_id` literally, the app hash by `secret_env` indirection (the implicit default rides the bare `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` cascade) — and a persisted **session file** holds the account login. So a connection resolves the app id/hash and a session *path*; the session *file* itself is produced by `telegram login`, not by any cascade tier. `telegram login --qr` is the same ceremony through Telethon's QR login token: it still needs the app id/hash and still persists the same session file, but the user approves it from an already-authorized Telegram app instead of entering an SMS/app code. Per SHEBANG's stateful-CLI guidance, `help` opens with the startup protocol and `doctor` doubles as the session-health/recovery point (`doctor` exit 2 → `telegram login` or `telegram login --qr`).

## Per-connection sessions; the implicit default's is un-keyed

A named connection keys its session per id at `<state-dir>/<id>/session`, per the standard, so two accounts never share a login. The **implicit default** connection keeps its session at the un-keyed `$XDG_STATE_HOME/telegram/session` (with a `~/.config/telegram/session.session` fallback an existing login already occupies) rather than `<state-dir>/default/session` — login continuity for the sole-account case, where inserting the `default/` segment would orphan a session already on disk. `--session` / `TELEGRAM_SESSION` is the one-shot path override for either.

Re-authenticating a login anywhere (another host, a phone sign-in) rotates the account session and can invalidate the prior file — one concurrent actor per login. Keying per connection isolates distinct accounts; it does not divide one account across hosts.
