# Linking a workspace

A connection is one workspace session of one linked account. It carries the env vars that hold the session secrets — never the secret values — plus the non-secret workspace identity. This guide is how the person links their own workspace.

## What the session is

The pair the web client holds:

- the workspace token, an `xoxc-...` string, one per workspace;
- the `d` cookie value, shared across the person's workspaces.

The person reads both from a browser they are already signed in to. In the browser's developer tools: the token is `localStorage["localConfig_v2"].teams[<team-id>].token`, and the `d` cookie is under Application → Cookies for `slack.com`. Paste the `d` value exactly as the browser holds it; a `+` in it is part of the value.

Browser-driven extraction (driving a real browser with Playwright or rod, letting the person complete password, magic code, SSO and 2FA, then reading the pair out) is how `slackdump` and `wee-slack` collect it. That ceremony is a deferred step for the human trial; the pilot links by paste, which needs no extra dependency.

## Declaring the connection

The two secrets are named by the env var that holds them, resolved through the credential cascade (flags → project `.env(.local)` → `~/.config/slacker/credentials.env` → process env):

- `token_env` — the env var holding the `xoxc-` token (default `SLACKER_TOKEN`).
- `cookie_env` — the env var holding the `d` cookie value (default `SLACKER_COOKIE`).

The non-secret identity, kept in the connection registry:

- `team_id` — the workspace this session belongs to (for example `T01234567`); `doctor` asserts the session resolves to it.
- `workspace` — a human-recognisable label or domain, display only.
- `expect_user` — a user id or handle `doctor` asserts the session resolves to.

Write the connection and its secrets through the contract, so the secret values land in the credentials file and never in the registry:

```
slacker connection add mine --stdin
{"token_env": "SLACKER_TOKEN", "cookie_env": "SLACKER_COOKIE",
 "team_id": "T01234567", "workspace": "my-workspace",
 "SLACKER_TOKEN": "xoxc-…", "SLACKER_COOKIE": "…"}
```

Then prove it:

```
slacker doctor --connection mine
```

A healthy `doctor` resolves the linked user and the workspace and matches the bindings. `invalid_auth` (exit 2) means the session expired or was signed out — link again. The `d` cookie is short-lived relative to its nominal expiry, and there is no silent refresh.

## Several workspaces

A machine with several workspaces declares one connection each, with its own `token_env` / `cookie_env` and `team_id`. Selection is `--connection <id>` (`-c`), else the registry's default, else the sole entry.

## What never leaves the system

No secret value is ever written to the registry, a plan, a log, a commit, or command output. The capability has no write verb: it reads the person's own workspace and stores it in the person's own database, and sends nothing back to Slack.
