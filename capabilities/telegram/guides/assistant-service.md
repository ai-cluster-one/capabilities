# Telegram Assistant Service

The Telegram assistant service is bundled with the `telegram` capability. The project stores policy and context only; the daemon engine runs from the installed capability bundle.

## Setup

1. Install or update from a bundled source:

   ```sh
   capabilities install telegram --from /path/to/capabilities/capabilities/telegram
   ```

2. In the consuming project:

   ```sh
   capabilities init
   capabilities enable telegram --project
   telegram service init
   ```

3. Edit `capabilities/telegram/service/settings.json`:

   - Set `connection` to the registry label whose entry carries the intended positive `expected_account_id`, or rely on the registry default.
   - Add `allowed_users` and `allowed_groups`.
   - Add optional per-channel `context_file` or short inline `context` entries when a chat needs its own soft prompt overlay.
   - Review `control.roles`: this hard gate limits who may run service control commands such as `/set`, `/reload`, and `/stop`.
   - Review `authority.roles`: this request-scoped hard gate limits which capability CLIs a worker may invoke for each sender role.
   - Set group `aliases` / `address_aliases` if the assistant should react to names other than the default.
   - Set a group's `call_recording.mode` to `auto`, `on_request`, or `disabled`. Recording is opt-in per group and defaults to `disabled`.
   - Set a group's `voice_transcription.mode` to `auto` to transcribe all voice messages and video notes from participants (unaddressed spoken media are echoed without creating worker jobs). Defaults to `disabled`.
   - Choose `defaults.worker`: `codex`, `claude`, or `stub`.

4. Ensure the selected connection can send replies:

   ```json
   {
    "default": "marvin",
    "connections": {
      "marvin": {
         "name": "Assistant",
         "username": "assistant_username",
         "expected_account_id": 8200881535,
         "api_id": 123456,
         "secret_env": "TELEGRAM_API_HASH",
         "allow_write": true
       }
     }
   }
   ```

   `expected_account_id` is the stable Telegram user/account ID; `api_id` is
   the separate Telegram API application ID. The registry key may remain a
   descriptive label. Run `telegram login --connection <label>` to discover
   the account ID, then bind it explicitly before service or live operation.

5. Authenticate and check readiness:

   ```sh
   telegram login --connection marvin
   telegram doctor --connection marvin
   telegram service doctor --connection marvin
   ```

6. Stop every legacy Telegram daemon for the current user and perform the
   one-time ownership cutover. The command scans all current-user processes and
   refuses while any old daemon is still observable:

   ```sh
   telegram service migrate-ownership --connection marvin \
     --confirm-all-legacy-daemons-stopped
   ```

7. Start and inspect the service:

   ```sh
   telegram service start --connection marvin
   telegram service status --connection marvin
   telegram service logs --connection marvin --tail 80
   ```

Use `telegram service stop` or foreground `run` for supervisor-managed processes. On macOS/local dev, `start` uses a background process with a PID file under the connection's service state directory.

After editing `settings.json`, use `telegram service reload`. The complete strict schema is validated before publishing: unknown properties, invalid shapes/enums/bounds, and unsafe overlay paths are errors with their full JSON path. A rejected cold start/doctor/init check never begins service work; a rejected reload keeps the previous immutable settings and project-layout snapshot active. Changing the selected connection still requires a real restart. The same operation is available as `/reload` and as the live-call `reload_service` tool only when the caller's `control.roles` policy permits `reload` (the shipped default grants it only to `supervisor`). A worker may invoke the CLI form only when its request authority role is `supervisor`; an external terminal without request authority remains an operator surface.

The accepted schema is deliberately finite:

- Top level: `connection`, `assistant_name`, `direct_messages`, `allowed_users`, `allowed_groups`, `control`, `authority`, and `defaults`.
- `direct_messages`: `mode` and `default_role`.
- User/member policy: display `name`/`username`, `role`, `may_address`, `control`, `authority`, `allowed_capabilities` (or `capabilities`), `context`/`context_file`, `context_mode`, `project`, `call_recording`, and `voice_agent`; group members additionally accept `kind` and `address_aliases`.
- Group policy: `name`, `role`/`member_role`, `aliases`/`address_aliases`/`mentions`, `require_reference`, `members`, `agent_dialogue`, `worker_timeout`, `voice_transcription`, `call_recording`, `control`, `authority`, `allowed_capabilities` (or `capabilities`), `context`/`context_file`, `context_mode`, `project`, and `topics`.
- `topics`: keyed by forum topic ID, each entry accepting `project`, `context`, `context_file`, and `context_mode`. A topic carries no authority tier: it inherits its group's rights and can neither narrow nor widen them.
- Top level: `connection`, `environment`, `assistant_name`, `direct_messages`, `allowed_users`, `allowed_groups`, `control`, `authority`, and `defaults`.
- Defaults: `assistant_name`, `tail_size`, `sync_interval`, `sync_stale_after`, `debounce`, `worker_timeout`, `progress_after`, `max_parallel_dialogue`, `max_parallel_jobs`, `job_poll_interval`, `job_recovery`, `max_attempts`, `group_aliases`, `worker`, `workers`, `voice_agent`, and `media_log_level`.
- `media_log_level` decides how much of the media stack reaches the daemon log: `info` is what a normal call is read at, `debug` is what a call under investigation is read at, and the level applies on start and on every reload. Importing pytgcalls mutes that logger, so this setting is the only thing that speaks for it.
- Worker policy: `model`; Claude also accepts `effort`; Codex accepts `reasoning_effort` and `service_tier`. Voice defaults accept `worker`, `workers`, `model`, `voice`, `greeting`, `history`, `timezone`, `progress_interval`, `recording_caption`, and `prompt_file`.
- Control policies contain `commands`; authority policies contain `allowed_capabilities` or `capabilities`. Capability rules accept booleans/`*`, verb lists, or `allow`/`deny`/`enabled`/`scope`/`verbs`/`connections` objects.

