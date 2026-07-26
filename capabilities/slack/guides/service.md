# Slack service

The optional Slack service receives Socket Mode events and applies three
separate decisions:

1. admission — which direct-message users and channels may reach the service;
2. routing — which admitted messages are answered automatically and which are
   relayed to the local inbox;
3. authority — which control commands and capability CLIs the sender's role may
   use.

Run `slack help` for lifecycle commands and credential scopes. This guide
describes the trust and configuration model.

## High-risk trusted-worker model

The real `claude` and `codex` workers intentionally match the Telegram
assistant service:

- Claude runs with `--dangerously-skip-permissions`.
- Codex runs with `--dangerously-bypass-approvals-and-sandbox`.
- The worker can read and change the full configured project.
- It inherits the service environment and can use the host shell, filesystem,
  network, provider credentials, and other available tools.
- The project `context.md`, generated request context, and model behavior are
  the primary boundary for host access.

This means an admitted sender whose message is automatically answered can cause
remote code execution with the daemon user's privileges. Prompt injection,
malicious repository content, and accidental destructive instructions can have
host-level impact. Only admit identities and channels for which that risk is
acceptable.

There is deliberately no separate `full_access` switch. Host access follows
from choosing a real worker. Capability access follows from the sender's
resolved authority role.

Slack bot and app tokens are removed from the worker's direct environment: the
daemon removes `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `SLACK_REAL_SLACK`.
The worker receives a current-conversation `slack` shim and can use:

```text
slack post current <text>
```

The daemon routes that progress message to the current conversation. Other
Slack operations through the shim fail with policy refusal (exit 4).
This is not credential isolation from a hostile worker: unrestricted host
access may expose project credential files, process state, or another installed
Slack executable. `context.md` instructs the worker not to seek or use those
paths.

## Fail-closed defaults

The shipped settings:

- admit no users or channels;
- automatically answer no conversations;
- select the `stub` worker;
- log no message snippets;
- grant no capabilities to direct users;
- grant the `supervisor` role all capabilities, but assign that role to nobody.

The wildcard role is inert until an admitted identity is explicitly assigned
that role.

## Configure admission and roles

Enable Socket Mode on the Slack app, subscribe to `message.im` and
`app_mention`, and create an app-level token with `connections:write`. Provide
the bot and app tokens through the project credential cascade, then run:

```text
slack service init
slack service doctor
```

Edit `capabilities/slack/service/settings.json`. Use exact Slack ids; names are
descriptive only:

```json
{
  "direct_messages": {
    "mode": "allowed_users",
    "default_role": "direct_user"
  },
  "allowed_users": {
    "U0123456789": {
      "name": "operator"
    }
  },
  "allowed_channels": {
    "C0123456789": {
      "name": "team",
      "default_role": "channel_member",
      "members": {
        "U0123456789": {
          "role": "channel_admin"
        }
      }
    }
  },
  "auto_answer": {
    "users": ["U0123456789"],
    "channels": ["C0123456789"]
  }
}
```

An admitted message is not necessarily sent to a worker. `auto_answer.users`
and `auto_answer.channels` are a second, narrower gate. Other admitted messages
are appended to the local inbox.

Role resolution follows this order:

- an explicit `allowed_users.<user>.role`;
- for direct messages, `direct_messages.default_role`;
- for channels, `allowed_channels.<channel>.members.<user>.role`;
- for channels, `allowed_channels.<channel>.default_role`;
- the built-in `direct_user` or `channel_member` fallback.

Assigning `role: "supervisor"` on `allowed_users.<user>` gives that role in
direct messages and every admitted channel. To grant a role only in selected
channels, leave the user row without a role and assign `role: "supervisor"`
under those channels' `members` entries.

The connection must set `allow_write: true` before `slack service start` or
`run`, because replies and reactions leave the system.

## Capability authority

The service writes a per-job `CAPABILITIES_AUTH_CONTEXT`. Capability CLIs read
it before resolving credentials; an unauthorized capability fails with policy
refusal (exit 4). This is the enforced gate on the normal capability CLI path.

Role policies live under
`authority.roles.<role>.allowed_capabilities`. A role can receive every
capability:

```json
{
  "authority": {
    "roles": {
      "supervisor": {
        "allowed_capabilities": {
          "*": true
        }
      },
      "channel_member": {
        "allowed_capabilities": {
          "slack": {
            "scope": "current_chat"
          },
          "routine": true
        }
      }
    }
  }
}
```

This wildcard is the role-based full-capability option. It does not alter
admission, automatic answering, or control-command permissions.

Authority overlays use the same precedence as Telegram:

```text
authority.default
  → authority.roles.<resolved-role>
  → allowed_users.<user>
  → allowed_channels.<channel>
  → allowed_channels.<channel>.members.<user>
```

At each level, an `allowed_capabilities` value replaces the previous list
wholesale. This allows a channel or member rule to narrow a powerful role, or
to grant broader authority only in one selected channel. For example:

```json
{
  "allowed_channels": {
    "C0123456789": {
      "allowed_capabilities": {
        "slack": { "scope": "current_chat" }
      },
      "members": {
        "U0123456789": {
          "allowed_capabilities": { "*": true }
        }
      }
    }
  }
}
```

The member has full capability access in that channel; other members receive
only the channel's scoped Slack authority.

The capability gate is not an isolation boundary against an intentionally
hostile unrestricted worker. Such a worker can attempt to remove the context
variable, invoke another executable path, use a direct SDK/API, or read
credentials available to the daemon user. Those paths remain governed by the
prompt and the daemon user's host permissions.

## Control authority and runtime settings

Control permissions are independent from capability permissions:

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
      "channel_member": {
        "commands": ["status", "help"]
      }
    }
  }
}
```

User, channel, and member rows may contain a `control` object to override the
role policy with the same specificity order. Slack accepts commands with or
without a leading slash and after an app mention:

```text
help
status
set worker codex
set tail 80
set model default
set claude.model claude-opus-4-6
set claude.effort high
set codex.model gpt-5.6
set codex.reasoning high
set codex.speed priority
stop
```

`set` values persist per Slack conversation under the private service state
directory. A direct message has one settings key; channel threads have separate
keys. Static defaults and worker profiles remain in `settings.json`:

```json
{
  "defaults": {
    "worker": "stub",
    "project": null,
    "tail_size": 40,
    "worker_timeout": 120,
    "max_parallel_jobs": 2,
    "workers": {
      "claude": {
        "model": null,
        "effort": null
      },
      "codex": {
        "model": null,
        "reasoning_effort": null,
        "service_tier": null
      }
    }
  }
}
```

Set `defaults.project` to an explicit directory when the daemon should work
outside the project that owns the service configuration.

## Delivery and recovery

Accepted events are reserved in a persistent register before routing. Relayed
events append to the local inbox. Answered events enter a FIFO queue per
conversation with a bounded global worker count. The daemon owns reactions,
final replies, and progress delivery.

On reconnection, one bounded catch-up pass considers channels known to the
watermark store and configured allowed channels. Watermarks advance after
terminal processing. Recovery is at-least-once around Slack posting: a crash
after Slack accepted a reply but before local terminal persistence can produce
a duplicate.

Service state is private user state under
`$XDG_STATE_HOME/slack/<connection>/service/`. Runtime settings, authority
envelopes, outboxes, registers, and logs are created with owner-only
permissions. Message snippets are absent from logs unless
`observability.log_message_snippets` is explicitly enabled.
