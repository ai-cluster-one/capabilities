# Slack service (real-time daemon)

The `slack service` daemon listens over Socket Mode and, per policy, either runs
a headless agent (the Oracle) that answers in-thread, or relays a message to a
local inbox.

## Setup

1. Slack app: enable Socket Mode; subscribe to bot events `message.im` and
   `app_mention`; create an App-Level Token with `connections:write`.
2. In the consuming project's `.env.local`: `SLACK_BOT_TOKEN=xoxb-…` and
   `SLACK_APP_TOKEN=xapp-…`.
3. `slack service init` — writes `capabilities/slack/service/settings.json` and `context.md`.
4. Edit `settings.json`:
   - `allowed_users` — `{ "U…": { "name": "Alice", "role": "supervisor|default" } }`
     (a bare string name is also accepted; role then defaults to `default_role`).
   - `auto_answer.users` / `auto_answer.channels` — who gets an agent answer vs. an inbox relay.
   - `control.roles` — which roles may send `stop` / `status` to the bot.
   - `authority.roles.<role>.allowed_capabilities` — which capabilities the worker
     may call for that role (written to a per-job `CAPABILITIES_AUTH_CONTEXT`).
   - `defaults.worker` — `claude` (default), `codex`, or `stub`; optional
     `defaults.model` / `defaults.effort`; `tail_size`, `worker_timeout`,
     `max_parallel_jobs`, `catch_up.{max_age_seconds,max_messages}`.
5. `slack service doctor` — validates both tokens + Socket Mode handshake.
6. `slack service start` — background daemon; `slack service logs` to watch.

## Behavior

- Admitted DMs (`direct_messages.mode` + `allowed_users`) and channel `@mention`s
  (`allowed_channels`) are deduped by a persistent register.
- `auto_answer` → a headless `claude`/`codex`/`stub` worker answers, rebuilding
  context from the live Slack tail each turn (Slack is the persistence layer).
  Otherwise → appended to `inbox.jsonl` (+ optional owner DM).
- Messages are queued FIFO per conversation and run in parallel across
  conversations up to `max_parallel_jobs`.
- A 👀 reaction marks a message received; it becomes ✅ on success or ❌ on error.
  The worker's `slack post` routes through the daemon outbox to the current
  conversation only.
- `stop` / `status` (addressed keywords) are gated by `control.roles`.
- On (re)connect, one bounded catch-up pass replays messages missed while down
  (newer than the per-channel watermark, within `catch_up` age/count bounds),
  each processed exactly once.
- A worker timeout or crash kills the process group, posts an error notice, and
  marks the job terminal (no auto-retry). A message that was mid-answer when the
  daemon itself crashed is not auto-re-delivered on restart — the sender re-asks;
  messages that arrived while the daemon was down (never started) are recovered.
- Tool authority (`authority.roles`) gates which capabilities a worker may call, and
  the outbox scopes a worker's `slack post` to the current conversation. Read verbs
  (`slack read`/`resolve`) are not channel-restricted — a worker may read other
  conversations it has access to; keep that in mind when granting `slack` to a role.

## State

`$XDG_STATE_HOME/slack/<connection>/service/`: `daemon.pid`, `daemon.log`,
`register.json`, `inbox.jsonl`, `watermarks.json`, `authority/`, `outbox/`.
