# Telegram Assistant Service

The Telegram assistant service is bundled with the `telegram` capability. The
project stores policy and context only; the daemon engine runs from the installed
capability bundle.

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

   - Set `connection` or rely on the default in `capabilities/telegram/connections.json`.
   - Add `allowed_users` and `allowed_groups`.
   - Add optional per-channel `context_file` or short inline `context` entries
     when a chat needs its own soft prompt overlay.
   - Review `control.roles`: this hard gate limits who may run service
     control commands such as `/set` and `/stop`.
   - Review `authority.roles`: this request-scoped hard gate limits which
     capability CLIs a worker may invoke for each sender role.
   - Set group `aliases` / `address_aliases` if the assistant should react to names other than the default.
   - Set a group's `call_recording.mode` to `auto`, `on_request`, or `disabled`.
     Recording is opt-in per group and defaults to `disabled`.
   - Set a group's `voice_transcription.mode` to `auto` to transcribe all
     voice notes from participants (unaddressed voices are echoed without
     creating worker jobs). Defaults to `disabled`.
   - Choose `defaults.worker`: `codex`, `claude`, or `stub`.

4. Ensure the selected connection can send replies:

   ```json
   {
     "default": "assistant",
     "connections": {
       "assistant": {
         "api_id": 123456,
         "secret_env": "TELEGRAM_API_HASH",
         "allow_write": true
       }
     }
   }
   ```

5. Authenticate and check readiness:

   ```sh
   telegram login --connection assistant
   telegram doctor --connection assistant
   telegram service doctor --connection assistant
   ```

6. Start and inspect the service:

   ```sh
   telegram service start --connection assistant
   telegram service status --connection assistant
   telegram service logs --connection assistant --tail 80
   ```

Use `telegram service stop` or foreground `run` for supervisor-managed
processes. On macOS/local dev, `start` uses a background process with a PID file
under the connection's service state directory.

## State Layout

For a connection named `assistant`, runtime state is:

```text
$XDG_STATE_HOME/telegram/assistant/session.session
$XDG_STATE_HOME/telegram/assistant/service/register.json
$XDG_STATE_HOME/telegram/assistant/service/health.json
$XDG_STATE_HOME/telegram/assistant/service/progress/
$XDG_STATE_HOME/telegram/assistant/service/worker-sessions/
$XDG_STATE_HOME/telegram/assistant/service/daemon.log
$XDG_STATE_HOME/telegram/assistant/service/daemon.pid
$XDG_STATE_HOME/telegram/assistant/calls/recordings/<timestamp>-<chat>-call-<id>.ogg
$XDG_STATE_HOME/telegram/assistant/calls/recordings/<timestamp>-<chat>-call-<id>.json
```

The auth session and service runtime files are separate. Worker session copies
let `telegram download` run inside workers without contending on the daemon's
Telethon SQLite session.

## Behavior

- Direct messages are accepted according to `direct_messages.mode` and
  `allowed_users`.
- Group messages are accepted only for `allowed_groups` and only when addressed
  by mention, reply, or configured alias unless the group policy sets
  `require_reference` to `false`.
- Each addressed message becomes its own queued job.
- The daemon performs protocol catch-up plus bounded watermark reconciliation when a
  Telegram session connects and at the configured sync interval. This recovers
  messages received while it was down and update packets the MTProto client could
  not deserialize.
- `telegram service status` reports update-stream health from `health.json`; a live
  PID with a stale sync watermark is not reported as healthy.
- A message is reserved in the persistent job register before voice transcription or
  any echo is attempted. Live re-delivery and startup catch-up therefore cannot
  transcribe or echo the same voice message twice.
- Group final replies and progress updates are sent as replies to the addressed
  message. Direct-chat replies are plain messages.
- `telegram send <chat> <text>` inside a worker writes to the daemon progress
  outbox instead of sending directly.
- Workers can be `codex`, `claude`, or `stub`; `/set` and `/status` in Telegram
  adjust or inspect per-channel runtime settings when `control.roles` allows
  the sender role to run that command.
- Worker subprocesses run in dedicated process groups. Timeout, task cancellation,
  reconnect, and incomplete post-worker delivery all terminate that group and move
  the persisted job to a terminal error or startup-retry state.
