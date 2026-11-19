# Operational model

`slacker` reads one Slack workspace as the person who links their own account, the way the `whatsapp` and `telegram` capabilities read as their vendor's own client. It lists the conversations that account can already see, reads their history and threads, keeps them in the project's Postgres store, syncs them forward, and captures them live. On a connection that grants writes it also writes as that person does in their own client: it sends, replies, edits, deletes, reacts, uploads, marks read and opens a DM.

## What it is

A Slack web client holds a session as an `xoxc-` token for one workspace plus a `d` cookie shared across the person's workspaces. A program holding that pair is, to Slack, one more of the person's own browser sessions. `slacker` uses the pair exactly as the browser does, over the public Web API at `https://slack.com/api/<method>`, with the token as a bearer and the cookie beside it. There is no app to install, no admin approval, no bot, and no OAuth token. The session is the person's own; this capability adds no concealment, no monitoring bypass, and no client-identity spoofing.

## What it reads

The read verbs:

- `doctor` / `whoami` — prove the session (`auth.test`), the linked user, and the workspace, and check the connection's `team_id` / `expect_user` bindings.
- `conversations` — list what the account can see: direct messages (`im`), group direct messages (`mpim`), and the public and private channels it belongs to.
- `history <channel>` — read a conversation, answering from the store and fetching from Slack only what the store cannot cover.
- `thread <channel> <thread_ts>` — read a thread's replies, from the store once it holds every reply the parent counts, otherwise from Slack.
- `search <query>` — search message text across every conversation the account can see, through Slack's own search (`search.messages`), so it finds messages the store has never captured and needs no store or running capture. Each match carries its `channel`, `ts` and `thread_ts`, which `history` and `thread` read; no match is a `count` of 0 at exit 0, while an invalid session, a refused read or an unreachable Slack exits non-zero.
- `sync` — bring the store forward incrementally for every member conversation, or one named channel.
- `service run` — hold the session's event stream and capture live; where its settings admit anyone, also answer as the assistant (see below).

## What it writes

The write verbs act as the person acts in their own Slack client, through the same Web API methods the web client calls, over the same session:

