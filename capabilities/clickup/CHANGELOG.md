# clickup — change log

## 2026-10-13 — Outside any project, `clickup connections` and `clickup doctor` read the machine's own connections

`clickup` declares `connections` and `doctor` as machine reads. Run outside any project, `clickup connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `clickup doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `clickup` declares what a person is asked for, and writes it through the contract

`clickup manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `workspace_id`, `external_id_field`), each with its label, help, type, whether it is required and whether it is a secret, and `clickup connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `clickup connection remove <id>` takes it out again with the secrets it wrote. Bare `clickup help` ends with a section rendered from these declarations. Nothing else changes.

## 2026-10-09 — No SQLite import

`clickup` no longer imports `sqlite3`, which it never used. Nothing it does changes.

## 2026-10-09 — `connections` drops its `store` block

`clickup connections` no longer carries `store` (`mode`, `resolved_by`, `orphan_grants`): records are kept in files only, so there is no mode to report and no grant kept apart from its connection.
