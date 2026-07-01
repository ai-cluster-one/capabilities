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

## A capability edit is a closed loop

Editing `capabilities/<name>/bin/<name>` here changes only this checkout. The
machine runs the copy in the registry (`~/.capabilities/<name>/<name>`), so an
edit is not done until that canonical copy carries the new bytes. Every change
to a capability closes the loop, in order:

1. **Conform** — the checks above pass.
2. **Publish** — commit and push to the canonical source (sanitize first).
3. **Reinstall** — `capabilities install <name>` so the registry holds a fresh
   real copy and the PATH symlink resolves to it.

The machine references the canonical binary, never a direct link into a working
tree — a live-edit `ln -s` from the registry back to this checkout is the leak
this loop closes. `capabilities doctor` enforces the end state: a registry entry
that is a symlink instead of a real file is a finding, not a convenience.
