# WhatsApp — assistant service

How the bundled `assistant` service holds one account's connection, captures every message live, answers the messages its settings admit with a dialogue turn, transcribes the voice notes among them, runs the jobs those turns hand over, sends what is queued for the account, and how it is operated.

## What it does

The service is one long-running process per account. It opens the account's linked-device session through the same engine, capture path, account check and Postgres store every verb uses, and keeps it open. Each message the account receives or sends is written to the store as it arrives, with sync type `LIVE`; history-sync chunks the server pushes are stored as they are by any verb. It sends the messages `whatsapp send` queues for the account, and answers the messages its settings admit (see The dialogue). Settings that admit nobody leave it capturing and sending and answering nothing.

## Before it starts

- The project enables the capability explicitly: `capabilities enable whatsapp --project`. Global availability grants CLI use, not a project daemon.
- `whatsapp service init` seeds the service settings records (`capabilities/whatsapp/service/settings.json` in files mode) from the bundle's template, which names every key with its default and admits nobody, and the service's prose where the project has none: the dialogue's `context` document, the `delegation` document a turn is given when it may hand work to a job, and the `job-worker` document that is the whole of a job's prompt (`capabilities/whatsapp/service/context.md`, `delegation.md` and `job-worker.md` in files mode). The prose is the project's to edit and is never overwritten; `init` on a project whose settings already exist seeds only the documents it lacks. The settings keys are listed under Settings; any other key is refused by `start`, `run`, `doctor` and `reload`, naming its path.
- The project has a database: its `.env.local` / `.env`, the process environment or the machine's store setting names one (`capabilities store show` shows which answers). Without one the service refuses to start and `service doctor` reports `store_not_configured`. The listener keeps the database it resolved at launch for its whole run.
- The connection is an in-house one with a linked device.

## Operating it

- `whatsapp service start` launches the listener detached and returns once it has taken the account lock and published its owner record. It refuses, each with its own code, when the project has not enabled the capability (`project_enable_required`), the service is not initialized (`service_not_initialized`), the store is not configured (`store_not_configured`), or a listener with fresh health already owns the account (`service_running`). A holder whose health is older than `defaults.stall_seconds` is not a reason to refuse: the listener it launches takes the account over (see Liveness), and the answer names the pid it took over from.
- `whatsapp service run` runs the same listener in the foreground; it is the deploy command. It exits 9 when it stalled (see Liveness).
- `whatsapp service status` reports state (`stale`, with `health_age_seconds`, when a live holder has published no health for 60 seconds), how long ago the listener's loop last made progress and the stall limit, pid, whether it is connected, uptime, the last event and message times, messages captured, messages sent and send failures, the send rate, the spool depth and the store error, reconnects and the last error, and the dialogue's counters (admitted, turns, answered, silent, failed, stopped, handed off, commands, transcribed, transcription failed, voice unaddressed, running, queued), with the job runner's under `dialogue.jobs` (running, the slot budget, the pause, claimed, succeeded, failed, cancelled, interrupted, quota, model refused, delivered).
- `whatsapp service logs` tails the listener's log, including one line per message the dialogue did not answer and why. `whatsapp service doctor` proves settings, store, link and ownership without opening a second connection and, when the settings admit anyone, the project id the register needs, every profile the settings reach, and whether a Deepgram key resolves for the chats that transcribe voice notes; without one it warns, naming those chats, and does not fail. It fails as `service_stale` when a live holder's health is stale; it is the deploy doctor, so supervision treats a stale listener as down.
- `whatsapp service reload` validates the settings, then signals the listener alone to re-read them; the listener resolves and fits their profiles before taking them, and a rejected reload leaves the previous settings in force. A change of connection or environment takes effect only on restart.
- `whatsapp service stop` ends the listener cleanly; the account lock is released when it exits.

## Settings

