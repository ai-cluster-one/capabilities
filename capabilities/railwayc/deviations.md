# railwayc — deviations

This file's sole purpose is to hold `railwayc`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults, kept apart so an audit reads them as choices, not drift (DOCTRINE — *[Deviations are allowed — and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*). Each realizes the *intent* of the pattern in the terms of a tool that **wraps another CLI's authentication rather than calling an API** — railwayc puts a project-scoped token in front of the official `railway` CLI and forwards to it.

## Transport is a subprocess passthrough to `railway`, not an HTTP/JSON API

railwayc makes no network calls of its own. It resolves a token, sets it in the child environment, and `subprocess`-execs `railway` (the [askproject](../askproject/) precedent of wrapping a peer CLI). So the inline dependency set is empty (`dependencies = []`) — no `httpx`, no client. The *resilient-HTTP* intent (backoff, `Retry-After`, 4xx→2/3/6 mapping) is realized by the `railway` CLI itself for forwarded commands; railwayc's only network-shaped failure is `doctor`'s one `railway status` round-trip, which maps a rejected token to exit 2 and an unreachable/failed `railway` to exit 5, and a timeout to exit 5.

## Forwarding is transparent — railway owns the forwarded command's contract

For everything except `help` and `doctor`, railwayc inherits stdio and execs `railway` with the args verbatim: `railway`'s stdout, stderr, and **exit code pass through unchanged**. This is what lets streaming (`logs`), `--json`, prompts, and colors behave exactly as in `railway` itself, and it is why railwayc never maps or re-documents the surface (the surface is `railway help`). Consequently the `_emit`/`_die` JSON envelope and the exit-code taxonomy below apply **only to railwayc's own layer** — token resolution, `doctor`, and `railway`-not-on-PATH. A forwarded command returns a `railway` exit code, which is not drawn from railwayc's taxonomy. The intent — a stable, machine-checkable contract where auth is always distinguishable — holds for railwayc's own layer (exit 2 = token problem) and is delegated to `railway` for the rest.

## Credential cascade reduced to two tiers — project `.env(.local)` → process env

A Railway project token is scoped to one project + one environment, so it has no sensible global home. Two of the four canonical tiers are dropped, and the **order of those that remain is preserved** (project files win over process env):

- **No flag tier.** The token is a secret; per SHEBANG it simply omits its flag and resolves from the tiers below, so it never lands on `argv`.
- **No user-config tier.** A token in `~/.config/railwayc/` would apply to *every* project as a fallback and silently mis-scope them — the opposite of the per-project isolation that is the whole point. So the token's home is the **consuming project's `.env`**, and there is no global credentials file to populate — the manifest declares `project` scope, and install scaffolds the key into the project `.env`.

A one-shot override is therefore `RAILWAY_TOKEN=… railwayc …` (process env, tier 2), not a flag.

## railwayc requires the token and refuses ambient auth

The `railway` CLI, with no token set, falls back to this machine's interactive *account* login (full account scope). railwayc deliberately does **not** allow that: if no `RAILWAY_TOKEN` resolves it exits 2 rather than forwarding, so a command can never run on account-wide credentials by accident. The refusal *is* the scoping guarantee — access stays bound to the project the token was minted for.

## `doctor` is `railway status --json`, not `whoami`; plus exit code 7

A project token is not bound to a user, so `railway whoami` returns "Not Authorized" under one. `doctor` therefore proves health with the cheapest project-scoped round-trip — `railway status --json` — and reports the project, workspace, environments, and services. railwayc adds one domain exit code beyond the taxonomy: **7 = wrong project** (the resolved token points at a project other than the optional, non-secret `RAILWAYC_EXPECT_PROJECT`), named in `railwayc help`. The taxonomy railwayc's own layer uses: `0` success · `2` auth (token missing or rejected) · `5` environment (`railway` not on PATH, or unreachable) · `6` input · `7` wrong project.