- The daemon supervises its media recorder when at least one allowed group opts in.
  The recorder joins muted and uses PyTgCalls' built-in `RecordStream` for the
  complete joined interval. That supported path captures MP3; after Marvin leaves,
  FFmpeg converts the closed capture to the final OGG/Opus artifact. The source MP3
  is removed only after successful conversion and is retained if conversion fails.
  The JSON sidecar stores the group, Telegram call id, joined interval, trigger,
  and participant state changes. It does not create a call or transcribe audio.

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
- `on_request`: an addressed `Marvin, запиши звонок`-style message or `/record`
  creates a recording request for an already active call.
- `disabled`: no media worker is allowed to join for this group. This is the default.
- `send_to_chat: true`: after the OGG container closes, upload it to the same
  group as two messages: a short `Запись звонка · <duration>` notice followed by
  a downloadable Telegram voice note with a waveform generated from the finalized
  OGG. The sidecar stores both Telegram message ids. Failed uploads are retried up
  to three times without repeating a notice that was already sent. The default is
  `false`. Delivery starts only after the MP3-to-OGG conversion succeeds; the
  recorder does not classify or reject a completed recording based on its loudness.

Transient Telegram join failures are retried without crashing the assistant
daemon. After a successful join, one recording runs until Marvin leaves or the
voice chat closes. The daemon does not split that interval into media fragments
or automatically rejoin the same call after Marvin has left it.

Only one call can be recorded by one Telegram account at a time. The first version
stores the incoming mixed stream; participant IDs and audio-source identifiers are
captured in participant snapshots, but the OGG does not contain separate
per-participant tracks.
The service never starts a group call itself. It posts the completed recording only
when `send_to_chat` is enabled; participant notice remains the operator's responsibility.

## Direct Calls

An incoming one-to-one call is handled by two independent per-user switches under
`allowed_users`. Both default to off, and the daemon answers only when at least
one is on:

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

`voice_agent` accepts optional `model`, `voice`, and `history` overrides. `history`
is how many messages of that direct chat are carried into the call prompt.

The voice agent runs on the daemon's own Telegram connection and its own PyTgCalls
instance. An account may have exactly one connection consuming the update stream:
a second one takes the incoming-call update and the first never sees it, so no part
of call handling may open another client.

The two media slots of one direct call are independent, and both honour the
requested audio parameters exactly. The inbound slot delivers 16 kHz mono PCM —
the format the speech model consumes — and the outbound slot accepts 24 kHz mono
PCM, the format it emits, so no resampling happens in either direction. Inbound
frames are batched to about 100 ms before being sent upstream; the outbound slot
is fed on a 10 ms tick and sends silence when nothing is buffered, so the stream
never stalls. When the caller interrupts, queued speech is dropped.

Recording a voice-agent call therefore writes two raw tracks against one shared
time origin, each padded with silence up to that origin, which FFmpeg joins into
one stereo Opus file with real speaker separation. Both call paths refuse to
deliver a recording that captured no audio.

The system prompt is the project's own
`capabilities/telegram/service/voice-agent.md`, then the recent tail of that
direct chat. `telegram service init` scaffolds that file from the shipped
template and never overwrites an existing one; the project owns it and edits it
freely, including which language the assistant prefers on a call. Both speech
directions are transcribed and the joined transcript is stored in the JSON
sidecar next to the recording; it is not sent as a message.

### Doing work from a call

The voice agent has one tool, `agent_task`: it hands a task to the project's
worker — the same worker a message runs, launched the same way — and returns
immediately, so the conversation is never blocked on it. Concurrent tasks are
capped and an identical task already running is not started twice. When the
result lands it is injected between turns, never into speech in progress, and
the assistant tells the caller. If the call ended first, the result is delivered
to the caller's direct chat as a message instead, so an answer the caller asked
for always reaches them. Hanging up is the caller's own button; there is no tool
for it, and no read-only inspection tools — the worker already sees the project.

A task runs under the authority the *same user's messages* resolve to: role and
`authority.allowed_capabilities` come from the one resolution the direct-message
path uses, given the caller id as sender. A call therefore reaches exactly what
a message from that person reaches, and no more.

