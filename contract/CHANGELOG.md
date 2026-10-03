# contract — change log

## 2026-10-04 — Machine reads outside a project, and the policy state in `connections`

A capability may declare `MACHINE_READS`, a tuple of read verbs, beside `WRITE_DEFAULT`. Outside any project a declared verb uses the connections declared globally whose grant does not resolve to `enabled: false`, each one read-only: a write through it exits 4 `read_only`, and `connections` names each one under `machine_reads` with the scope `machine`. Every other verb outside a project, and every verb inside one, resolves its connections as before, and the policy gate, the machine ceiling and the read-only switch apply as before. `<name> connections` now also carries `policy`, the capability's effective policy state: `effective`, its `source` (`machine`, `project`, `global` or `default`) and the `machine` ceiling. A capability that declares no `MACHINE_READS` changes in nothing else. Every capability carries the change through `sync-contract`.

## 2026-10-04 — The store tier reads the machine's store setting

The store tier carries `read_store_setting()`, which returns this machine's store setting and its password from the manager's files, or None when the machine has none, and writes nothing, with `store_setting_url()` to turn it into a connection URL. Nothing calls it yet, so no capability's behaviour changes. Every capability carries it through `sync-contract`.

## 2026-10-03 — The machine ceiling and machine-service calls in the gate

The gate reads this machine's ceiling, `$XDG_CONFIG_HOME/capabilities/machine.json`, before it resolves a project or a policy row: a capability quarantined there refuses its operational verbs and `doctor` with exit 4 `quarantined` in every project and outside any, while `help`, `stub`, `manifest` and `connections` keep working. A capability with no entry, or a machine with no file, behaves as before. A service that declares `service.machine` passes a `<name> service <verb> ... --machine` call without resolving a project or a policy row; under the read-only switch such a service's `init`, `start`, `run`, `reload`, `join` and `leave` are refused, and its `join` requires explicit project enable like `init`, `start` and `run`. A capability without `service.machine` treats `--machine` as before. Every capability carries the change through `sync-contract`.
