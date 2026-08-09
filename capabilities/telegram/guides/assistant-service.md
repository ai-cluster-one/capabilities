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
   capabilities enable telegram
   telegram service init
   ```

3. Edit `capabilities/telegram/service/settings.json`:

   - Set `connection` to the Telegram account ID or rely on the account-ID default in `capabilities/telegram/connections.json`.
   - Add `allowed_users` and `allowed_groups`.
   - Add optional per-channel `context_file` or short inline `context` entries when a chat needs its own soft prompt overlay.
   - Review `control.roles`: this hard gate limits who may run service control commands such as `/set`, `/reload`, and `/stop`.
   - Review `authority.roles`: this request-scoped hard gate limits which capability CLIs a worker may invoke for each sender role.
   - Set group `aliases` / `address_aliases` if the assistant should react to names other than the default.
   - Set a group's `call_recording.mode` to `auto`, `on_request`, or `disabled`. Recording is opt-in per group and defaults to `disabled`.
   - Set a group's `voice_transcription.mode` to `auto` to transcribe all voice notes from participants (unaddressed voices are echoed without creating worker jobs). Defaults to `disabled`.
   - Choose `defaults.worker`: `codex`, `claude`, or `stub`.

4. Ensure the selected connection can send replies:

   ```json
   {
     "default": "8200881535",
     "connections": {
       "8200881535": {
         "name": "Assistant",
         "username": "assistant_username",
         "api_id": 123456,
         "secret_env": "TELEGRAM_API_HASH",
         "allow_write": true
       }
     }
   }
   ```

   The connection key (`8200881535`) is the stable Telegram user/account ID;
   `api_id` is the separate Telegram API application ID. `name` and `username`
   are display metadata. For an existing labelled key such as `assistant`, run
   `telegram login --connection assistant` once: the result reports the actual
   account ID and an explicit rename/default migration. Labels are never
   silently reinterpreted as identities.

5. Authenticate and check readiness:

   ```sh
   telegram login --connection 8200881535
   telegram doctor --connection 8200881535
   telegram service doctor --connection 8200881535
   ```

6. Start and inspect the service:

   ```sh
   telegram service start --connection 8200881535
   telegram service status --connection 8200881535
   telegram service logs --connection 8200881535 --tail 80
   ```

Use `telegram service stop` or foreground `run` for supervisor-managed processes. On macOS/local dev, `start` uses a background process with a PID file under the connection's service state directory.

After editing `settings.json`, use `telegram service reload`. The complete strict schema is validated before publishing: unknown properties, invalid shapes/enums/bounds, and unsafe overlay paths are errors with their full JSON path. A rejected cold start/doctor/init check never begins service work; a rejected reload keeps the previous immutable settings and project-layout snapshot active. Changing the selected connection still requires a real restart. The same operation is available as `/reload` and as the live-call `reload_service` tool only when the caller's `control.roles` policy permits `reload` (the shipped default grants it only to `supervisor`). A worker may invoke the CLI form only when its request authority role is `supervisor`; an external terminal without request authority remains an operator surface.

The accepted schema is deliberately finite:

- Top level: `connection`, `assistant_name`, `direct_messages`, `allowed_users`, `allowed_groups`, `control`, `authority`, and `defaults`.
- `direct_messages`: `mode` and `default_role`.
- User/member policy: display `name`/`username`, `role`, `control`, `authority`, `allowed_capabilities` (or `capabilities`), `context`/`context_file`, `call_recording`, and `voice_agent`; group members additionally accept `kind` and `address_aliases`.
- Group policy: `name`, `role`/`member_role`, `aliases`/`address_aliases`/`mentions`, `require_reference`, `members`, `agent_dialogue`, `worker_timeout`, `voice_transcription`, `call_recording`, `control`, `authority`, `allowed_capabilities` (or `capabilities`), and `context`/`context_file`.
- Defaults: `assistant_name`, `tail_size`, `sync_interval`, `sync_stale_after`, `debounce`, `worker_timeout`, `progress_after`, `max_parallel_jobs`, `max_attempts`, `group_aliases`, `worker`, `workers`, and `voice_agent`.
- Worker policy: `model`; Claude also accepts `effort`; Codex accepts `reasoning_effort` and `service_tier`. Voice defaults accept `worker`, `workers`, `model`, `voice`, `greeting`, `history`, `timezone`, `progress_interval`, `recording_caption`, and `prompt_file`.
- Control policies contain `commands`; authority policies contain `allowed_capabilities` or `capabilities`. Capability rules accept booleans/`*`, verb lists, or `allow`/`deny`/`enabled`/`scope`/`verbs` objects.

IDs must have the correct sign (users positive, groups negative), paths must resolve inside the Telegram service directory, and numeric settings use the bounds named by `/set help` or the shipped template. There is no permissive/legacy mode and no support for a `project` routing property.

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
$XDG_STATE_HOME/telegram/8200881535/calls/recordings/<timestamp>-<chat>-call-<id>.ogg
$XDG_STATE_HOME/telegram/8200881535/calls/recordings/<timestamp>-<chat>-call-<id>.json
```

The auth session and service runtime files are separate. Worker session copies let `telegram download` run inside workers without contending on the daemon's Telethon SQLite session.

## Behavior