- `connection`: the connection the service runs; null takes the registry's default. A change takes effect on restart.
- `environment`: a label the service reports, and part of the register's key. A change takes effect on restart.
- `assistant_name`: how the assistant is named in prompts, and the alias a group uses when it declares none.
- `direct_messages`: `mode` is `allowlist` (default: only numbers under `allowed_users`), `anyone`, or `off` (no direct chat is answered); `default_role` is the role of a direct sender without one (default `direct_user`).
- `allowed_users`: keyed by phone number with country code (digits, a leading `+` accepted): `name`, `role`, `profile`, `worker_timeout` (1 to 3600 seconds), `context` (text added to the prompt in this person's direct chat), `voice_transcription`.
- `allowed_groups`: keyed by group JID (`<digits>@g.us`): `name`, `require_reference` (default true), `aliases` (case-insensitive patterns matched as whole words in the text; default the assistant name), `may_address` (`anyone`, the default, `allowed_users`, or a list of phone numbers), `member_role` (default `group_member`), `profile`, `worker_timeout`, `context`, `voice_transcription`.
- `control.roles.<role>.commands`: which of `status`, `set`, `reload`, `stop`, `help` a role may send (a list, `"*"`, or per command `{allow|deny|enabled}`), over the defaults: `supervisor` all five, `channel_admin` status, set and help, `direct_user` and `group_member` status and help.
- `authority`: `default` and `roles.<role>`, each `allowed_capabilities` (`{"*": true}`, a list of capability names, or per capability `true` or `{verbs, scope, connections}`). The role's merged policy is written per turn to a user-only file named by `CAPABILITIES_AUTH_CONTEXT`. Without an `authority` key no context is passed and the project's own gate is the whole policy.
- `defaults`: `tail_size` (1 to 500, default 40), `debounce` (0 to 300 seconds, default 3), `max_age` (10 to 86400 seconds, default 600), `worker_timeout` (1 to 3600 seconds, default 120), `max_parallel_dialogue` (1 to 32 per chat, default 1), `profile` (default `whatsapp-claude`), `send_rate` (1 to 120 messages a minute, default 20), `max_parallel_jobs` (1 to 32 jobs running at once for the project, environment and account, default 1), `job_profile` (the profile a job runs on, default `whatsapp-job-claude`), `job_recovery` (`requeue`, the default, or `inspect`; see Jobs), `voice_transcription` (`off`, `addressed`, the default, or `auto`; see Voice notes), `stall_seconds` (30 to 3600 seconds, default 120; see Liveness). A top-level `send_rate` is accepted as the deprecated spelling of `defaults.send_rate`; setting both is refused, and `service doctor` names it.

## The dialogue

Every message the service captures live is offered to the dialogue once it is stored. A broadcast - `status@broadcast`, a broadcast list, a channel post (`@newsletter`) - is captured and never judged, so a start that drains many of them does not wait on the store for each. A gate judges every other message in this order, and the first step that stops it wins:

1. Its own message: never answered. The one exception is the account's own self-chat, where a message typed on the phone (or another of the account's devices) is judged like any other, as sent by the account's own number. What this device sent, including every answer, never starts a turn.
2. History-sync origin: never answered; only live deliveries are.
3. Older than `max_age`: dropped, so a long offline queue is not answered in bulk.
4. The register: a message already processed, or older than the service's first sight of the chat, is dropped.
5. Chat allowed: a group under `allowed_groups`; a direct chat unless `direct_messages.mode` is `off`.
6. Sender allowed: by phone number. A sender that arrives as a LID is matched through the store's phone/LID identities, and an unknown LID is asked of the engine.
7. Addressed: a trailing `#noreply` line opts a message out everywhere. In a group the message must mention the account's JID or LID, quote one of the account's messages, or match an alias, unless the group sets `require_reference` to false. A voice note in a group whose `voice_transcription` is `auto` passes unaddressed, and its transcript decides (see Voice notes).
8. Role: the sender's `allowed_users` role, else the group's `member_role` or `direct_messages.default_role`.
9. A control command at the head of the text (past a leading mention or alias) runs as one; anything else becomes a dialogue turn.

An admitted message is reserved in `whatsapp_register` before anything else is done with it, so the same message delivered live and again after a reconnect or restart is answered once. The register holds, per project id (from `capabilities/project.json`), environment, account and chat, the first message admitted there, the watermark, the ids processed within a day of it, the chat's `/set` overrides and its counters. A chat enters it on its first admitted message, so nothing before the service first saw a chat is answered. A message reserved by a run that then crashed is not answered after the restart.

