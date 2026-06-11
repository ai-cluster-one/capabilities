# mail — recorded deviations

Deliberate departures from the executable standard (SHEBANG.md), kept in this
dedicated file so an audit reads them as choices, not drift.

## Domain commands are human-first, exit 1 on failure

The domain verbs (`read`, `show`, `links`, `draft`, …) print human-readable
output by default (`--json` opts into JSON) and fail through ad-hoc messages
with exit 1 rather than the structured envelope. The CLI wraps interactive
Mail.app inspection where a person is usually in the loop; the contract verbs
(`stub`, `manifest`, `ids`, `refs`, `guide`, `doctor`) ride the standard
envelope and exit-code taxonomy in full.

## No credentials

Auth is the one-time macOS Automation grant, not a value the cascade could
resolve: `CRED_KEYS` is empty, nothing scaffolds at install, and `doctor`
proves the grant by listing accounts through Mail.app itself.
