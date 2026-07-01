# ical — recorded deviations

Deliberate departures from the executable standard (SHEBANG.md), kept in this
dedicated file so an audit reads them as choices, not drift.

## Domain commands are human-first, exit 1 on failure

The domain verbs (`calendars`, `events`, `show`, `search`, `freebusy`,
`create`, `update`, `delete`) print human-readable output by default (`--json`
opts into JSON) and fail through ad-hoc messages with exit 1 rather than the
structured envelope. The CLI wraps interactive Calendar.app work where a
person is usually in the loop; the contract verbs (`stub`, `manifest`, `ids`,
`refs`, `guide`, `doctor`) ride the standard envelope and exit-code taxonomy
in full.

## Connections carry policy, not a secret

`CRED_KEYS` is empty — auth is the one-time macOS Automation grant, not a
value the cascade could resolve. The connections envelope still applies, but
each connection's payload is *policy* rather than a secret:

    {
      "default": "<id>",
      "connections": {
        "<id>": { "calendars": ["<Calendar.app name>", ...],
                  "allow_write": <bool> }
      }
    }

`calendars` scopes the connection to a subset of Calendar.app calendars
(empty ⇒ every calendar). `allow_write` is the exit-4 write-gate flag.

The standard `_write_gate` handles the `allow_write` half; a bespoke
`_enforce_write` layers the scope check on top so a write whose target
calendar is outside the connection's `calendars` list also exits 4
(`out_of_scope`). Read verbs consult the same scope: `--calendar` outside
scope exits 4 with the same code; without `--calendar`, reads intersect with
the scope silently. Both branches keep the write model's principle — the
active connection's policy governs the reach — while carrying no secret.

Because no key ever resolves for ical, the `keys` array in
`connections` output is always `[]`; the report exists to publish the
policy (`calendars`, `allow_write`) and mark the default with `*` in the
`default` field.

## Write verbs on top of a shared JXA prelude

`create`, `update`, `delete` reuse the same `JXA_PRELUDE` helpers
(`calendarsForNames`, `eventSummary`, `eventFull`, `localISO`, `env`)
as the read verbs, so both branches see the same view of Calendar.app.
Attendee invites and alarm delivery depend on the underlying account —
Calendar.app is the boundary the CLI does not cross.
