# railwayc — change log

## 2026-10-30 — `railwayc` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon railwayc` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `railwayc connections` and `railwayc doctor` read the machine's own connections

`railwayc` declares `connections` and `doctor` as machine reads. Run outside any project, `railwayc connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `railwayc doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `railwayc` declares what a person is asked for, and writes it through the contract

`railwayc manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `expect_project`), each with its label, help, type, whether it is required and whether it is a secret, and `railwayc connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `railwayc connection remove <id>` takes it out again with the secrets it wrote. Bare `railwayc help` ends with a section rendered from these declarations. Nothing else changes.