The moment a message is admitted for a turn - before its debounce, before its voice note is transcribed and before any model starts - it is marked read and the chat shows the account composing, sent from a thread of the listener's own so the engine never waits on it. The composing is refreshed every eight seconds while the chat has a turn waiting or running, and set to paused when a turn ends without an answer or everything admitted was dropped before a turn ran. A control command is answered at once and shows neither. A turn waits `debounce` seconds after its message, then runs in arrival order, at most `max_parallel_dialogue` at once in a chat. Its prompt is the `context` document, the chat's `context`, the channel state (chat, participant and role, effective settings, profile, authority), the current request, and the chat's last `tail_size` stored messages. It runs through callva-harness-runner from the chat's profile, in the project directory, on a fresh session, with a scrubbed environment and the service's variables (`WHATSAPP_AUTHORIZED_CHAT_ID`, `WHATSAPP_AUTHORIZED_REQUESTER`, `WHATSAPP_AUTHORIZED_ORIGIN_MESSAGE_ID`, `WHATSAPP_AUTHORIZED_CONNECTION`, `WHATSAPP_AUTHORIZED_JOB_PROFILE`, `WHATSAPP_ENVIRONMENT`, `WHATSAPP_REAL_WHATSAPP`, `WHATSAPP_DAEMON_CHILD`, `CAPABILITIES_AUTH_CONTEXT`) set on top, and the bundle's `service/worker-bin/` first on its `PATH` (see Jobs). At `worker_timeout` the run is cancelled, which ends everything it started, and the chat is told. The answer is what follows the last `=== REPLY ===` line, or the whole text without one; it is queued as pending rows in `whatsapp_messages`, quoting the request in a group and plain in a direct chat, split between paragraphs above 3500 characters, and the listener sends it like any queued message, with no pause of its own. A completed turn with an empty answer sends nothing; a failed one says it could not answer and names the kind of failure.

### Voice notes

A voice note (a push-to-talk note or an audio file) in an admitted chat is transcribed before its turn when the chat's `voice_transcription` asks for it: its `/set voice-transcription`, else its group's or person's `voice_transcription`, else `defaults.voice_transcription`, else `addressed`.

- `off`: no voice note is transcribed; one that is admitted reaches its turn as `[audioMessage]`.
- `addressed`: in a direct chat every admitted sender's voice note is transcribed. In a group only one addressed without its words is: one that quotes the account's message, or any where the group sets `require_reference` to false. An unaddressed group voice note is captured and neither transcribed nor answered.
- `auto`: in a group every voice note from a sender who may address it is transcribed and echoed, and it is answered only when it was addressed already or its transcript matches one of the group's aliases; otherwise it is echoed and nothing is answered. In a direct chat it is `addressed`.

The voice note is reserved in the register before anything is fetched, so a redelivery or a restart never transcribes or answers it twice. The listener fetches the audio through the connection it holds, on a thread of its own, and sends it to Deepgram (model `nova-3`) with the `DEEPGRAM_API_KEY` the credential cascade resolves; the note keeps its place in the chat's queue meanwhile, so the turns of that chat still run in arrival order. The audio is cached in the account's `media/` folder and the transcript is stored in `whatsapp_enrichment` beside the message, as `whatsapp transcribe` stores one, so the read verbs and exports show it.

Every transcribed note is echoed into its chat at once, before any turn, as a message the listener sends like any other: in a direct chat `Твоё сообщение:` followed by the transcript as quoted lines (`> `), in a group the quoted transcript alone, sent as a reply quoting the voice note so the reply shows whose it was. Then the ordinary rules decide whether a turn runs, and the turn reads the note as `[voice] <transcript>`. A note that cannot be transcribed - audio no longer on WhatsApp's servers, a Deepgram failure, no key, no speech recognised - is echoed as `[голосовое — не удалось расшифровать]`; a turn that runs for it reads `[voice note - transcription failed: <reason>]`, the reason is stored as the transcript's error, and nothing is tried again. An unaddressed `auto` note that fails is echoed and not answered. An echo that cannot be queued is logged and not repeated. The account's own sends are never transcribed, and answers are text.

Wherever a stored voice note has a transcript, older ones included, the conversation in a prompt shows it as `[voice] <transcript>` under its sender, and one whose transcription failed as the failure marker; this holds whatever the chat's mode. The echoes are left out of it: each chat's register keeps the ids of the echoes sent there, so a note's words appear once, as the sender's, and never as the assistant's.

### Profiles

A profile says how the model runs; it is a callva-harness-runner profile file. A name resolves through the runner's own discovery, with `capabilities/whatsapp/service/profiles/` in the project first, then the bundle's `service/profiles/`, then the machine folder, then the runner's shipped profiles; the first file found is used whole. The bundle ships `whatsapp-claude` (claude, model `opus`) and `whatsapp-codex` (codex, model family `sol`) for turns, and `whatsapp-job-claude` and `whatsapp-job-codex` for jobs, the same with a one-day deadline. A chat's profile is its `/set profile`, else its group's or person's `profile`, else `defaults.profile`, else `whatsapp-claude`; a job's is the one recorded when it was registered, from `defaults.job_profile`, else `whatsapp-job-claude`. Every profile the settings name is resolved and fit-checked when the service starts and reloads and in `service doctor`; a profile fits when it leaves `output_schema` unset and gives the full access a worker has (claude: `permission_mode = "bypassPermissions"`, Bash available, no sandbox; codex: `sandbox_mode = "danger-full-access"`, no permissions profile, `approval_policy = "never"`).