IDs must have the correct sign (users positive, groups negative, topics positive), context paths must resolve inside the Telegram service directory, and numeric settings use the bounds named by `/set help` or the shipped template. A `project` route is checked for shape at load and for reachability at dispatch, as described under Worker Project Routing. There is no permissive/legacy mode.

### Using a second account

A worker turn runs on the account that received the message, and that is the only account it reaches by default. Naming another connection is refused, and so is `--session`, which points at a file rather than a declared connection and would therefore reach any account on the machine.

A project that connects more than one account can grant a role the ones it may read. The original list form is read-only:

```json
{
  "authority": {
    "roles": {
      "supervisor": {
        "allowed_capabilities": {
          "*": true,
          "telegram": {"allow": true, "connections": ["principal-personal"]}
        }
      }
    }
  }
}
```

To let that role write through one of those accounts, use the object form and grant the connection explicitly:

```json
{
  "authority": {
    "roles": {
      "supervisor": {
        "allowed_capabilities": {
          "telegram": {
            "connections": {
              "principal-personal": {"allow_write": true}
            }
          }
        }
      }
    }
  }
}
```

The role grant and the connection's own `allow_write` are independent gates; both must be true for `send`, `send-media`, or `react`. Omitting `allow_write`, setting it to false, or using the legacy list keeps that role read-only. A role with no entry for the connection cannot reach it at all.

The grant is deliberately narrow, because the hazard is not the flag but who holds it. A group member who may address the assistant would otherwise be able to ask, from a group, for something that lives in a private account - so reach is a property of the sender's role, named in the project's own settings, and the reachable set is exactly what the project declared.

- Read verbs cross whenever the role names the connection: `chats`, `read`, `search`, `topics`, `download`, `export`, `whoami`, `doctor`, `connections`.
- Write verbs cross only with the role's explicit `allow_write: true`: `send`, `send-media`, `react`.
- A granted action uses the selected connection's own session. The worker session belongs to the receiving account and is left out, so one account is never used through another's session.
- Login and service-control verbs never cross, and a worker still cannot name an arbitrary `--session` path.
- The connection must exist in the project's registry and its own `allow_write` still governs it; a read-only connection stays read-only regardless of the role grant.

## State Layout

For account ID `8200881535`, runtime state is:

```text
$XDG_STATE_HOME/telegram/8200881535/session.session
$XDG_STATE_HOME/telegram/8200881535/service/register.json
$XDG_STATE_HOME/telegram/8200881535/service/health.json
$XDG_STATE_HOME/telegram/8200881535/service/progress/
$XDG_STATE_HOME/telegram/8200881535/service/worker-sessions/
$XDG_STATE_HOME/telegram/8200881535/service/daemon.log
$XDG_STATE_HOME/telegram/8200881535/service/daemon.pid
$XDG_STATE_HOME/telegram/8200881535/service/state-schema.json
$XDG_STATE_HOME/telegram/8200881535/control/ownership-v1.json
$XDG_STATE_HOME/telegram/8200881535/control/daemon.lock
$XDG_STATE_HOME/telegram/8200881535/control/owner.json
$XDG_STATE_HOME/telegram/8200881535/control/takeover.json
$XDG_STATE_HOME/telegram/8200881535/calls/recordings/<timestamp>-<chat>-call-<id>.ogg
$XDG_STATE_HOME/telegram/8200881535/calls/recordings/<timestamp>-<chat>-call-<id>.json
```

The auth session and service runtime files are separate. `TELEGRAM_SERVICE_STATE_DIR`
may relocate runtime files, but never the account-global `control/` directory.
`owner.json` names the actual bundle, launch nonce, project, connection, auth
session, state and health paths. The exact positive `service_state_version` in
the canonical payload, development payload and `state-schema.json` must agree;
live takeover never migrates or downgrades state. Marker-less pre-feature state
is version 1 and receives only an atomic version-1 marker from compatible code.

