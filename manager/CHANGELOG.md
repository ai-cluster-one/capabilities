# capabilities — change log

## 2026-10-05 — A core capability arrives allowed and enabled globally

`capabilities install <name>` of a core capability (`tier` "core" in `list`) new to this machine, from the official catalogue, now leaves it `allowed` on the machine and writes an explicit global enable record, at the global scope `capabilities enable <name> --global` writes from the same directory; the JSON carries `global_policy {enabled, gate}`. A global entry the user already holds for it is kept and nothing is written. A standard capability, and a core name installed with `--from` or `--source`, still arrives quarantined with no policy written. Reinstalling or updating an installed capability changes neither its machine state nor its policy, and a bundled service still needs an explicit project enable. Run `capabilities help` for the details.

## 2026-10-04 — A capability's machine reads are validated wherever it is

`capabilities audit`, `source check` and `install` validate a capability's `MACHINE_READS` declaration: a tuple of strings, each a verb the capability's `help` names and none of them in its `WRITE_VERBS`. Audit and source check report a bad declaration as a `connections/machine-reads` failure, and install refuses it with exit 6 `machine_reads_invalid` before anything is installed. A capability that declares none is validated as before.

## 2026-10-04 — The machine's store setting, the one store pointer

`capabilities store set` records the Postgres this machine's store lives in: host, port, database, user, an `sslmode` of `require`, `verify-ca` or `verify-full` (anything weaker is refused) and an optional root certificate in `$XDG_CONFIG_HOME/capabilities/store.json`, and the password, taken from stdin, a file or a named environment variable and never from argv, in `$XDG_CONFIG_HOME/capabilities/credentials.env` at mode 0600. It is refused under the read-only switch. `capabilities store show` reports the setting and where each value came from without the password, and `capabilities store doctor` proves a TLS connection works, with the server version and the negotiated TLS, and fails when the server accepts plain text. The manager's own records in database mode now resolve their store from the setting; `CAPABILITIES_STORE_URL` still overrides it, and a machine with neither behaves as before. The manager now carries the Postgres driver. Run `capabilities help` for the details.

## 2026-10-03 — A machine ceiling above both policy scopes, and machine services declared

Each installed capability is `allowed` or `quarantined` on this machine, in `$XDG_CONFIG_HOME/capabilities/machine.json`, which only the manager writes. A capability with no entry is allowed, so every capability installed before this release stays allowed and nothing is migrated. Quarantined is effective-disabled in every project and outside any, whatever project or global policy says. `capabilities install <name>` of a capability new to the machine arrives quarantined, and `--allow` installs it allowed; reinstalling or updating keeps the state, and uninstalling drops it. `capabilities allow <name>` and `capabilities quarantine <name>` set it and answer `{capability, machine, changed}`, exit 3 when the capability is not installed and exit 4 under the read-only switch. Every `list` row gains `machine` and `machine_connections`, and a quarantined row reads effective `disabled` with source `machine`, so `inventory` and the generated context leave it out. Enabling a quarantined capability writes the entry and warns that it stays disabled. `dev install` installs the session payload allowed in the session's own isolated machine. The manifest's optional `service.machine` declaration is validated at install, audit, source check and verify, and `audit` also proves the ceiling refuses a capability enabled at both scopes. Run `capabilities help` for the details.

## 2026-10-02 — Check for updates without applying them

`capabilities update --check [<name>]` and `capabilities self-update --check` report the installed and the available payload or release, and whether an update is available, while installing, staging and switching nothing. `capabilities help` states their JSON and exit codes, and that every `search` row carries `installed`.

## 2026-10-02 — Print what changed since the installed version

`capabilities changelog` prints the change log of a capability, of the manager, or of the contract, by default only what changed since the installed version. Run `capabilities help` for its options.