The listener resolves the runner itself: when its settings admit anyone it runs again under `uv run --with` the runner pinned in the bundle's `service/profiles.py`, which `start` and `doctor` run as a script to check profiles. The CLI's own dependencies do not include it, and settings that admit nobody never need it.

### Control commands

Sent as text in an admitted chat (in a group, addressed like any message), gated by the sender's role under `control.roles`. A refused command is answered with the role that lacks it.

- `/status`: the connection, the chat and the sender's role, the chat's profile and effective settings, turns running and queued, the chat's counters, the chat's open jobs (id, state, description), and the job queue's pause when there is one.
- `/set <tail|debounce|worker-timeout|profile|voice-transcription> <value|default>`: a per-chat override kept in the register; a profile must resolve and fit, and `voice-transcription` takes `off`, `addressed` or `auto`. `/set` alone lists them with their current values.
- `/reload`: re-reads the settings as `service reload` does, and answers whether they were taken.
- `/stop`: cancels the chat's running turn, drops what is queued, and stops the chat's waiting and running jobs; each stopped job answers with a line saying it stopped and can be continued.
- `/help`: the commands the sender's role may send.

## Jobs

A turn may hand longer work to a job instead of answering itself. It is offered that only where the listener runs a job runner, the register answers, and the sender's role reaches `whatsapp jobs` under `authority` (or the settings declare no authority); then the `delegation` document follows the `context` document in its prompt, and the channel state names the jobs command. The bundle's `service/worker-bin/whatsapp` is first on every worker's `PATH`. For `jobs` it adds the chat, the requester, the origin message and the job profile from the environment the listener set, and refuses a call that names any of them itself (`worker_scope_denied`, exit 4); every other verb passes to the real CLI unchanged. The CLI holds a worker the listener started (`WHATSAPP_DAEMON_CHILD`) to the same scope again, refusing a different chat, requester, origin or profile (`job_scope_denied`).

`whatsapp jobs help` lists the verbs: `list`, `active`, `show`, `register` (writes a `draft` no runner takes), `submit` (hands a draft to the runner; it needs `--confirm-active-jobs-checked`), `amend`, `discard` (drafts only), `stop` and `resume`. Outside a worker the verbs read and move every job of the project, environment and account, and `register` takes `--chat` and `--requested-by`. One message hands over at most one job: a second registration from it answers with the first.

Submitting ends the turn that submitted it. The turn is checked every second for a job submitted from its message; once there is one, the turn has eight seconds to end on its own, and its answer is the acknowledgement. A turn still running then is ended, and the job's description, after `▶ `, is sent instead; so is an empty answer.

The listener's job runner looks at the register every two seconds, on a thread of its own and a store connection of its own, connected or not. Each look renews its leases, fences attempts whose owner is gone, honours stops and amendments, keeps the quota pause, delivers finished results, and claims the oldest waiting jobs while fewer than `max_parallel_jobs` run. A claim takes one row in `whatsapp_job_slots` and stamps the job with an attempt token, an owner, the host and a 30-second lease; every write about the attempt names the token and the owner, so an owner that lost its lease changes nothing, and two claimants never take one job or more slots than the budget. A job runs through callva-harness-runner from its profile, in the project directory, with the `job-worker` document as its prompt, then the channel state naming the job, the request (the description and any amendments) and the chat's last `tail_size` stored messages, under the requester's role's authority and the same worker environment as a turn. Its harness pid and session are recorded the moment the harness starts. A job's only deadline is its profile's `timeout_seconds`.

`amend` on a running job stops its attempt and continues the same session with the added text; on a stopped job with a session it puts the job back to waiting. `stop` cancels a waiting job at once and ends a running one on the runner's next look; `resume` continues a stopped job on its session.

A finished job's answer, cut at `=== REPLY ===`, is its result; an empty one sends nothing. The result is queued as pending rows in `whatsapp_messages`, the first quoting the message that asked for the work (plain when the store does not hold it), in the same transaction that marks it delivered, so it is queued exactly once whichever listener delivers it and whenever. It goes out with no pause of its own.

When the harness reports a spent quota the job stops as `quota`, the queue pauses for five minutes (no job is claimed; a pause another listener recorded is honoured too) and the chat is told when it resumes; when the pause lifts the job waits again and continues its session. A refused model stops it as `model_refused`, any other failure as `failed`, each with the reason in `error`, and the chat is told: a supervisor sees the reason, anyone else that it could not be completed.