For a managed development session, use `capabilities dev live start <session>
telegram`. The manager stops only a matching canonical owner, launches the
session payload against the same auth session and watermark register, and keeps
a durable `takeover.json` transition lease. `dev live stop` restores the
recorded canonical state. After interruption, `dev live status` is read-only and
`capabilities dev live recover <session> telegram --restore-canonical` performs
the explicit fail-closed restoration. Auth and register files are never copied
or merged.

## Behavior

- Direct messages are accepted according to `direct_messages.mode` and `allowed_users`.
- Group messages are accepted only for `allowed_groups` and only when addressed by mention, reply, or configured alias unless the group policy sets `require_reference` to `false`.
- Each addressed message becomes its own queued dialogue job, capped per channel by `max_parallel_dialogue`.
- A Codex turn that exits successfully with `turn.completed` and an intentionally empty final answer completes silently: the job is marked done and nothing is posted to Telegram. An empty result without that protocol completion remains a worker error.
- The worker tail is a compact Tallinn-time timeline. Forum-topic messages automatically use a topic-specific channel key and fetch/filter only that topic; interleaved topics never share a tail, watermark, debounce, retry, progress, or dialogue slot. Ordinary groups and direct chats keep whole-chat behavior. Each message keeps its Telegram id and in-window reply topology.
- The daemon performs protocol catch-up plus bounded watermark reconciliation when a Telegram session connects and at the configured sync interval. This recovers messages received while it was down and update packets the MTProto client could not deserialize.
- `telegram service status` reports update-stream health from `health.json`; a live PID with a stale sync watermark is not reported as healthy.
- `telegram service reload` applies policy and worker/voice defaults without disconnecting an active call. Prompt files are already read per request or call; reload is for `settings.json`.
- A message is reserved in the persistent job register before voice transcription or any echo is attempted. Live re-delivery and startup catch-up therefore cannot transcribe or echo the same voice message twice.
- Group final replies and progress updates are sent as replies to the addressed message. Direct-chat replies are plain messages.
- `may_address` (default `true`) decides whether a participant may invoke the assistant in a group. Set it on `allowed_groups.<chat_id>.members.<user_id>` to silence someone in that one room, or on `allowed_users.<user_id>` for every group; a member entry setting it back to `true` wins over the global default. It applies to `kind: "human"` and `kind: "agent"` alike. A silenced sender's mention, alias, @username, Telegram reply, `/record`, and control commands all produce no worker job and no reply, including when the transcript of an ambient voice note names the assistant — the note is still transcribed and echoed. Their messages stay fully present in the worker tail and in the channel-state counterpart list. This is a group-addressing gate only: their direct chat with the assistant is still governed by `direct_messages.mode` and membership in `allowed_users`, and is unaffected.
- A group member entry may set `"kind": "agent"` for another automation peer, plus `"address_aliases": ["Solomon"]` for deliberate handoffs in the worker prompt. A Telegram reply from that peer is then context rather than an implicit invocation: the peer must explicitly name or mention the assistant. Final responses, progress, control responses, transcription echoes, and error notices triggered by that peer are sent as new group messages rather than Telegram replies. Set group-level `"agent_dialogue": {"max_turns": 4, "reset_on_human_message": true}` to hard-limit consecutive accepted agent turns; further agent requests are silently consumed until a human message resets the counter.
- `telegram send <chat> <text>` inside a worker writes to the daemon progress outbox instead of sending directly. The wrapper accepts exactly those three arguments, pins chat/topic/connection to the daemon-created authority scope, and rejects extra flags or destination/session/topic substitution before touching the outbox or Telegram.
- Workers can be `codex`, `claude`, or `stub`; `/set` and `/status` in Telegram adjust or inspect per-channel runtime settings when `control.roles` allows the sender role to run that command.
- Worker subprocesses run in dedicated process groups. Timeout, task cancellation, reconnect, and incomplete post-worker delivery all terminate that group and move the persisted job to a terminal error or startup-retry state.
- The daemon supervises its media recorder when at least one allowed group opts in. The recorder joins muted and uses PyTgCalls' built-in `RecordStream` for the complete joined interval. That supported path captures MP3; after Marvin leaves, FFmpeg converts the closed capture to the final OGG/Opus artifact. The source MP3 is removed only after successful conversion and is retained if conversion fails. The JSON sidecar stores the group, Telegram call id, joined interval, trigger, and participant state changes. It does not create a call or transcribe audio.

## Registered Jobs

Two classes of work run side by side, with separate budgets and one ledger.

A **dialogue turn** answers in the channel and is expected to finish while the person is still there. Its unit is the addressed message, its record is the watermark register, and `max_parallel_dialogue` caps how many one channel runs at once.

A **registered job** is work that outlives the sentence that asked for it. It is a row in `tg_worker_jobs` in the shared capabilities store, drained by the job runner in arrival order within `max_parallel_jobs` slots for the whole daemon. `job_poll_interval` is how often the runner looks.

