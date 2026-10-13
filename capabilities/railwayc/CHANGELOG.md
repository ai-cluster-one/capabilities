# railwayc — change log

## 2026-10-13 — `railwayc` declares what a person is asked for, and writes it through the contract

`railwayc manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `expect_project`), each with its label, help, type, whether it is required and whether it is a secret, and `railwayc connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `railwayc connection remove <id>` takes it out again with the secrets it wrote. Bare `railwayc help` ends with a section rendered from these declarations. Nothing else changes.
