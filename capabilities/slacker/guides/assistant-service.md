# Slack — assistant service

How the bundled `capture` service, beyond capturing, answers the Slack messages its settings admit with a dialogue turn on the harness runner, posted as the linked user; how it collects one person's burst of messages into one turn, runs the jobs those turns hand over, transcribes audio clips, and how it is operated.

## What it does

The service is one long-running process per linked account. It holds the account's realtime event stream and writes every message into the store, as the operational model describes. Each message it captures live is then offered to the assistant, which decides whether it is answered (see The gate). An admitted message waits for the person to stop writing, one turn runs for everything they said, and its answer is posted to Slack as the linked user. Settings that admit nobody - no allowed user, no listed conversation, direct messages not open to anyone - leave the service capturing exactly as before: it starts no dialogue, needs no harness runner and posts nothing.

The answer is the linked person's own message. Slack shows it under their name, in their conversation, like anything they post themselves.

## Before it starts

- The project enables the capability explicitly: `capabilities enable slacker --project`.
- The connection grants writes: `capabilities set slacker grant <connection> '{"allow_write": true}'`, run inside the project, by a person. The service posts only through that grant; on a connection that does not grant writes it admits nothing, runs no turn and posts nothing, and `service doctor` says so.
- `slacker service init` seeds the settings from the bundle's template, which names every default and admits nobody, and writes nothing over settings that exist unless given `--force`.
- The project has a database and an id (`capabilities init`); the assistant keys what it answered by the project's id.

## Settings

The settings are written and read through the contract's `slacker settings` verb (`settings`, `get`, `set`, `unset`, `put`, `edit`), at project scope over the global one. Every write is validated whole by the bundle's schema before anything is written, and an unknown key is refused naming its path, never ignored. A key holding a dot - a thread - is given as a JSON array of keys: `slacker settings set '["allowed_channels","C0123456/1700000000.000100","wake"]' message`. A person is named by their Slack user id (`U...`, or `W...` on an Enterprise Grid org), a conversation by its id.

