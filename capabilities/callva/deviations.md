# callva deviations

This file's sole purpose is to hold `callva`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults and the [DOCTRINE](../../DOCTRINE.md), kept apart so an audit reads them as choices, not drift (DOCTRINE - *[Deviations are allowed - and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## `CALLVA_HEADLESS=read-only` reads with one credential from the environment

Doctrine rules 17 and 18 refuse operational use without a policy entry and refuse an environment-only identity without a registry. A process that runs `callva` for someone else's short-lived credential - a server starting it once per request with a fresh temporary home - holds that one credential and has no project, store or manager in which a policy or a registry could exist. `CALLVA_HEADLESS=read-only` serves that process: the one connection is `CALLVA_API_KEY` and `CALLVA_API_URL` from the process environment, and nothing else is read.

The reasoning is the machine read's: where no project's policy could govern a call and the connection is held read-only, the policy has nothing to decide. The intent of both rules holds:

- No verb can change the platform or print a secret: every verb `callva routes` marks `writes` or `secret` exits 4, and so does any verb not declared there. No flag or env value lifts it, and `--connection`, `--key-env` and `--api-url` are refused.
- The mode widens nothing where a gate could speak. It exits 4 `headless_refused` wherever a project resolves, wherever the machine holds capability configuration (`capabilities/` or `callva/` in the config home, `~/.capabilities`) and wherever a store setting resolves, so a leaked variable closes a normal environment rather than opening it. The runtime authority context still runs first.
- The switch names what it allows, and any other value exits 4, so a value this version does not know is refused rather than read as permission.

The gate remains a guardrail, not a security boundary: anyone who can write a process's whole environment, an emptied home included, can already use the credential they put there.
