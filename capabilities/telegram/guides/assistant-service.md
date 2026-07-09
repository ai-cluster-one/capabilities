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

3. Edit `.capabilities/telegram/service/settings.json`:

   - Set `connection` or rely on the default in `.capabilities/telegram/connections.json`.
   - Add `allowed_users` and `allowed_groups`.
   - Review `authority.roles`: this request-scoped hard gate limits which
     capability CLIs a worker may invoke for each sender role.
   - Set group `aliases` / `address_aliases` if the assistant should react to names other than the default.
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
- Group final replies and progress updates are sent as replies to the addressed
  message. Direct-chat replies are plain messages.
- `telegram send <chat> <text>` inside a worker writes to the daemon progress
  outbox instead of sending directly.
- Workers can be `codex`, `claude`, or `stub`; `/set` and `/status` in Telegram
  adjust per-channel runtime settings.

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
policy/context files into `.capabilities/telegram/service/` and keep the
connection/session state under `$XDG_STATE_HOME/telegram/<connection>/`.