- `connection`: the connection the service runs; null or empty takes the registry's default. Takes effect on restart.
- `environment`: a label the service reports, and part of the register's and the jobs' key. Takes effect on restart.
- `assistant_name`: how the assistant is named in prompts, and the alias a listed conversation uses when it declares none.
- `direct_messages`: `mode` is `allowlist` (default: only people under `allowed_users`), `anyone`, or `off`; `default_role` is the role of a direct sender without one (default `direct_user`).
- `allowed_users`: keyed by Slack user id: `name`, `role`, `profile`, `worker_timeout` (1 to 3600 seconds), `context` (text added to the prompt in this person's direct messages), `voice_transcription`.
- `allowed_channels`: keyed by a conversation id - a channel, a private channel, a group direct message, or a direct message - or by `<conversation id>/<thread ts>` for one thread of it, which then has settings of its own over the conversation's. Each entry: `name`, `wake` (`mention`, the default in a channel or group direct message, or `message`), `may_address` (`anyone`, the default, `allowed_users`, or a list of user ids), `member_role` (default `channel_member`), `aliases` (case-insensitive patterns matched as whole words; default the assistant name), `reply_in_thread` (default true outside a direct message), `follow_threads` (default true), `profile`, `worker_timeout`, `context`, `voice_transcription`. A conversation that is not listed, and no thread of it is, is never answered, except a direct message, which `direct_messages` governs.
- `authority`: `default` and `roles.<role>`, each `allowed_capabilities` (`{"*": true}`, a list of capability names, or per capability `true` or `{verbs, scope, connections}`). The role's merged policy is written per turn to a user-only file named by `CAPABILITIES_AUTH_CONTEXT`. Without an `authority` key no context is passed and the project's own gate is the whole policy.
- `defaults`: `tail_size` (1 to 500, default 40), `debounce` (0 to 300 seconds, default 3), `max_age` (10 to 86400 seconds, default 600), `worker_timeout` (1 to 3600 seconds, default 120), `max_parallel_dialogue` (1 to 32 per conversation key, default 1), `profile` (default `slacker-claude`), `max_parallel_jobs` (1 to 32, default 1), `job_profile` (default `slacker-job-claude`), `job_recovery` (`requeue`, the default, or `inspect`), `voice_transcription` (`off`, `addressed`, the default, or `auto`).

The pilot's settings document - `connection` and a `_help` object - is still read; `_help` means nothing.

`slacker service reload` makes the running listener take the settings: it signals the listener alone, which re-reads the settings and the connection's write grant, validates them, resolves and fits every profile they reach, and only then takes them. A refused reload exits 6 `service_reload_refused` naming why, and the previous settings stay in force. A reload waits out the store's two-second read cache since its last read, so it reads what was written before it was asked for.

## The gate

Every message the service captures live is judged in this order, and the first step that stops it wins:

1. Kind: a message of a subtype nobody says - an edit, a deletion, a join, a topic - and a bot's post are never answered. A plain message, a thread reply also sent to the channel, and a file shared with a message are.
2. Its own message: the linked person's own messages are theirs and never answered. The one exception is the account's own direct message with itself, where what the person types is judged like any other message, sent by them. What the service posted never starts a turn, there or anywhere: each post is marked `sent_by_service` on its stored row, and a message in that direct message is judged only once the posts in flight to it are recorded.
3. Older than `max_age`: dropped, so a backlog is not answered in bulk.
4. The register: a message already processed, or older than the service's first sight of the conversation, is dropped.
5. Conversation allowed: listed under `allowed_channels`, itself or the thread it is in; a direct message unless `direct_messages.mode` is `off`.
6. Sender allowed: by Slack user id. In a direct message that is not listed, `direct_messages.mode` decides; in a listed conversation, its `may_address`.
7. Woken: a trailing `#noreply` line opts a message out everywhere. A direct message wakes a turn on every message. A listed conversation with `wake: message` does too; with `wake: mention` the message must mention the linked account (`<@U...>`), call it by an alias, or - with `follow_threads` - be a reply in a thread the account started or has posted in. A message from the person whose turn is still waiting in that conversation key always joins it (see The turn). An audio clip in a conversation whose `voice_transcription` is `auto` passes unwoken, and its transcript decides.
8. Write grant: on a connection that does not grant writes, or under the read-only switch, nothing is admitted.
9. Role: the person's `allowed_users` role, else the conversation's `member_role` or `direct_messages.default_role`.

An admitted message is reserved in `slacker_register` before anything else is done with it, so the same message delivered again after a reconnect or a restart is answered once. The register holds, per project id, environment, account and conversation, the first message admitted there, the watermark, the ts processed within a day of it, and the conversation's counters. The listener's log carries one line per message the gate stopped and why, except the person's own, a subtype, a bot's post and an unlisted conversation.

## The turn

Admitted messages wait per conversation key: the conversation, or one thread of it. Each waits `debounce` seconds, and every admitted message in that key restarts the wait. Consecutive messages from one person collect into the turn waiting for them, so a burst of several messages gets one answer; a message from someone else in the same key is a turn of its own, after it. At most `max_parallel_dialogue` turns run at once in one key, in arrival order.

A turn's prompt is the `context` prompt, then the `delegation` prompt where the turn may hand work to a job, the conversation's `context`, the channel state (conversation, kind, thread, participant and role, effective settings, profile, authority), the current request - every message of the burst, in order - and the conversation read back from the store: in a thread, the thread, brought into the store whole from Slack when the store does not hold every reply its parent counts; elsewhere the conversation's top level, the last `tail_size` messages up to the request. In that tail a message the service posted is the assistant's, the linked person's own messages are named as the account's owner, and mentions are shown by name.

It runs through callva-harness-runner from the conversation's profile, in the project directory, on a fresh session, with a scrubbed environment and the service's variables (`SLACKER_AUTHORIZED_CHAT_ID`, `SLACKER_AUTHORIZED_THREAD_TS`, `SLACKER_AUTHORIZED_REQUESTER`, `SLACKER_AUTHORIZED_ORIGIN_MESSAGE_ID`, `SLACKER_AUTHORIZED_CONNECTION`, `SLACKER_AUTHORIZED_JOB_PROFILE`, `SLACKER_ENVIRONMENT`, `SLACKER_REAL_SLACKER`, `SLACKER_DAEMON_CHILD`, `CAPABILITIES_AUTH_CONTEXT`) set on top, and the bundle's `service/worker-bin/` first on its `PATH`. At `worker_timeout` the run is cancelled and the conversation is told.

The answer is what follows the last `=== REPLY ===` line, or the whole text without one. It is posted as the linked user with `chat.postMessage`, through the connection's write grant, split between paragraphs above 3500 characters: a request made in a thread is answered in that thread; one at the top of a channel or group direct message is answered in a thread under its first message unless `reply_in_thread` is false; one in a direct message is answered there. Each post is recorded in the store as the capture records a sent message, marked as the service's own. A completed turn with an empty answer posts nothing; a failed one says it could not answer and names the kind of failure.

### Profiles

A profile says how the model runs - Claude or Codex, the model, its effort - and is a callva-harness-runner profile kept in the store, in the `harness_profile_versions` table the runner owns. A name resolves to the project's active version, then the global one, then the runner's shipped profiles, then the bundle's `service/profiles/`; the version found is used whole. A profile is saved as versions with `tasks profiles save <name> <file>` (`--global` for every project). The bundle ships `slacker-claude` (claude, model `opus`) and `slacker-codex` (codex, model family `sol`) for turns, and `slacker-job-claude` and `slacker-job-codex` for jobs, the same with a one-day deadline. A conversation's profile is its thread's entry, else its conversation's, else - in a direct message - its person's, else `defaults.profile`, else `slacker-claude`. Every profile the settings name is resolved and fit-checked when the service starts and at every reload, and `service doctor` lists them where the runner is in process. A profile fits when it leaves `output_schema` unset and gives the full access a worker has (claude: `permission_mode = "bypassPermissions"`, Bash available, no sandbox; codex: `sandbox_mode = "danger-full-access"`, no permissions profile, `approval_policy = "never"`).

The listener resolves the runner itself: when its settings admit anyone it runs again under `uv run --with` the runner pinned in the bundle's `service/profiles.py`. The CLI's own dependencies do not include it, and settings that admit nobody never need it. A reload that first admits someone in a listener started without the runner is refused; restart the service.

### Prompts

The service's three prompts are the `context` of every turn, the `delegation` text a turn is given when it may hand work to a job, and the `job-worker` text that is the whole of a job's prompt. Each is the agent's active version in the store's `slacker_prompts` table, else the standard text the bundle ships in `service/templates/`. `slacker prompts list` shows each with its source, `prompts show <key>` its text, `prompts put <key> [--base ID] [--note TEXT]` saves a new version from stdin, `prompts history <key>` lists every version and `prompts retire <key>` returns the key to the bundle's text. Every turn reads its prompt fresh, so a saved version applies to the next one with no restart.

### Audio clips

An audio clip - a Slack clip or an audio file shared with a message - in an admitted conversation is transcribed before its turn when its `voice_transcription` asks for it: its thread's or conversation's entry, else its person's in a direct message, else `defaults.voice_transcription`, else `addressed`.

- `off`: no clip is transcribed; the turn reads it as the file it is.
- `addressed`: a clip in a message that is admitted on its own - any in a direct message, one that wakes a turn in a listed conversation - is transcribed.
- `auto`: in a listed conversation every clip from a sender who may address it is transcribed, and it is answered only when it woke a turn already or its transcript calls the assistant by an alias.

Where Slack has transcribed the clip itself, its transcript is used. Otherwise the clip is read over the session and sent to Deepgram (model `nova-3`) with the `DEEPGRAM_API_KEY` the credential cascade resolves. The message keeps its place in its key's queue meanwhile, so turns still run in arrival order. The transcript, or why there is none, is stored beside the message (`transcript`, `transcript_error`) and never tried again; the turn reads it as `[voice] <transcript>`, or `[voice message - transcription failed: <reason>]`. Later turns read it the same way in the tail. `service doctor` warns when a conversation transcribes and no Deepgram key resolves.

## Jobs

A turn may hand longer work to a job instead of answering itself. It is offered that only where the listener runs a job runner, the register answers, and the sender's role reaches `slacker jobs` under `authority` (or the settings declare no authority); then the `delegation` prompt follows the `context` prompt and the channel state names the jobs command. The bundle's `service/worker-bin/slacker` is first on every worker's `PATH`: for `jobs` it adds the conversation, the thread, the requester, the origin message and the job profile from the environment the listener set, and refuses a call that names any of them itself (`worker_scope_denied`, exit 4); every other verb passes to the real CLI unchanged. The CLI holds a worker the listener started (`SLACKER_DAEMON_CHILD`) to the same scope again (`job_scope_denied`).

`slacker jobs help` lists the verbs: `list`, `active`, `show`, `register` (writes a `draft` no runner takes), `submit` (needs `--confirm-active-jobs-checked`), `amend`, `discard` (drafts only), `stop` and `resume`. Outside a worker, `register` takes `--chat`, `--thread` and `--requested-by`. One message hands over at most one job.

Submitting ends the turn that submitted it: the turn is checked every second for a job submitted from its first message, and once there is one it has eight seconds to end on its own with a short line; a turn still running then is ended, and the job's description, after `▶ `, is posted instead.

The job runner looks at the register every two seconds, on a thread and a store connection of its own. It renews its leases, fences attempts whose owner is gone, honours stops and amendments, keeps the quota pause, delivers finished results, and claims the oldest waiting jobs while fewer than `max_parallel_jobs` run, each claim one row in `slacker_job_slots`, an attempt token and a 30-second lease. A job runs through callva-harness-runner from its profile with the `job-worker` prompt, the channel state naming the job, the request and the conversation's tail, under the requester's role's authority. Its result, cut at `=== REPLY ===`, is posted into the conversation, in the thread the request was answered in, inside the transaction that marks it delivered: a post Slack refuses leaves it pending for the next look. A listener that dies after Slack took the post and before the commit is the one window in which a result can be posted twice. A spent quota pauses the queue for five minutes; a failure is posted as a failure, with the reason for a `supervisor`. When the listener stops, each running job is interrupted, and with `job_recovery` `requeue` one with a session continues it on the next start.

## Operating it

- `slacker service run` runs the listener in the foreground; it is the deploy command. It refuses with exit 6 `service_settings_invalid` on settings the schema refuses, and `service_dialogue_invalid` when settings that admit someone reach a profile that does not resolve or fit, or the service runs outside a project.
- `slacker service reload` validates, signals the listener, and waits for its answer (see Settings).
- `slacker service status` reports the last health: state, events captured, the reload counters and the last refusal, whether the connection grants writes, and the assistant's counters (admitted, merged, turns, answered, silent, failed, stopped, handed off, transcribed, transcription failed, audio unaddressed, running, queued), with the job runner's under `dialogue.jobs`.
- `slacker service doctor` proves the store and the session and says what the settings reach: whether they admit anyone, whether the connection grants writes, the source of each prompt, and the profiles.
- SIGTERM ends the listener: the socket is closed, running turns are cancelled and running jobs interrupted.

## Files and tables

The control files are machine-local, under the account's state home: `service/health.json`, `service/owner.json`, and `service/authority/`, which holds each running turn's and job's authority file only while it runs. The store holds `slacker_register`, `slacker_jobs`, `slacker_job_amendments`, `slacker_job_slots` and `slacker_prompts`, each keyed by the agent's id, and the `sent_by_service`, `transcript` and `transcript_error` columns of `slacker_messages`.
