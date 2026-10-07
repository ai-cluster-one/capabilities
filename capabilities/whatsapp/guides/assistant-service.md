# WhatsApp — assistant service

How the bundled `assistant` service holds one account's connection, captures every message live, answers the messages its settings admit with a dialogue turn, sends what is queued for the account, and how it is operated.

## What it does

The service is one long-running process per account. It opens the account's linked-device session through the same engine, capture path, account check and Postgres store every verb uses, and keeps it open. Each message the account receives or sends is written to the store as it arrives, with sync type `LIVE`; history-sync chunks the server pushes are stored as they are by any verb. It sends the messages `whatsapp send` queues for the account, and answers the messages its settings admit (see The dialogue). Settings that admit nobody leave it capturing and sending and answering nothing.

## Before it starts

- The project enables the capability explicitly: `capabilities enable whatsapp --project`. Global availability grants CLI use, not a project daemon.
- `whatsapp service init` seeds the service settings records (`capabilities/whatsapp/service/settings.json` in files mode) from the bundle's template, which names every key with its default and admits nobody, and the dialogue's prose, the `context` document (`capabilities/whatsapp/service/context.md`), where the project has none. The prose is the project's to edit and is never overwritten. The settings keys are listed under Settings; any other key is refused by `start`, `run`, `doctor` and `reload`, naming its path.
- The machine's store is configured (`capabilities store show`). Without it the service refuses to start and `service doctor` reports `store_not_configured`.
- The connection is an in-house one with a linked device.

## Operating it

- `whatsapp service start` launches the listener detached and returns once it has taken the account lock and published its owner record. It refuses, each with its own code, when the project has not enabled the capability (`project_enable_required`), the service is not initialized (`service_not_initialized`), the store is not configured (`store_not_configured`), or a listener already owns the account (`service_running`).
- `whatsapp service run` runs the same listener in the foreground; it is the deploy command.
- `whatsapp service status` reports state, pid, whether it is connected, uptime, the last event and message times, messages captured, messages sent and send failures, the send rate, the spool depth and the store error, reconnects and the last error, and the dialogue's counters (admitted, turns, answered, silent, failed, stopped, commands, running, queued).
- `whatsapp service logs` tails the listener's log, including one line per message the dialogue did not answer and why. `whatsapp service doctor` proves settings, store, link and ownership without opening a second connection, and, when the settings admit anyone, the project id the register needs and every profile the settings reach.
- `whatsapp service reload` validates the settings, then signals the listener alone to re-read them; the listener resolves and fits their profiles before taking them, and a rejected reload leaves the previous settings in force. A change of connection or environment takes effect only on restart.
- `whatsapp service stop` ends the listener cleanly; the account lock is released when it exits.

## Settings