### State and outcome

A job answers two questions, and only one of them changes what anything does.

`state` is the whole of the runner's interest: **`waiting`** for a slot, **`running`** in one, or **`stopped`** in neither. Nothing else is a state, because nothing else changes what may happen next — the session is the checkpoint, so work that stopped for any reason at all continues from where it stopped, and a job that never started continues by starting. There is no `finished`: it would differ from `stopped` only in the label, and the label has a column of its own.

`outcome` is that label — why a stopped job stopped: **`succeeded`**, **`failed`**, **`cancelled`** (somebody asked), **`interrupted`** (the daemon restarted under it), **`quota`** (the subscription is spent). Each value earns its place by behaving differently: `quota` is continued by the runner when the pause lifts, `interrupted` by the `job_recovery` policy, the rest only when somebody asks. It says nothing while the job is waiting or running.

### The verb surface

The register is a table, so `telegram jobs` reads and writes it directly rather than asking the daemon. That is what makes it usable at all: the person looking at a stuck queue is at a terminal, and the worker deciding whether a message amends running work is a subprocess — neither is the daemon.

```
telegram jobs list [--state S] [--outcome O] [--chat C] [--topic-id T] [--limit N]
telegram jobs active [--chat C] [--topic-id T]
telegram jobs show <id>
telegram jobs register "<one line>" --chat C --requested-by U [--engine E]
telegram jobs amend <id> "<what changed>"
telegram jobs stop <id>
telegram jobs resume <id>
```

**One way to halt work and one way to continue it.** There is no second pair for the stop that is meant to be final, because every stop is continuable: what a caller decides is not whether the work can come back — it always can — but whether anybody asks it to. `stop` and `resume` are asks about a direction rather than transitions, so both are idempotent and neither refuses a state: resuming work that is already moving withdraws a stop nobody wants any more.

Both are asynchronous. `stop`, and the staged text behind `amend`, record what was asked; the runner holds the process groups and acts on the next tick. A read reports an unlanded ask as `stopping: true`, because a flag and a state are two halves of one sentence.

Inside a worker turn the same commands run through the shim, which pins `--chat` and `--topic-id` to the authorized channel and refuses any attempt to name another. There is no daemon round-trip and no separate grammar.

### What the worker is told, and what it asks for

The prompt names the surface; it does not carry a snapshot of it. The queue moves while a turn is being written, so a list pasted in at dispatch is already stale when it is read — the worker runs `telegram jobs active` at the moment it needs to know. Attribution is still a judgement made in the prompt rather than a mechanism, and a wrong one degrades to a redundant new job.

### Amendment keeps the row

A correction is not a new request. `amend` stages the added context against the job; the runner stops that job's process group, counts the amendment, and continues the same engine session by its id. Nothing marks the intent separately — the staged text being there *is* the intent, and it is consumed when the job is next dispatched. Amending stopped work brings it back to `waiting` for the same reason: being corrected is a reason to continue. Stopping loses nothing the turn had established, because the session is the checkpoint.

### What the row holds

References, never content: what was asked, whose authority it carries, which channel it reports into, how long it waited, and why it stopped. `session_id` names the rollout, `log_path` names the process output, `engine` names what must run it — a job registered under one engine is continued under that engine, because resumption is engine-specific. `effort` and `service_tier` are deliberately absent; the session's own `turn_context` already records them.

### Isolation is a correctness condition

Every queue read is filtered by `project_id`, `environment` and `surface`, so one project can never take another's work and a development daemon can never take a production job off a store they share. `environment` is settings' own top-level key, overridden by `TELEGRAM_ENVIRONMENT`.

### Restart, quota, reporting

On start, rows claiming to be running stop with outcome `interrupted` and their surviving process groups are terminated. `job_recovery` decides what happens next: `requeue` continues them as a fresh attempt, `inspect` leaves them for a person to look at.

When the engine reports the subscription spent, the running job stops with outcome `quota`, the queue stops taking new work, and nothing is retried into the wall. The dialogue worker is told, in words, including when the queue tries again.

A job reports into its own channel, as a reply to the originating message. Progress lines use the same outbox a dialogue turn does.

The register needs the store. Where a project keeps its *configuration* is a separate question, answered by `project.json`; a queue lives in the store either way. Without a project identity the register cannot open, the job class is off for the session, and the conversation is unaffected.

## Group Call Recording

Call recording is disabled unless an allowed group explicitly selects a mode:

```json
{
  "allowed_groups": {
    "-100123": {
      "name": "Recorded automatically",
      "call_recording": {
        "mode": "auto",
        "send_to_chat": true
      }
    },
    "-100456": {
      "name": "Recorded when asked",
      "call_recording": {"mode": "on_request"}
    }
  }
}
```