When the listener stops, each running job is ended and interrupted; one whose listener died is found on the next look by any listener of the project, environment and account (at once when it ran on this machine, when its lease expires otherwise), its harness process group ended when it ran here, and interrupted. With `job_recovery` `requeue` an interrupted job that has a session and ran here waits again and continues its session; with `inspect`, or without a session, it stays stopped and the chat is told it was interrupted and can be continued. A result finished but not yet delivered is delivered by the next listener, once.

## One account, one connection

The service holds the account lock every connected verb takes, for its whole life. While it runs, `send` on that account writes its message as a pending row and returns the service's answer once the service has sent it; any other verb that would connect on that account exits 8 `session_busy`, and the message names the service and the project it runs from. Reads the store answers keep working: `status`, `health`, `chats` without `--fresh`, and `messages` when the store covers the request or with `--rounds 0`.

## Disconnects and removal

When the connection drops, the service closes the session and opens a new one after a wait that starts at 2 seconds and doubles to at most 60, reset once a connection has held for a minute. The server's offline queue drains on each reconnect, so what arrived while the wire was down is captured then. `SIGUSR1` drops the socket on purpose and takes the same path, which is how a reconnect is exercised.

A device removed from the phone is final: the service stops reconnecting, stays up in state `logged_out`, and `status` and `doctor` say so. The remedy is to stop it, re-link with `whatsapp pair --recreate --yes`, and start it again.

If the engine does not release its connection when a session closes, the process exits rather than let a second client exist on the device.

## Liveness

A listener that stops making progress must not hold the account hostage, so it leaves and is restarted.

- A heartbeat thread publishes `service/health.json` every 15 seconds, whatever the engine and the dialogue are doing, with how long ago the listener's loop last made progress (`progress_age_seconds`) and where.
- A watchdog thread checks every second that the loop keeps going round and that no engine handler has been running for longer than `defaults.stall_seconds` (default 120). The clock it uses stands still while the machine sleeps, so a sleep is never a stall. When either has, the listener writes the stack of every thread into `service/daemon.log` and onto stderr, marks its health `stalled`, and leaves at once with exit 9, releasing nothing gracefully; the account lock goes with the process. The stacks say where it stood. Should the watchdog itself never run again, the interpreter's own fault handler dumps the stacks and ends the process after twice the limit.
- SIGTERM and SIGINT are seen by a thread of their own, so a stop request reaches the listener even when its loop is blocked. A listener that has not left 30 seconds after one dumps its stacks the same way and leaves with exit 9.
- A `service run` (and so a `service start`) that finds the account held by a listener whose health is older than `stall_seconds`, and still is two seconds later, ends it - SIGTERM, then SIGKILL after 15 seconds - and takes the account over. Only a process that is a listener (`service run` in its command line) is ever signalled. A holder with fresher health is respected: the new one exits 8 `service_running`.
- Health older than 60 seconds from a live holder is `stale`: `service status` reports it with its age and `service doctor`, the deploy doctor, fails on it, so the deployment watchdog recovers it too.

With `WHATSAPP_SERVICE_STALL_SEAM=1` in its environment, SIGUSR2 blocks the listener's loop for good, the way a lock never released would; that is how the rule is exercised.

## Sending

The service claims the pending rows of its account, the dialogue's answers among them, oldest first and sends each, at most `defaults.send_rate` a minute; the rest wait. A queued row wakes the sender at once - in-process directly, from another process through a Postgres notification - so a row goes out within milliseconds of being queued, with a half-second poll behind both. Each row ends `sent` with WhatsApp's id or `failed` with the reason. A row a previous run claimed and never answered for is failed at start as `interrupted_delivery_unknown` and never sent again. The sending guide holds the whole model.

## When the store is unreachable

WhatsApp hands a message to the service once. A capture the store cannot take because it cannot be reached is therefore appended to `service/capture-spool.jsonl` and written into the store from there once it answers again, and at the next start. While the spool holds anything, new captures join the back of it, so the store receives everything once and in the order it arrived; the spool is empty again as soon as the store is. A spooled capture the store refuses for its content rather than for being down is moved to `service/capture-spool.rejected.jsonl` so it cannot hold back what follows. `service status` reports the spool depth and the store error. Sending pauses while the spool holds anything.

The spool holds an outage and nothing else. A store that is unreachable when the service starts keeps the service from connecting at all, which loses nothing: WhatsApp keeps the device's queue until it connects.

## Files

The control files are machine-local, under the account's state home: `service/owner.json` (who holds the account, with the launch nonce `start` waits for), `service/health.json`, `service/daemon.pid`, `service/daemon.log`, the capture spool while the store is unreachable, and `service/authority/`, which holds each running turn's and job's authority file only while it runs. Captured messages go to the store, and to the spool only until the store answers.
