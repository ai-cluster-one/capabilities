# Note — standardize the capability-declaration surface (the core tier)

**Branch:** `main`. **Status:** noted, not started. Sibling to the connections
standard; same vendoring mechanism (`capabilities sync-contract`), different tier.

## The premise

A capability, at its floor, is its **protocol-2 declaration surface** plus its core
file handling — and nothing more. That floor is universal: every capability has it,
whether or not it also implements connections. So it deserves to be a *named
standard*, vendored canonically and drift-checked, exactly like the connection
machinery is under Task C.

Two consequences of stating it this way:

- **"No connections" is not a deviation and not an outlier.** A capability either
  implements connections or it does not; the absence is just an absence. It never
  goes in a `deviations.md`. (Reinforces the standard's "connection-less
  capabilities are first-class" line.)
- **The two tiers are independent.** `contract/preamble.py` already separates them:
  the `contract: capability core` fence (universal) and the `contract: connections`
  fence (opt-in). A core-only capability takes the core fence and stops.

## The declaration surface (the verbs every capability should answer)

These are the protocol-2 contract verbs — "what is this capability, and hand me its
core files for the context":

| verb | returns | helper | status |
|---|---|---|---|
| `stub` | one-line summary for context inclusion | `_contract` | ships |
| `manifest` | name/protocol/summary/creds/docs/state/post_install | `_contract` | ships |
| `refs` | the capability's reference docs (front-matter list) | `_cmd_refs` | ships |
| `guide` | a shipped guide topic (cached) | `_cmd_guide` | ships |
| `ids` | the capability's identifiers.json | `_cmd_ids` | ships |
| `connections` | the connections.json resolution report | `_cmd_connections` | ships (connection tier) |
| `settings` | the capability's settings.json (non-secret behavioral config) | — | **GAP — proposed** |

The **`settings` verb is a gap**: there is no contract verb today that returns a
capability's `settings.json`. The user wants the declaration surface to include
"returning its settings JSON" alongside connections JSON and the references list.
Propose adding a `settings` verb to `_contract` + a `_cmd_settings` helper that
reads `.capabilities/<NAME>/settings.json` (and/or the project gate file) and emits
it. Design it in this pass; do not bolt it on mid–Task C.

## The pass (do it the same way as connections)

1. **Audit all 13 capabilities' declaration surface for uniformity and
   completeness** — every capability answers every applicable verb identically
   (modulo the per-capability `_cmd_connections` body). The Task-C diff already
   confirmed `_cmd_guide`/`_cmd_ids`/`_cmd_refs`/`_contract` are near-uniform
   (fathom's `_cmd_guide` httpx/`_warn` is the lone real fork; normalize it).
2. **Vendor the core tier** — this rides Task C's `sync-contract` mechanism: the
   `contract: capability core` fence stamps into all 13, not just the 10
   connection-bearing ones. Mechanically it is the same codegen + drift-check.
3. **Fill the `settings` gap** — add the verb, the helper, vendor it in the core
   fence, audit it across all 13.
4. **Doctrine (Task D)** — name the two tiers in SHEBANG/DOCTRINE; state plainly
   that a connection-less capability is the core tier alone and is never a
   deviation; document the declaration surface as the universal floor.

## Scope call

The **mechanical** half (vendoring the core tier) is in-scope for Task C — the
`sync-contract` verb stamps both fences and the audit drift-checks both; the only
extra is including the 3 connection-less capabilities (mail, simplbooks, whatsapp)
in the core-fence migration rather than deferring them entirely.

The **new** half — the `settings` verb design, the completeness audit, and the
doctrine framing — is this dedicated pass, run after Task C's machinery lands.
