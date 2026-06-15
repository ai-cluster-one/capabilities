# The manager vs. the capabilities

`bin/capabilities` is the **manager** — it installs, snapshots, gates, composes
context for, audits, scaffolds, and grooms the capability CLIs under
`capabilities/*/bin/*`. It operates *on* the capabilities; it is not one of them.

So the capability contract does not apply to the manager. It is **excluded from
the contract preamble and `capabilities sync-contract`** — never stamped with the
vendored `capability core` / `connections` fences, declares no archetype, carries
no `deviations.md`, and is not a target of the capability-standardization passes
(connections, declaration surface). The manager evolves on its own track, worked on
as its own concern.