- `auto`: the daemon detects an active group voice/video chat and joins to record it.
- `on_request`: an addressed `Marvin, запиши звонок`-style message or `/record` creates a recording request for an already active call.
- `disabled`: no media worker is allowed to join for this group. This is the default.
- `send_to_chat: true`: after the OGG container closes, upload it to the same group as two messages: a short `Запись звонка · <duration>` notice followed by a downloadable Telegram voice note with a waveform generated from the finalized OGG. The sidecar stores both Telegram message ids. Failed uploads are retried up to three times without repeating a notice that was already sent. The default is `false`. Delivery starts only after the MP3-to-OGG conversion succeeds; the recorder does not classify or reject a completed recording based on its loudness.

Transient Telegram join failures are retried without crashing the assistant daemon. After a successful join, one recording runs until Marvin leaves or the voice chat closes. The daemon does not split that interval into media fragments or automatically rejoin the same call after Marvin has left it.

Only one call can be recorded by one Telegram account at a time. The first version stores the incoming mixed stream; participant IDs and audio-source identifiers are captured in participant snapshots, but the OGG does not contain separate per-participant tracks.

The service never starts a group call itself. It posts the completed recording only when `send_to_chat` is enabled; participant notice remains the operator's responsibility.

## Direct Calls

An incoming one-to-one call is handled by two independent per-user switches under `allowed_users`. Both default to off, and the daemon answers only when at least one is on:

```json
{
  "allowed_users": {
    "1000000001": {
      "name": "Supervisor",
      "role": "supervisor",
      "call_recording": {"mode": "auto"},
      "voice_agent": {"mode": "auto"}
    }
  }
}
```

| `voice_agent` | `call_recording` | Behaviour |
|---|---|---|
| off | on | The call is answered and recorded to a single mixed track: MP3 capture, converted to OGG/Opus, delivered to the caller's direct chat. |
| on | on | The assistant holds a spoken conversation and the call is recorded as a stereo OGG — caller left, assistant right — delivered to the caller's direct chat. |
| on | off | Conversation only; nothing is written and nothing is delivered. |
| off | off | The call is not answered. |

`voice_agent` accepts optional `model`, `voice`, and `history` overrides for that one user. Each of the three resolves per-user first, then `defaults.voice_agent`, then the built-in. `history` is how many messages of that direct chat are carried into the call prompt.

The voice agent runs on the daemon's own Telegram connection and its own PyTgCalls instance. An account may have exactly one connection consuming the update stream: a second one takes the incoming-call update and the first never sees it, so no part of call handling may open another client.

The two media slots of one direct call are independent, and both honour the requested audio parameters exactly. The inbound slot delivers 16 kHz mono PCM — the format the speech model consumes — and the outbound slot accepts 24 kHz mono PCM, the format it emits, so no resampling happens in either direction. Inbound frames are batched to about 100 ms before being sent upstream; the outbound slot is fed on a 10 ms tick and sends silence when nothing is buffered, so the stream never stalls. When the caller interrupts, queued speech is dropped.

Recording a voice-agent call therefore writes two raw tracks against one shared time origin, each padded with silence up to that origin, which FFmpeg joins into one stereo Opus file with real speaker separation. Both call paths refuse to deliver a recording that captured no audio.

The system prompt is the project's own resolved Telegram service `voice-agent.md`, the time the call is happening, then the recent tail of that direct chat. The launcher resolves absolute project layers once and hands a snapshot to the daemon; ContextKit is consulted only by the owner/launcher when the project is bound to it. On controlled reload a fully valid new settings/layout pair replaces the old snapshot atomically. `read_project_file` allows only the resolved layer roots and still denies traversal, symlink escapes, credentials, secrets, state, sessions, databases, and key material.

The call is answered before the prompt is built. Reading a long chat tail first expires the ring window, after which the call layer tries to *place* a call instead of accepting one; so the daemon claims both media slots, then reads the tail, then sets the instruction and opens the speech session. The cost of that order is a second of dead air after pickup rather than a call that cannot be answered at all.

A second tool, `send_to_chat`, writes into the caller's chat mid-call — for a link, an exact spelling, or a list that speech carries badly. An authorised supervisor also receives `reload_service`, which applies `settings.json` in the daemon itself; it never delegates to a worker and never ends the call.

### The record of a call

When a call ends, its transcript is written up into a short summary and posted into the caller's chat as a reply to the recording, so the two read as one thing and the summary enters the next call's tail through the ordinary path. Audio is not context; the summary is what a later conversation reads. It is written from the transcript, not the audio, and generated while the recording is still being joined and uploaded. A call with no recording still gets its summary, standing alone, and a summary that fails never costs the recording.

`recording_caption` is the line the recording is announced with; the summary reply carries no heading of its own. Left unset, the built-in caption is used.

### Reading from a call

A caller asking what something *is* — a balance, a status, what is on a board, what was agreed with a supplier — should not wait on a worker. Two tools answer inside the turn that asked.

