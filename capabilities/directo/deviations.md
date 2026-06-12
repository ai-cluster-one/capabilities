# directo — recorded deviations

Deliberate departures from the executable standard (SHEBANG.md), kept in this
dedicated file so an audit reads them as choices, not drift.

## Browser-session auth; the secret is not a flat token

Directo exposes no public API. Auth is a three-step browser ceremony
(`keeks.asp` → credential POST → location POST) minting a cookie pair bound
to a location (koht). The primaries resolve per connection: a registry entry
names `db`/`koht` literally and `username`/`password` by env-key indirection
(`username_env`/`password_env`, resolved through the cascade), while the
implicit default rides the bare `DIRECTO_*` cascade. The minted session
resolves from the per-connection session file in the state home first, with
the cascade as the one-shot override path. Every authed call self-heals: an
expired session triggers a re-login from the primaries and one retry.

## Domain commands are click-based, exit 1 on failure

`doctor` and the contract verbs (`connections`, `stub`, `manifest`, `guide`,
`ids`, `refs`) and the gate ride the standard JSON envelope and exit-code
taxonomy in full. The remaining domain verbs ride click: human-readable
errors, exit 1 (auth ceremony failures exit 2).

## One session per login

Re-authenticating a login anywhere (another host, a browser sign-in) rotates
the server session and invalidates the prior cookie — two actors on one
login evict each other. One login per concurrent actor. Sessions are keyed
per connection, so two connections never share a session; that isolation
holds only when the connections' logins differ.