- `connection`: the connection the service runs; null takes the registry's default. A change takes effect on restart.
- `environment`: a label the service reports, and part of the register's key. A change takes effect on restart.
- `assistant_name`: how the assistant is named in prompts, and the alias a group uses when it declares none.
- `direct_messages`: `mode` is `allowlist` (default: only numbers under `allowed_users`), `anyone`, or `off` (no direct chat is answered); `default_role` is the role of a direct sender without one (default `direct_user`).
- `allowed_users`: keyed by phone number with country code (digits, a leading `+` accepted): `name`, `role`, `profile`, `worker_timeout` (1 to 3600 seconds), `context` (text added to the prompt in this person's direct chat).
- `allowed_groups`: keyed by group JID (`<digits>@g.us`): `name`, `require_reference` (default true), `aliases` (case-insensitive patterns matched as whole words in the text; default the assistant name), `may_address` (`anyone`, the default, `allowed_users`, or a list of phone numbers), `member_role` (default `group_member`), `profile`, `worker_timeout`, `context`.
- `control.roles.<role>.commands`: which of `status`, `set`, `reload`, `stop`, `help` a role may send (a list, `"*"`, or per command `{allow|deny|enabled}`), over the defaults: `supervisor` all five, `channel_admin` status, set and help, `direct_user` and `group_member` status and help.
- `authority`: `default` and `roles.<role>`, each `allowed_capabilities` (`{"*": true}`, a list of capability names, or per capability `true` or `{verbs, scope, connections}`). The role's merged policy is written per turn to a user-only file named by `CAPABILITIES_AUTH_CONTEXT`. Without an `authority` key no context is passed and the project's own gate is the whole policy.
- `defaults`: `tail_size` (1 to 500, default 40), `debounce` (0 to 300 seconds, default 3), `max_age` (10 to 86400 seconds, default 600), `worker_timeout` (1 to 3600 seconds, default 120), `max_parallel_dialogue` (1 to 32 per chat, default 1), `profile` (default `whatsapp-claude`), `send_rate` (1 to 120 messages a minute, default 20). A top-level `send_rate` is accepted as the deprecated spelling of `defaults.send_rate`; setting both is refused, and `service doctor` names it.

## The dialogue

Every message the service captures live is offered to the dialogue once it is stored. A gate judges it in this order, and the first step that stops it wins:

1. Its own message: never answered. The one exception is the account's own self-chat, where a message typed on the phone (or another of the account's devices) is judged like any other, as sent by the account's own number. What this device sent, including every answer, never starts a turn.
2. History-sync origin: never answered; only live deliveries are.
3. Older than `max_age`: dropped, so a long offline queue is not answered in bulk.
4. The register: a message already processed, or older than the service's first sight of the chat, is dropped.
5. Chat allowed: a group under `allowed_groups`; a direct chat unless `direct_messages.mode` is `off`.
6. Sender allowed: by phone number. A sender that arrives as a LID is matched through the store's phone/LID identities, and an unknown LID is asked of the engine.
7. Addressed: a trailing `#noreply` line opts a message out everywhere. In a group the message must mention the account's JID or LID, quote one of the account's messages, or match an alias, unless the group sets `require_reference` to false.
8. Role: the sender's `allowed_users` role, else the group's `member_role` or `direct_messages.default_role`.
9. A control command at the head of the text (past a leading mention or alias) runs as one; anything else becomes a dialogue turn.

An admitted message is reserved in `whatsapp_register` before anything else is done with it, so the same message delivered live and again after a reconnect or restart is answered once. The register holds, per project id (from `capabilities/project.json`), environment, account and chat, the first message admitted there, the watermark, the ids processed within a day of it, the chat's `/set` overrides and its counters. A chat enters it on its first admitted message, so nothing before the service first saw a chat is answered. A message reserved by a run that then crashed is not answered after the restart.

A turn waits `debounce` seconds after its message, then runs in arrival order, at most `max_parallel_dialogue` at once in a chat; the chat shows typing while it runs. Its prompt is the `context` document, the chat's `context`, the channel state (chat, participant and role, effective settings, profile, authority), the current request, and the chat's last `tail_size` stored messages. It runs through callva-harness-runner from the chat's profile, in the project directory, on a fresh session, with a scrubbed environment and the service's variables (`WHATSAPP_AUTHORIZED_CHAT_ID`, `WHATSAPP_AUTHORIZED_REQUESTER`, `WHATSAPP_AUTHORIZED_ORIGIN_MESSAGE_ID`, `WHATSAPP_AUTHORIZED_CONNECTION`, `WHATSAPP_DAEMON_CHILD`, `CAPABILITIES_AUTH_CONTEXT`) set on top. At `worker_timeout` the run is cancelled, which ends everything it started, and the chat is told. The answer is what follows the last `=== REPLY ===` line, or the whole text without one; it is queued as pending rows in `whatsapp_messages`, quoting the request in a group and plain in a direct chat, split between paragraphs above 3500 characters, and the listener sends it like any queued message. A completed turn with an empty answer sends nothing; a failed one says it could not answer and names the kind of failure.

### Profiles