`run_capability` runs one of the project's capability CLIs and returns its output. It takes the capability's name and its arguments as separate items, and runs them with no shell, so a pipe or a redirect is only ever part of an argument. It runs under the authority that caller's *messages* resolve to — the same resolution `agent_task` uses, with the same `CAPABILITIES_AUTH_CONTEXT` handed to the process — so a call reaches exactly what a message from that person reaches. A capability the caller's role does not name is refused before anything is spawned, and the CLI's own exit 4 is honoured behind that.

Two things are decided for the model rather than asked of it. Output is bounded before it reaches a speech model, and where it was cut is said, because a silent truncation reads as a whole answer. And the first call to a capability on a call is answered by the daemon itself with that capability's `help` and its `ids list`, not by running what was asked: a model guessing a flag or an identifier spends the caller's seconds three times over, and the round trip is owed anyway. A capability's own contract verbs — `help`, `guide`, `ids`, `connections`, `refs`, `stub`, `manifest` — pass straight through, since reaching for one is already the behaviour that gate exists to produce.

`read_project_file` opens one file of the project body — a reference, a routine, a settings file. Only the project's own material roots can be opened, paths are resolved before they are judged so `..` and symlinks are caught by where they land, and credentials, session files and state directories are refused inside those roots. A guide belonging to a capability is not read this way: it is asked of the capability.

Both are for reading. Anything that changes state belongs to `agent_task`, whose worker can weigh what it is about to do. A tool that raises costs its own answer and never the call — the failure is returned to the model as something to say, not to the stream as an exception.

### Doing work from a call

`agent_task` hands a task to the project's worker — the same worker a message runs, launched the same way — and returns immediately, so the conversation is never blocked on it. An identical task already running is not started twice, and **exactly one task runs per call**. That bound is fixed in code and is deliberately not a setting: several workers on one call race each other's progress and answers into the conversation, and the caller cannot tell which reply belongs to which question. A second request is refused with instructions to say it will be done after the current one.

When the result lands it is injected between turns, never into speech in progress, and the assistant tells the caller. A result taken off the queue is held until it has actually been handed over, so a call ending while waiting for a pause cannot swallow it; and the wait for a pause is bounded, after which the assistant is interrupted rather than the result lost. If the call ended first, the result is delivered to the caller's direct chat as a message instead, so an answer the caller asked for always reaches them. Hanging up is the caller's own button; there is no tool for it.

While a task runs, the caller hears roughly where it stands. Progress is read from the worker's own event stream rather than asked for — a fast worker works silently — and from any line the worker writes itself, which outranks a derived one. Events are accumulated over the interval and folded into where the work stands now, never reported as "the last thing that happened": a worker's last step is often something that failed and it then routed around, and reporting that tells the caller a failure that is not one. What reaches the caller is bounded independently of how talkative the worker is — at most one note per `progress_interval`, only into a genuine lull, and never the same note twice. Progress goes to the assistant only; the worker is told not to address the caller, and the answer it returns at the end is the result.

A task runs under the authority the *same user's messages* resolve to: role and `authority.allowed_capabilities` come from the one resolution the direct-message path uses, given the caller id as sender. A call therefore reaches exactly what a message from that person reaches, and no more.

How the channel behaves is `defaults.voice_agent`, beside the text worker's policy in the same file — worker policy never lives in the connection entry, which carries identity and secret references only:

```json
{
  "defaults": {
    "voice_agent": {
      "worker": "claude",
      "workers": {"claude": {"model": "<a fast model id>", "effort": null}},
      "voice": null,
      "history": null,
      "timezone": "<an IANA zone name>",
      "progress_interval": 10,
      "recording_caption": null,
      "prompt_file": null
    }
  }
}
```

Every field is optional. `worker` and `workers` fall back to the project's existing worker settings (`defaults.worker`, `defaults.workers`). `voice`, `history` and the worker's `model` are also settable per user, which wins over these. `timezone` is an IANA zone name and is what the call states as "now" and stamps the chat tail with; unset or unparseable, it is UTC — never the host's zone, so a call never quietly reports the wrong time. `progress_interval` is how often a running task may report in, in seconds. `prompt_file` overrides which file the call speaks from, relative to the service directory unless absolute.

Pick a **fast, non-reasoning model** here: work asked for by voice is operational and quick — look something up, check or file a ticket, note something down — not coding, and the caller is waiting on the line. The timeout is the text worker's `defaults.worker_timeout`; task size is bounded by the model choice, not by cutting the clock short.

An answered call needs a Gemini API key, in the environment variable the selected connection names as `gemini_secret_env` (`GOOGLE_API_KEY` when the entry omits it), resolved like every other credential. Without the key, or without `voice-agent.md`, the daemon logs which one is missing and a caller who also has `call_recording` on is recorded as usual instead.

## Voice Transcription

Voice messages and video notes addressed to the assistant are always transcribed in both direct messages and groups. Groups may additionally enable ambient transcription:

```json
{
  "allowed_groups": {
    "-100789": {
      "name": "Ambient voice enabled",
      "voice_transcription": {"mode": "auto"}
    },
    "-100999": {
      "name": "Addressed only",
      "voice_transcription": {"mode": "disabled"}
    }
  }
}
```

