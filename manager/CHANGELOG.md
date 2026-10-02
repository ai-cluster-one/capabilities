# capabilities — change log

## 2026-10-02 — Check for updates without applying them

`capabilities update --check [<name>]` and `capabilities self-update --check` report the installed and the available payload or release, and whether an update is available, while installing, staging and switching nothing. `capabilities help` states their JSON and exit codes, and that every `search` row carries `installed`.

## 2026-10-02 — Print what changed since the installed version

`capabilities changelog` prints the change log of a capability, of the manager, or of the contract, by default only what changed since the installed version. Run `capabilities help` for its options.
