# directo — change log

## 2026-10-30 — `directo` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon directo` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `directo` declares what a person is asked for, and writes it through the contract

`directo manifest` now declares a connection's fields under `connections.fields` (`db`, `koht`, `username_env`, `password_env`), each with its label, help, type, whether it is required and whether it is a secret, and `directo connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `directo connection remove <id>` takes it out again with the secrets it wrote. Bare `directo help` ends with a section rendered from these declarations. Nothing else changes.