- `auto`: All Telegram voice messages and video notes from participants are transcribed and echoed to the chat. In groups, the echo is sent as a Telegram reply to the original spoken-media message (Telegram's reply preview shows sender attribution), and the echo content itself contains only the blockquoted transcript. In direct messages, the echo uses the "Твоё сообщение:" prefix. After transcription, the transcript is checked against the same configured assistant name/username/group-alias matching semantics used for text messages. If the spoken transcript names the assistant (e.g., "Marvin" or "Assistant"), the media message is treated as an addressed assistant request and dispatches a worker. If the transcript does not name the assistant, it is echoed without creating a worker job. Spoken media already addressed via Telegram-level mention/reply dispatch as normal regardless of transcript content. In conversation history provided to workers, these echoes are attributed to the original sender via the reply relationship, not to the assistant.
- `disabled`: Only spoken media addressed to the assistant are transcribed. This is the default.

Runtime overrides are available per channel via `/set voice-transcription auto|disabled`.

Transcription failures produce a fallback message. The daemon reserves ownership before transcription so duplicate deliveries and restart catch-up cannot transcribe the same spoken-media message twice.

## Channel Context

The global soft prompt lives in `capabilities/telegram/service/context.md`. Group policies may add a channel-specific overlay with either a markdown file or a short inline string. File paths are relative to `capabilities/telegram/service/`.

```json
{
  "allowed_groups": {
    "-100123": {
      "name": "Family",
      "member_role": "group_member",
      "context_file": "context/family.md"
    },
    "-100456": {
      "name": "Small Team",
      "member_role": "group_member",
      "context": "Keep replies brief and operational in this channel."
    }
  }
}
```

A forum topic may add its own overlay under its group's `topics` map, and a direct sender may add one on their `allowed_users` entry. Topic prose follows room prose rather than replacing it, so a topic says what is specific to its lane while the room keeps saying what is true everywhere in it.

The prompt order is: global `context.md`, channel context overlay, daemon channel state, current request, then the recent conversation tail. Channel context is a soft behavior layer only; access control still belongs to `control.roles` and tool access still belongs to `authority.roles`.

### Exclusive channel prose

`context_mode` decides whether a level's prose joins what came before it or stands alone. It accepts `extend`, the default, and `exclusive`, and it may be declared on a group, on a topic entry, and on a direct sender.

Exclusivity cuts every layer above the level that declares it, and never a layer below. A topic set to `exclusive` answers with its own prose alone — the room's overlay and the global `context.md` both drop away. A group set to `exclusive` drops `context.md` while its topics still add their own lines on top of the room's.

```json
{
  "allowed_groups": {
    "-100123": {
      "context": "House rules for this room.",
      "topics": {
        "12": {
          "project": "~/dev/storefront-api",
          "context": "You are the API service assistant. Answer only about this service.",
          "context_mode": "exclusive"
        }
      }
    }
  }
}
```

An exclusive channel takes over everything `context.md` was saying: who the assistant is, how it is addressed, how it introduces itself, how it treats history, and when to report progress. Write those into the channel's own prose, or the channel will not have them.

What exclusivity never removes is the daemon-resolved state that a worker needs to answer at all — the channel and its id, participants and roles, active settings, tool authority, delivery, and the progress command for this request. The progress command is a fact about the request rather than prose, so it is stated in the channel state block whenever the prose does not already name it, and prose naming it in either spelling is rewritten to the real command wherever that prose came from.

## Worker Project Routing

A chat, a forum topic, or a direct sender can name the project its worker runs in with a `project` property. Resolution order is topic, then chat, then direct sender, then the daemon's own project — the daemon's project is the row that results when nothing matched, not a privileged default.

```json
{
  "allowed_groups": {
    "-100123": {
      "name": "Storefront",
      "project": "~/dev/storefront",
      "topics": {
        "12": {
          "project": "~/dev/storefront-api",
          "context": "This topic is the API service. Work there, not in the web app."
        }
      }
    }
  },
  "allowed_users": {
    "42": {"name": "Owner", "project": "~/dev/notes"}
  }
}
```

The daemon does not resolve the target project; it stops asserting its own. The service launcher pins `CAPABILITIES_PROJECT_ENVELOPE` to the daemon's project, and a capability CLI invoked with that variable pointing outside its resolved project exits 6. A routed worker therefore has that pin removed and receives `CLAUDE_PROJECT_DIR` naming the target, which every capability CLI reads before it falls back to the working directory.

Routing changes which rights the worker has, not only where it runs. The target project's capability gate governs the routed worker, so a capability must be enabled there — `capabilities enable <name> --project` inside that project, or `--global` for every project — before the worker can call it. Replies and progress are unaffected: `telegram send` from a worker is intercepted by the worker shim and delivered by the daemon's own client, so it never depends on the worker's directory or on the target project's policy.

A route is judged twice. Its shape is checked when settings load: a string, absolute after expansion, and inside `$HOME`. Its reachability is checked when a message arrives: the target must exist, be a directory, and carry a project marker (`capabilities/settings.json`, `.contextkit/config.toml`, `.capabilities`, `.env`, `.env.local`, or `.git`). A target that fails at dispatch fails that one request and says so in the chat, because a neighbouring repository that was renamed must not stop the daemon that serves every other channel.

### General

Telegram lists a forum's General as topic `1`, but marks nothing on the wire: General messages carry no forum-topic flag and arrive looking like ordinary chat messages. A chat that declares a `topics` map has asked for its rooms to be addressable by name, so General resolves to topic `1` there and can be routed and given prose like any other room. A chat that declares no map keeps General on the bare chat key, where a plain group's messages belong and where a reply chain must not become a topic of its own.

Declare `topics` only on a forum group. On a plain group the map would key its messages on a topic Telegram never made, and history reads for that key ask for the replies of a non-topic.

The first reload after a chat gains a `topics` map moves that chat's existing register row onto the General key, so catch-up resumes where it stopped instead of replaying the room.

The resolved map is printed at start and carried in `telegram service status` as `routes`, and it is refreshed on every settings reload. A route that points at a directory which exists and is a project, but the wrong one, runs there silently; reading the map once is the mitigation.

The voice path is deliberately not routed. A live call resolves project files and runs its capability subprocesses in the daemon's own project, so in a routed group the text worker works in the target project while a voice answer describes the daemon's project.

## Agent Messages

Two assistants can share a room, each with its own daemon answering on its own. The hazard is not the conversation but the consumer: when a live session asks the peer a question, the answer belongs to that session, and a second daemon answering it speaks for the same account with none of the session's context.

Two tags settle who consumes an answer. Both count only as the message's **last line, standing alone** — prose that names a tag is a conversation about the protocol, not the protocol, and agents discuss these tokens.

- `#external` on a request says its answer is consumed by a live session.
- `#noreply` on a message says no daemon may act on it.

The daemon decides both, never the model. A request carrying `#external` is recorded on the job, and every message the daemon sends for that job carries `#noreply`: the final answer, each chunk of a long one, a media caption, every progress line, and the error notice a failed job delivers. A peer daemon reading a message tagged `#noreply` treats it as not addressed to it, whatever else the message does — reply, mention, or name.

Send a tagged request with `telegram send <chat> <text> --external`, which appends the tag as its own last line. The literal works identically when written into the text, which is how an assistant inside a worker sends one, since the worker shim accepts no flags.

The tags are mechanics, not configuration: they work in every chat and need no policy. Three properties follow from where they are checked.

A tagged message stays in context. Suppression happens at the invocation gate, while the worker's tail is read live from Telegram, so the next job in that room still sees what was said.

`#external` never demands an answer. It shapes an answer that would happen anyway; a request nobody was addressed by produces nothing.

`agent_dialogue.max_turns` is a runaway bound, not a way to arrange this. Its counter resets only when a non-agent writes, so in a room of two assistants and no humans it latches at the cap and the assistant falls permanently silent to its peer.

## Control Authority

Service control commands are handled by the daemon before a worker job exists, so they are governed by `control.roles` instead of `authority.roles`. `/status` is safe to expose broadly; `/set` changes per-channel runtime settings; `/reload` validates and reapplies `settings.json` without disconnecting; `/stop` stops queued/running work for the channel.

```json
{
  "control": {
    "roles": {
      "supervisor": {
        "commands": ["status", "set", "stop", "help"]
      },
      "channel_admin": {
        "commands": ["status", "set", "help"]
      },
      "group_member": {
        "commands": ["status", "help"]
      }
    }
  }
}
```

## Tool Authority

The service creates a per-job `CAPABILITIES_AUTH_CONTEXT` file for workers when `settings.json` declares an `authority` policy. Capability CLIs read this file before resolving credentials; an unlisted capability exits with policy refusal (`exit 4`). This is a hard gate for normal capability use, while `context.md` remains soft behavioral guidance.

Role policies live under `authority.roles.<role>.allowed_capabilities`:

```json
{
  "authority": {
    "roles": {
      "supervisor": {
        "allowed_capabilities": { "*": true }
      },
      "group_member": {
        "allowed_capabilities": {
          "telegram": { "scope": "current_chat" },
          "routine": true
        }
      }
    }
  }
}
```

For group members, keep personal or administrative capabilities such as `mailbox`, `coolify`, or external write tools absent unless the project has a deliberate reason to expose them. The bundled worker `telegram` wrapper also honors `scope: current_chat` for chat-addressed Telegram commands.

To migrate a project that copied a service directory, delete the copied engine files after installing the bundled capability. Keep or move only the project policy/context files into `capabilities/telegram/service/` and keep the connection/session state under `$XDG_STATE_HOME/telegram/<connection>/`.
