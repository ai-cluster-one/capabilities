# Slack service

The optional Slack service receives Socket Mode events and applies three
independent decisions: admission, answer-versus-relay routing, and per-role
capability authority. Run `slack help` for the lifecycle command surface and
credential scopes; this guide explains the operating model.

## Trust boundary

An admitted sender can supply instructions to the configured headless worker.
Treat admission as access to the worker's project view. The shipped settings
therefore fail closed:

- no admitted users or channels;
- no automatic answers;
- the `stub` worker;
- a read-only workspace;
- no authorized capabilities;
- no message text in operational logs.

Selecting `claude` or `codex` requires the explicit
`defaults.trusted_ingress: true` acknowledgement. Keep `workspace_mode` at
`read_only` unless admitted senders are also authorized to change project
files.

Workers receive a bounded process environment without either Slack token.
Provider API-key environment variables are also removed. A real worker must use
a dedicated `defaults.worker_home` containing only its harness authentication
and state; do not point it at the operator's general home directory.
Their `slack` command is a shim: only `slack post current <text>` is accepted,
and the daemon delivers it to the current conversation. Other Slack operations
fail with exit 4. Capability authority is an explicit per-role list; wildcard
grants are rejected.

Codex runs ephemerally with its sandbox enabled and a non-interactive
fail-closed approval policy. Claude runs with its normal permission system,
an empty MCP configuration, no project/user customizations, and a strict
fail-if-unavailable sandbox whose command and network allow-lists are derived
from the role. Claude Code 2.1.216 or newer is required for these controls.
Neither harness uses a dangerous permission-bypass flag.
Because Codex's fail-closed sandbox cannot safely expose the network used by
capability CLIs, service validation rejects capability grants when Codex is the
selected worker. Use Claude for explicitly authorized capability calls, or use
Codex with empty capability authority.

## Configure

1. Enable Socket Mode on the Slack app, subscribe to `message.im` and
   `app_mention`, and create an app-level token with `connections:write`.
2. Provide the bot token and app token through the project credential cascade.
3. Initialize the service, then edit its generated settings and context.
4. Admit exact Slack user and channel ids. Add ids to `auto_answer` only after
   they are admitted.
5. Grant explicit capability names under
   `authority.roles.<role>.allowed_capabilities`. This requires the Claude
   worker; Codex accepts no capability grants. Put every API hostname those
   capabilities require in the same role's `network_domains`; an empty list
   means no subprocess network, and wildcard domains are rejected.
6. For a real worker, choose `claude` or `codex`, acknowledge trusted ingress,
   configure an owner-only (`0700`) dedicated worker home, and keep the default
   read-only workspace unless writes are intentional.
7. Run the service doctor before starting it.

For example, this admits one user as a supervisor and permits automatic
answers only in one admitted channel. The ids below are placeholders; copy
actual ids from Slack:

```json
{
  "allowed_users": {
    "U0123456789": { "name": "operator", "role": "supervisor" }
  },
  "allowed_channels": {
    "C0123456789": { "name": "team", "default_role": "default" }
  },
  "auto_answer": {
    "users": [],
    "channels": ["C0123456789"]
  }
}
```

The connection must set `allow_write: true` before the daemon can start or run,
because replies and reactions leave the system. The direct CLI's
`capabilities/slack/policy.json` controls conversations that `slack read` and
`slack post` may access. Service settings control inbound Socket Mode
admission; these are different directions and neither widens the other.

## Runtime behavior

Accepted events are reserved in a persistent register before routing. Relayed
events append to the local inbox. Answered events enter a FIFO queue per
conversation with a bounded global worker count. The daemon owns reactions,
final replies, and progress outbox delivery.

On reconnection, one bounded catch-up pass considers channels already known to
the watermark store and configured allowed channels. A job that crashes is
terminal and receives an error marker. Watermarks advance only after terminal
processing, so a daemon interruption leaves the reserved event eligible for
catch-up. Recovery is at-least-once around the remote-post boundary: a crash
after Slack accepted a reply but before local terminal persistence can produce
a duplicate reply.

Service state is private user state under
`$XDG_STATE_HOME/slack/<connection>/service/`. Files and directories are
created with owner-only permissions. Message snippets are absent from logs
unless `observability.log_message_snippets` is explicitly enabled.
