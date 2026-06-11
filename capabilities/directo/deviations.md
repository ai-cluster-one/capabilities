# directo — recorded deviations

Deliberate departures from the executable standard (SHEBANG.md), kept in this
dedicated file so an audit reads them as choices, not drift.

## Browser-session auth; the secret is not a flat token

Directo exposes no public API. Auth is a three-step browser ceremony
(`keeks.asp` → credential POST → location POST) minting a cookie pair bound
to a location (koht). The cascade's tiers and order are preserved for the
primaries (`DIRECTO_USERNAME`/`PASSWORD`/`DB`/`KOHT`); the minted session
resolves from the session file in the state home first, with the cascade as
the one-shot override path. Every authed call self-heals: an expired session
triggers a re-login from the primaries and one retry.

## Domain commands are click-based, exit 1 on failure

The domain verbs ride click: human-readable errors, exit 1 (auth ceremony
failures exit 2). The contract verbs (`stub`, `manifest`, `guide`, `ids`,
`refs`) and the gate ride the standard JSON envelope and exit-code taxonomy
in full.

## One session per login

Re-authenticating a login anywhere (another host, a browser sign-in) rotates
the server session and invalidates the prior cookie — two actors on one
login evict each other. One login per concurrent actor; per-project state
isolates projects only when their logins differ.
