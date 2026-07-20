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
- The daemon performs one catch-up pass when a Telegram session connects, including
  supervisor-driven reconnects, to recover messages received while it was down. It
  does not poll chat history periodically while the live update stream is healthy.
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
  group as seekable Telegram audio, with the duration read from the finalized
  media, and persist the delivery status and Telegram message id in the JSON
  sidecar. Failed uploads are retried up to three times. The default is `false`.
  Delivery starts only after the MP3-to-OGG conversion succeeds; the recorder
  does not classify or reject a completed recording based on its loudness.

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
