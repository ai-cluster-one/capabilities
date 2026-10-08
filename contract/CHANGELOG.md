# contract — change log

## 2026-10-08 — The store tier reads the family's store setting file

`read_store_setting()` reads `$XDG_CONFIG_HOME/agentkit/store.json` (`~/.config/agentkit/store.json` when `XDG_CONFIG_HOME` is unset), an `agentkit.store.v1` file carrying the password and an optional `db_schema`, and falls back to the manager's former pair, `$XDG_CONFIG_HOME/capabilities/store.json` and `credentials.env`, only while that file is absent. A file of an unknown `agentkit.store.*` version is refused as `store_setting_too_new`, and a field the format does not have as `bad_store_setting`. `check_store_setting()` admits `sslmode` `disable` for a local host - a Unix socket directory, `localhost` or a loopback address - and `store_setting_url()` carries a socket directory as a parameter. The tier adds `find_store_setting()`, which also says where the setting was found, `store_setting_path()`, `store_url_override()` (`AGENTKIT_STORE_URL`, then `CAPABILITIES_STORE_URL`), `store_host_is_local()`, `store_setting_version()` and `store_setting_document()`. A machine with only the former pair reads as before. Every capability carries the change through `sync-contract`.

## 2026-10-07 — The store tier reads a store setting that names its schema

`read_store_setting()` reads a `capabilities.store.v2` setting as well as a v1 one and returns `db_schema`: the schema a v2 setting names, else `agentkit`. A v2 setting naming a schema that is not a lowercase identifier, or that is `public`, `information_schema` or `pg_*`, is refused as `bad_schema_name`; `check_store_schema_name()` carries that rule. A v1 setting reads as before, with `db_schema` `agentkit`. Every capability carries the change through `sync-contract`.

## 2026-10-04 — Machine reads outside a project, and the policy state in `connections`

A capability may declare `MACHINE_READS`, a tuple of read verbs, beside `WRITE_DEFAULT`. Outside any project a declared verb uses the connections declared globally whose grant does not resolve to `enabled: false`, each one read-only: a write through it exits 4 `read_only`, and `connections` names each one under `machine_reads` with the scope `machine`. Every other verb outside a project, and every verb inside one, resolves its connections as before, and the policy gate, the machine ceiling and the read-only switch apply as before. `<name> connections` now also carries `policy`, the capability's effective policy state: `effective`, its `source` (`machine`, `project`, `global` or `default`) and the `machine` ceiling. A capability that declares no `MACHINE_READS` changes in nothing else. Every capability carries the change through `sync-contract`.

## 2026-10-04 — The store tier reads the machine's store setting

The store tier carries `read_store_setting()`, which returns this machine's store setting and its password from the manager's files, or None when the machine has none, and writes nothing, with `store_setting_url()` to turn it into a connection URL. Nothing calls it yet, so no capability's behaviour changes. Every capability carries it through `sync-contract`.

## 2026-10-03 — The machine ceiling and machine-service calls in the gate

The gate reads this machine's ceiling, `$XDG_CONFIG_HOME/capabilities/machine.json`, before it resolves a project or a policy row: a capability quarantined there refuses its operational verbs and `doctor` with exit 4 `quarantined` in every project and outside any, while `help`, `stub`, `manifest` and `connections` keep working. A capability with no entry, or a machine with no file, behaves as before. A service that declares `service.machine` passes a `<name> service <verb> ... --machine` call without resolving a project or a policy row; under the read-only switch such a service's `init`, `start`, `run`, `reload`, `join` and `leave` are refused, and its `join` requires explicit project enable like `init`, `start` and `run`. A capability without `service.machine` treats `--machine` as before. Every capability carries the change through `sync-contract`.
