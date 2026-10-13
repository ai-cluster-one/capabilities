# calcomc — change log

## 2026-10-13 — `calcomc` declares what a person is asked for, and writes it through the contract

`calcomc manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `api_url`, `expect_email`), each with its label, help, type, whether it is required and whether it is a secret, and `calcomc connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `calcomc connection remove <id>` takes it out again with the secrets it wrote. Bare `calcomc help` ends with a section rendered from these declarations. Nothing else changes.
