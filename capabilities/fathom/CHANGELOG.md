# fathom — change log

## 2026-10-13 — `fathom` declares what a person is asked for, and writes it through the contract

`fathom manifest` now declares a connection's fields under `connections.fields` (`db_host`, `db_port`, `db_user`, `db_name`, `db_sslmode`, `fathom_key_env`, `db_password_env`), each with its label, help, type, whether it is required and whether it is a secret, and `fathom connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `fathom connection remove <id>` takes it out again with the secrets it wrote. Bare `fathom help` ends with a section rendered from these declarations. Nothing else changes.
