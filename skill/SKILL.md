---
name: capabilities
description: The capabilities manager — install, gate and operate capability CLIs on this machine. Use when a capability is named, when one should be installed or enabled, or to find out which capabilities exist.
---

# Capabilities

The `capabilities` manager installs and operates self-contained capability CLIs on this machine. Each capability is one credentialed executable on PATH, gated per project, and self-describing.

Three commands reach everything:

- `capabilities help` — the manager's full contract: every verb, the policy gate, the credential model, the exit taxonomy.
- `capabilities list` — what is installed here and how each one is gated in this project; `capabilities search` finds what is not installed yet.
- `<name> help` — one capability's own contract, once you have its name.

Nothing further is written down here by design. The manager and each capability answer for themselves at the moment you ask, so read them rather than working from this file.
