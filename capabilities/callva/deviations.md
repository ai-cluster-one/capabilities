# callva deviations

This file's sole purpose is to hold `callva`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults and the [DOCTRINE](../../DOCTRINE.md), kept apart so an audit reads them as choices, not drift (DOCTRINE - *[Deviations are allowed - and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*).

## `CALLVA_HEADLESS` serves one credential from the environment

Doctrine rules 17 and 18 refuse operational use without a policy entry and refuse an environment-only identity without a registry. A process that runs `callva` for someone else's short-lived credential - a server starting it once per request with a fresh temporary home - holds that one credential and has no project, store or manager in which a policy or a registry could exist. `CALLVA_HEADLESS` serves that process: the one connection is `CALLVA_API_KEY` and `CALLVA_API_URL` from the process environment, and nothing else is read.

`read-only` carries the machine read's reasoning: where no project's policy could govern a call and the connection is held read-only, the policy has nothing to decide. `read-write` adds the verbs `callva routes` marks `writes`, and rests on a different reason: rule 18's `allow_write` is a project's statement about a connection the project holds, and here there is no project and the connection is one user's token, minted short-lived and bounded by the scopes that user granted, so the platform that receives the write is what decides it and refuses what the token does not allow. The caller that sets the mode answers for handing out only a token so bounded. The intent of both rules holds:

- No verb in either mode prints a secret, and `read-only` changes nothing on the platform: every verb `routes` marks `secret` exits 4, in `read-only` so does every verb marked `writes`, and in both so does any verb not declared there. No flag or env value lifts it, and `--connection`, `--key-env` and `--api-url` are refused.
- The read-only switch closes `read-write`'s writes with `read_only_switch`, as it closes every connection's.
- Neither mode touches the disk it runs on for a caller: a flag naming a local path exits 4 `headless_refused`, the `-` that reads stdin included, so a caller passes content inline and takes a recording on stdout.
- The mode widens nothing where a gate could speak. It exits 4 `headless_refused` wherever a project resolves, wherever the machine holds capability configuration (`capabilities/` or `callva/` in the config home, `~/.capabilities`) and wherever a store setting resolves, so a leaked variable closes a normal environment rather than opening it. The runtime authority context still runs first.
- The switch names what it allows, and any other value exits 4, so a value this version does not know is refused rather than read as permission.

The gate remains a guardrail, not a security boundary: anyone who can write a process's whole environment, an emptied home included, can already use the credential they put there.
