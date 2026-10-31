# Linking a workspace

A connection is one workspace session of one linked account. It carries the env vars that hold the session secrets — never the secret values — plus the non-secret workspace identity. This guide is how the person links their own workspace.

## What the session is

The pair the web client holds:

- the workspace token, an `xoxc-...` string, one per workspace;
- the `d` cookie value, shared across the person's workspaces.

There are two ways to put that pair into a connection: sign in through a Chrome window the tool opens, or paste it by hand. Both land the two secrets only in the credentials file, never in the registry, a log, or output.

## Link by signing in through Chrome

`slacker link` reads the session out of a Chrome you sign in to, so you copy nothing:

```
slacker link [<id>] [--workspace <team-id|domain|name>] [--default]
```

It opens the installed Google Chrome with a fresh temporary profile and Chrome's remote-debugging channel, loads the Slack sign-in page, and waits while you sign in normally — password, magic code, SSO, 2FA. The tool types and clicks nothing; it only watches. Once you are in, it reads the workspace `xoxc-` token from the page's own `localStorage["localConfig_v2"]` and the `d` cookie over Chrome's DevTools protocol, writes them as the connection, closes the window, deletes the temporary profile, and proves the link with the same check `doctor` runs.

- `<id>` is the connection id to write; it defaults to the workspace's domain.
- With several workspaces signed in, name which to link with `--workspace` (its team id, domain or name), or pass an `<id>` that matches one; otherwise the tool lists them and asks.
- Your everyday Chrome profile is never touched — the window runs on a throwaway profile you sign in to just for this. No browser is bundled and no dependency is added: the DevTools channel is spoken over the websocket library `slacker` already ships.

It needs an installed Google Chrome and a display to sign in on. With no Chrome, a closed window, or a sign-in that times out, it refuses and leaves no partial connection — link by paste instead.

Browser-driven extraction like this is also how `slackdump` and `wee-slack` collect the session.

## Link by paste

Read both values from a browser you are already signed in to. In the browser's developer tools: the token is `localStorage["localConfig_v2"].teams[<team-id>].token`, and the `d` cookie is under Application → Cookies for `slack.com`. Paste the `d` value exactly as the browser holds it; a `+` in it is part of the value.

## Declaring the connection

The two secrets are named by the env var that holds them, resolved through the credential cascade (flags → project `.env(.local)` → `~/.config/slacker/credentials.env` → process env):

- `token_env` — the env var holding the `xoxc-` token (default `SLACKER_TOKEN`).
- `cookie_env` — the env var holding the `d` cookie value (default `SLACKER_COOKIE`).

The non-secret identity, kept in the connection registry:

- `team_id` — the workspace this session belongs to (for example `T01234567`); `doctor` asserts the session resolves to it.
- `workspace` — a human-recognisable label or domain, display only.
- `expect_user` — a user id or handle `doctor` asserts the session resolves to.

Write the connection and its secrets through the contract, so the secret values land in the agent's vault (or a credentials file, as `slacker help` says under `Where a secret resolves`) and never in the registry:

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
