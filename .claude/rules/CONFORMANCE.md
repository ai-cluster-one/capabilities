# Staying conformant

Conformance is proven by running the manager, not judged from memory. After any
change to a capability or the shared contract:

- `capabilities audit <name>` — the capability against the contract.
- `capabilities sync-contract --check` — the vendored shared regions against `contract/preamble.py`.
- `capabilities doctor` — registry, snapshots, gate, and gitignore guard in agreement.

An unmet clause is a violation to fix, not to note. Authoring runs through the
manager's emitted procedures — `capabilities new`, `conform <ref>`,
`groom <name>` — which keep the judgment with the agent and the steps in one
home. `capabilities help` is the full surface.

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
