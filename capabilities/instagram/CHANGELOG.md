# instagram — change log

## 2026-10-30 — `instagram` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon instagram` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `instagram` declares what a person is asked for, and writes it through the contract

`instagram manifest` now declares a connection's fields under `connections.fields` (`expected_username`, `timeout_seconds`), each with its label, help, type, whether it is required and whether it is a secret, and `instagram connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `instagram connection remove <id>` takes it out again with the secrets it wrote. Bare `instagram help` ends with a section rendered from these declarations. Nothing else changes.