- Direct messages are accepted according to `direct_messages.mode` and `allowed_users`.
- Group messages are accepted only for `allowed_groups` and only when addressed by mention, reply, or configured alias unless the group policy sets `require_reference` to `false`.
- Each addressed message becomes its own queued job.
- A Codex turn that exits successfully with `turn.completed` and an intentionally empty final answer completes silently: the job is marked done and nothing is posted to Telegram. An empty result without that protocol completion remains a worker error.
- The worker tail is a compact Tallinn-time timeline. Forum-topic messages automatically use a topic-specific channel key and fetch/filter only that topic; interleaved topics never share a tail, watermark, debounce, retry, progress, or parallel-job slot. Ordinary groups and direct chats keep whole-chat behavior. Each message keeps its Telegram id and in-window reply topology.
- The daemon performs protocol catch-up plus bounded watermark reconciliation when a Telegram session connects and at the configured sync interval. This recovers messages received while it was down and update packets the MTProto client could not deserialize.
- `telegram service status` reports update-stream health from `health.json`; a live PID with a stale sync watermark is not reported as healthy.
- `telegram service reload` applies policy and worker/voice defaults without disconnecting an active call. Prompt files are already read per request or call; reload is for `settings.json`.
- A message is reserved in the persistent job register before voice transcription or any echo is attempted. Live re-delivery and startup catch-up therefore cannot transcribe or echo the same voice message twice.
- Group final replies and progress updates are sent as replies to the addressed message. Direct-chat replies are plain messages.
- A group member entry may set `"kind": "agent"` for another automation peer, plus `"address_aliases": ["Solomon"]` for deliberate handoffs in the worker prompt. A Telegram reply from that peer is then context rather than an implicit invocation: the peer must explicitly name or mention the assistant. Final responses, progress, control responses, transcription echoes, and error notices triggered by that peer are sent as new group messages rather than Telegram replies. Set group-level `"agent_dialogue": {"max_turns": 4, "reset_on_human_message": true}` to hard-limit consecutive accepted agent turns; further agent requests are silently consumed until a human message resets the counter.
- `telegram send <chat> <text>` inside a worker writes to the daemon progress outbox instead of sending directly. The wrapper accepts exactly those three arguments, pins chat/topic/connection to the daemon-created authority scope, and rejects extra flags or destination/session/topic substitution before touching the outbox or Telegram.
- Workers can be `codex`, `claude`, or `stub`; `/set` and `/status` in Telegram adjust or inspect per-channel runtime settings when `control.roles` allows the sender role to run that command.
- Worker subprocesses run in dedicated process groups. Timeout, task cancellation, reconnect, and incomplete post-worker delivery all terminate that group and move the persisted job to a terminal error or startup-retry state.
- The daemon supervises its media recorder when at least one allowed group opts in. The recorder joins muted and uses PyTgCalls' built-in `RecordStream` for the complete joined interval. That supported path captures MP3; after Marvin leaves, FFmpeg converts the closed capture to the final OGG/Opus artifact. The source MP3 is removed only after successful conversion and is retained if conversion fails. The JSON sidecar stores the group, Telegram call id, joined interval, trigger, and participant state changes. It does not create a call or transcribe audio.

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

The system prompt is the project's own resolved Telegram service `voice-agent.md`, the time the call is happening, the optional compiled project body, then the recent tail of that direct chat. The launcher resolves absolute project layers once and hands a snapshot to the daemon; ContextKit is consulted only by the owner/launcher when the project is bound to it. The daemon neither imports nor invokes ContextKit, and a missing compiled body never prevents a call. On controlled reload a fully valid new settings/layout pair replaces the old snapshot atomically. `read_project_file` allows only the resolved layer roots and still denies traversal, symlink escapes, credentials, secrets, state, sessions, databases, and key material.

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

Voice notes addressed to the assistant are always transcribed in both direct messages and groups. Groups may additionally enable ambient voice transcription:

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

- `auto`: All Telegram voice notes from participants are transcribed and echoed to the chat. In groups, the echo is sent as a Telegram reply to the original voice message (Telegram's reply preview shows sender attribution), and the echo content itself contains only the blockquoted transcript. In direct messages, the echo uses the "Твоё сообщение:" prefix. After transcription, the transcript is checked against the same configured assistant name/username/group-alias matching semantics used for text messages. If the spoken transcript names the assistant (e.g., "Marvin" or "Assistant"), the voice note is treated as an addressed assistant request and dispatches a worker. If the transcript does not name the assistant, it is echoed without creating a worker job. Voices already addressed via Telegram-level mention/reply dispatch as normal regardless of transcript content. In conversation history provided to workers, voice echoes are attributed to the original sender via the reply relationship, not to the assistant.
- `disabled`: Only voice notes addressed to the assistant are transcribed. This is the default.

Runtime overrides are available per channel via `/set voice-transcription auto|disabled`.

Transcription failures produce a fallback message. The daemon reserves ownership before transcription so duplicate deliveries and restart catch-up cannot transcribe the same voice note twice.

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

The prompt order is: global `context.md`, channel context overlay, daemon channel state, current request, then the recent conversation tail. Channel context is a soft behavior layer only; access control still belongs to `control.roles` and tool access still belongs to `authority.roles`.

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
