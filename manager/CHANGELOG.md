# capabilities — change log

## 2026-10-03 — A machine ceiling above both policy scopes, and machine services declared

Each installed capability is `allowed` or `quarantined` on this machine, in `$XDG_CONFIG_HOME/capabilities/machine.json`, which only the manager writes. A capability with no entry is allowed, so every capability installed before this release stays allowed and nothing is migrated. Quarantined is effective-disabled in every project and outside any, whatever project or global policy says. `capabilities install <name>` of a capability new to the machine arrives quarantined, and `--allow` installs it allowed; reinstalling or updating keeps the state, and uninstalling drops it. `capabilities allow <name>` and `capabilities quarantine <name>` set it and answer `{capability, machine, changed}`, exit 3 when the capability is not installed and exit 4 under the read-only switch. Every `list` row gains `machine` and `machine_connections`, and a quarantined row reads effective `disabled` with source `machine`, so `inventory` and the generated context leave it out. Enabling a quarantined capability writes the entry and warns that it stays disabled. `dev install` installs the session payload allowed in the session's own isolated machine. The manifest's optional `service.machine` declaration is validated at install, audit, source check and verify, and `audit` also proves the ceiling refuses a capability enabled at both scopes. Run `capabilities help` for the details.

## 2026-10-02 — Check for updates without applying them

`capabilities update --check [<name>]` and `capabilities self-update --check` report the installed and the available payload or release, and whether an update is available, while installing, staging and switching nothing. `capabilities help` states their JSON and exit codes, and that every `search` row carries `installed`.

## 2026-10-02 — Print what changed since the installed version

`capabilities changelog` prints the change log of a capability, of the manager, or of the contract, by default only what changed since the installed version. Run `capabilities help` for its options.
