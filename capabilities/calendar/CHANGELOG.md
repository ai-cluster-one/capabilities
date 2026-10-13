# calendar — change log

## 2026-10-13 — `calendar` declares what a person is asked for, and writes it through the contract

`calendar manifest` now declares a connection's fields under `connections.fields` (`backend`, `address`, `username`, `base_url`, `principal_url`, `secret_env`, `timezone`, `meeting_url`, `calendars`), each with its label, help, type, whether it is required and whether it is a secret, and `calendar connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `calendar connection remove <id>` takes it out again with the secrets it wrote. Bare `calendar help` ends with a section rendered from these declarations. Nothing else changes.