How the channel behaves is `defaults.voice_agent`, beside the text worker's
policy in the same file — worker policy never lives in the connection entry,
which carries identity and secret references only:

```json
{
  "defaults": {
    "voice_agent": {
      "worker": "claude",
      "workers": {"claude": {"model": "<a fast model id>", "effort": null}},
      "max_parallel_jobs": 2,
      "prompt_file": null
    }
  }
}
```

Every field is optional and falls back to the project's existing worker settings
(`defaults.worker`, `defaults.workers`, `defaults.max_parallel_jobs`);
`prompt_file` overrides which file the call speaks from, relative to the service
directory unless absolute. Pick a **fast, non-reasoning model** here: work asked
for by voice is operational and quick — look something up, check or file a
ticket, note something down — not coding, and the caller is waiting on the line.
The timeout is the text worker's `defaults.worker_timeout`; task size is bounded
by the model choice, not by cutting the clock short.

An answered call needs a Gemini API key, in the environment variable the
selected connection names as `gemini_secret_env` (`GOOGLE_API_KEY` when the
entry omits it), resolved like every other credential. Without the key, or
without `voice-agent.md`, the daemon logs which one is missing and a caller who
also has `call_recording` on is recorded as usual instead.

## Voice Transcription

Voice notes addressed to the assistant are always transcribed in both direct messages
and groups. Groups may additionally enable ambient voice transcription:

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

- `auto`: All Telegram voice notes from participants are transcribed and echoed to
  the chat. In groups, the echo is sent as a Telegram reply to the original voice
  message (Telegram's reply preview shows sender attribution), and the echo content
  itself contains only the blockquoted transcript. In direct messages, the echo uses
  the "Твоё сообщение:" prefix. After transcription, the transcript is checked against
  the same configured assistant name/username/group-alias matching semantics used for
  text messages. If the spoken transcript names the assistant (e.g., "Marvin" or
  "Assistant"), the voice note is treated as an addressed assistant request and
  dispatches a worker. If the transcript does not name the assistant, it is echoed
  without creating a worker job. Voices already addressed via Telegram-level
  mention/reply dispatch as normal regardless of transcript content. In conversation
  history provided to workers, voice echoes are attributed to the original sender
  via the reply relationship, not to the assistant.
- `disabled`: Only voice notes addressed to the assistant are transcribed. This is
  the default.

Runtime overrides are available per channel via `/set voice-transcription auto|disabled`.

Transcription failures produce a fallback message. The daemon reserves ownership before
transcription so duplicate deliveries and restart catch-up cannot transcribe the same
voice note twice.

## Channel Context

The global soft prompt lives in `capabilities/telegram/service/context.md`.
Group policies may add a channel-specific overlay with either a markdown file or
a short inline string. File paths are relative to
`capabilities/telegram/service/`.

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

The prompt order is: global `context.md`, channel context overlay, daemon channel
state, current request, then the recent conversation tail. Channel context is a
soft behavior layer only; access control still belongs to `control.roles` and
tool access still belongs to `authority.roles`.

## Control Authority

Service control commands are handled by the daemon before a worker job exists,
so they are governed by `control.roles` instead of `authority.roles`.
`/status` is safe to expose broadly; `/set` changes per-channel runtime
settings; `/stop` stops queued/running work for the channel.

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

The service creates a per-job `CAPABILITIES_AUTH_CONTEXT` file for workers when
`settings.json` declares an `authority` policy. Capability CLIs read this file
before resolving credentials; an unlisted capability exits with policy refusal
(`exit 4`). This is a hard gate for normal capability use, while `context.md`
remains soft behavioral guidance.

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

For group members, keep personal or administrative capabilities such as
`mailbox`, `coolify`, or external write tools absent unless the project has a
deliberate reason to expose them. The bundled worker `telegram` wrapper also
honors `scope: current_chat` for chat-addressed Telegram commands.

To migrate a project that copied a service directory, delete the copied engine
files after installing the bundled capability. Keep or move only the project
policy/context files into `capabilities/telegram/service/` and keep the
connection/session state under `$XDG_STATE_HOME/telegram/<connection>/`.