A profile says how the model runs; it is a callva-harness-runner profile file. A name resolves through the runner's own discovery, with `capabilities/whatsapp/service/profiles/` in the project first, then the bundle's `service/profiles/`, then the machine folder, then the runner's shipped profiles; the first file found is used whole. The bundle ships `whatsapp-claude` (claude, model `opus`) and `whatsapp-codex` (codex, model family `sol`). A chat's profile is its `/set profile`, else its group's or person's `profile`, else `defaults.profile`, else `whatsapp-claude`. Every profile the settings name is resolved and fit-checked when the service starts and reloads and in `service doctor`; a profile fits when it leaves `output_schema` unset and gives the full access a worker has (claude: `permission_mode = "bypassPermissions"`, Bash available, no sandbox; codex: `sandbox_mode = "danger-full-access"`, no permissions profile, `approval_policy = "never"`).

The listener resolves the runner itself: when its settings admit anyone it runs again under `uv run --with` the runner pinned in the bundle's `service/profiles.py`, which `start` and `doctor` run as a script to check profiles. The CLI's own dependencies do not include it, and settings that admit nobody never need it.

### Control commands

Sent as text in an admitted chat (in a group, addressed like any message), gated by the sender's role under `control.roles`. A refused command is answered with the role that lacks it.

- `/status`: the connection, the chat and the sender's role, the chat's profile and effective settings, turns running and queued, and the chat's counters.
- `/set <tail|debounce|worker-timeout|profile> <value|default>`: a per-chat override kept in the register; a profile must resolve and fit. `/set` alone lists them with their current values.
- `/reload`: re-reads the settings as `service reload` does, and answers whether they were taken.
- `/stop`: cancels the chat's running turn and drops what is queued.
- `/help`: the commands the sender's role may send.

## One account, one connection

The service holds the account lock every connected verb takes, for its whole life. While it runs, `send` on that account writes its message as a pending row and returns the service's answer once the service has sent it; any other verb that would connect on that account exits 8 `session_busy`, and the message names the service and the project it runs from. Reads the store answers keep working: `status`, `health`, `chats` without `--fresh`, and `messages` when the store covers the request or with `--rounds 0`.

## Disconnects and removal

When the connection drops, the service closes the session and opens a new one after a wait that starts at 2 seconds and doubles to at most 60, reset once a connection has held for a minute. The server's offline queue drains on each reconnect, so what arrived while the wire was down is captured then. `SIGUSR1` drops the socket on purpose and takes the same path, which is how a reconnect is exercised.

A device removed from the phone is final: the service stops reconnecting, stays up in state `logged_out`, and `status` and `doctor` say so. The remedy is to stop it, re-link with `whatsapp pair --recreate --yes`, and start it again.

If the engine does not release its connection when a session closes, the process exits rather than let a second client exist on the device.

## Sending

The service claims the pending rows of its account, the dialogue's answers among them, oldest first and sends each, at most `defaults.send_rate` a minute; the rest wait. Each row ends `sent` with WhatsApp's id or `failed` with the reason. A row a previous run claimed and never answered for is failed at start as `interrupted_delivery_unknown` and never sent again. The sending guide holds the whole model.

## When the store is unreachable

WhatsApp hands a message to the service once. A capture the store cannot take because it cannot be reached is therefore appended to `service/capture-spool.jsonl` and written into the store from there once it answers again, and at the next start. While the spool holds anything, new captures join the back of it, so the store receives everything once and in the order it arrived; the spool is empty again as soon as the store is. A spooled capture the store refuses for its content rather than for being down is moved to `service/capture-spool.rejected.jsonl` so it cannot hold back what follows. `service status` reports the spool depth and the store error. Sending pauses while the spool holds anything.

The spool holds an outage and nothing else. A store that is unreachable when the service starts keeps the service from connecting at all, which loses nothing: WhatsApp keeps the device's queue until it connects.

## Files

The control files are machine-local, under the account's state home: `service/owner.json` (who holds the account, with the launch nonce `start` waits for), `service/health.json`, `service/daemon.pid`, `service/daemon.log`, the capture spool while the store is unreachable, and `service/authority/`, which holds each running turn's authority file only for that turn. Captured messages go to the store, and to the spool only until the store answers.