- `send <channel> <text>` — send a message to a DM, group DM or channel (`chat.postMessage`); `--thread <ts>` replies in that thread and `--broadcast` also posts the reply to the channel. Text `-` is read from stdin.
- `edit <channel> <ts> <text>` and `delete <channel> <ts>` — edit or delete one of the account's own messages (`chat.update`, `chat.delete`).
- `react` and `unreact <channel> <ts> <emoji>` — add or remove the account's reaction (`reactions.add`, `reactions.remove`); asking for what already holds answers `changed: false`.
- `upload <channel> <path>` — upload a file into the conversation, or a thread with `--thread`, posted with `--comment` (Slack's external upload: an upload URL, the bytes, then the completion into the conversation).
- `mark <channel> [<ts>]` — mark the conversation read up to `ts`, by default its newest message (`conversations.mark`).
- `open <user> [<user> ...]` — open the DM with one user, or the group DM with several (`conversations.open`), and answer its channel id.

Every write verb is in `WRITE_VERBS`, and the contract's write gate refuses it with exit 4 on a connection that does not grant writes. A connection grants nothing by default (`allow_write: false`); a person grants one with `capabilities set slacker grant <connection> '{"allow_write": true}'` inside the project. A write Slack itself refuses — a message that is not the account's own, an archived conversation, an unknown emoji — exits 6 naming Slack's reason.

What a write changes lands in the store the way the live capture records the same act arriving as an event: a sent message or reply is a `live` row keyed by its `ts`, and a reply raises its stored parent's reply count; an edit moves the stored text and edited time, keeping the message's thread and reactions; a deletion sets the soft `deleted` flag; a reaction moves the message's reactions map; an opened conversation is a conversation row. A read mark is not captured, so `mark` writes nothing to the store. When the capture service is running it also receives the event, and the `ts` key means the row is never doubled.

Nothing here sets presence, joins a channel, or presents the session as another client.

## The store

Captured data lives in the project's Postgres database, in `slacker_*` tables keyed by the workspace account (its team id), created and migrated on first use. The capture belongs to the account, not to one project: an account has one history in a database, whichever project on the machine reads it, so the tables carry no project id — the same choice the `whatsapp` capability's capture makes, and for the same reason.

- `slacker_conversations` — one row per conversation, its kind, membership, latest activity, the incremental-sync cursor (`synced_oldest` / `synced_newest`), and the open gap of a sync from the cursor that was cut short (`gap_newest` / `gap_latest`).
- `slacker_messages` — one row per message, keyed by `(account, channel_id, ts)`; the Slack `ts` is the natural id and the dedup key. A deletion is a soft `deleted` flag; reactions are a `{name: [user_id]}` jsonb; the whole original object is kept in `raw` so a parser fix can rebuild the typed columns.
- `slacker_users` — users seen in the capture, resolved from `users.info` on demand.
- `slacker_meta` — the schema version and the linked identity.
- `slacker_register`, `slacker_jobs`, `slacker_job_amendments`, `slacker_job_slots`, `slacker_prompts` — the assistant service's own rows, keyed by the agent's id (see the `assistant-service` guide); a message the service posted is marked `sent_by_service`, and an audio clip's words are kept in `transcript`.

## Incremental sync and live capture

`sync` fetches, per conversation, every message newer than the stored `synced_newest` cursor, however many, and writes them deduplicated on `ts`. `--limit` bounds only how far back a conversation's first sync, or `--full`, reaches. The cursor moves only over a range that joins what the store already covers: a fetch that stops short of it — a limited `history --fresh`, a `--full` cut by its limit, a sync interrupted by a rate limit — leaves the cursor where it was, so the next sync still asks for every message it did not reach. A sync from the cursor that is cut short keeps the part it fetched as an open gap (`gap_newest` / `gap_latest` on the conversation), and the next sync resumes below that part rather than starting again from the newest message, so a gap deeper than one round's rate budget still closes over successive rounds; once it closes, the cursor moves to the gap's top and the messages that arrived above it are fetched in the same sync. A message already held is never doubled.

The `capture` service holds the session's realtime event stream and writes each new message, edit, deletion, thread reply and reaction into the store as it arrives. On a dropped connection it reconnects with backoff and, before resuming the live stream, backfills each member conversation from its cursor, so the window it was disconnected for is closed through the same dedup path. No channel message is lost and nothing is doubled, because the store's `ts` key is the single source of identity for both the live and the backfill writes. A thread reply posted during the drop is the exception: Slack's channel history carries no thread replies except those also sent to the channel, so the backfill does not reach it, and `thread <channel> <thread_ts> --fresh` reads it.

Until a realtime connection has held for 30 seconds, each reconnect doubles the backoff, up to a minute, and the log names why the connection ended: a refused handshake is logged as `realtime handshake failed` with its HTTP status. The socket carries the session's `d` cookie and the web client's origin. The gateway can accept the socket and then refuse the session with an error frame; that is logged as `realtime handshake failed` with the gateway's error code and counts as a failed connect. The service's health reads `connecting` until the gateway's first other frame and `connected` from then, and it counts each captured event (`events`, `last_event_at`) as it is written.

## The assistant

The same service answers the messages its settings admit, as the linked person, the way the `whatsapp` and `telegram` assistant services answer theirs. Its settings, written through `slacker settings` and taken by a running listener at `slacker service reload`, say which direct messages, group direct messages, channels and threads it answers in, who may wake it (by Slack user id), whether a listed conversation wakes it on a mention or on every message, each person's role and the authority a role's turn runs under, and the harness profile (Claude or Codex, model, effort) each conversation's turns run on. One person's consecutive messages collect into one turn after a debounce, the turn reads the conversation (in a thread, the thread) back from the store, runs through callva-harness-runner, and its answer is posted in the thread the request was made in, or in a thread under it in a channel. A turn may hand longer work to a job; an audio clip is transcribed before its turn. The service posts only through the connection's write grant: on a connection that does not grant writes it answers nothing. Settings that admit nobody leave it capturing exactly as before. The `assistant-service` guide holds the whole model.

## Session lifetime

The `d` cookie carries roughly a one-year expiry, but a session ends sooner — a sign-out, a password change, an admin signing members out, or the workspace's session-duration setting. Expiry shows as `invalid_auth` (exit 2); there is no silent refresh, so the person links the connection again. The live-capture service reports an unlinked session as exit 7 and stops reconnecting.

## Limits to record, not assume

Rate limits, the exact realtime handshake, and whether every client view (for example search) is reachable as the session are to be observed against the person's own workspace during the trial and recorded as findings, not assumed. Enterprise Grid is out of scope for the own-workspace pilot; its anomaly detection has suspended session-route clients elsewhere, so it is not exercised here.
