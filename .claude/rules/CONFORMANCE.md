# Staying conformant

Conformance is proven by running the manager, not judged from memory. After any
change to a capability or the shared contract:

- `capabilities audit <name>` — the capability against the contract.
- `capabilities sync-contract --check` — the vendored shared regions against `contract/preamble.py`.
- `capabilities doctor` — registry, snapshots, gate, and gitignore guard in agreement.

An unmet clause is a violation to fix, not to note. Framework-level authoring
and review workflows live in the manager's on-demand guide surface; start with
`capabilities guide` and load the relevant topic. `capabilities help` is the
full executable surface.

Those checks prove a capability obeys the manager. `capabilities selfcheck`
proves the inward half — that the manager obeys the doctrine and the repo is
whole: the enforcer parses, every capability audits clean, both audit branches
(core-only and connection-bearing) have a living green example and fire on a
deliberate break, every standing rule names an enforcement the manager actually
performs, and every doc link resolves. Run it after touching `DOCTRINE.md`,
`contract/preamble.py`, the audit, or any `bin/<name>`. The binding it enforces:
a rule the manager asserts but does not check is itself a violation — fix the
enforcer, never soften the rule to match a silent gap.

This repo is public and capability-agnostic. Before committing or pushing edits
to the shipped doctrine surface, run the `sanitize-project` skill so no consumer,
person, company, or real value leaks into it.

## A source edit ends as one release

The immutable Git commit is the release unit. The integration checkout stays
clean; authoring happens in dedicated worktrees. A prepared commit is complete
only when the manager's source release transaction has verified and published
it, synchronized the integration checkout, reconciled locally installed changed
payloads, and removed its completed dev session. `capabilities guide publishing`
and `capabilities guide dev` are the executable procedure surfaces.

The machine references canonical registry payloads, never links into authoring
worktrees. `capabilities doctor` enforces that installed boundary; source release
enforces the commit, publication, checkout, and local-registry boundary.
