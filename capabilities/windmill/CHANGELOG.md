# windmill — change log

## 2026-10-13 — `windmill` declares what a person is asked for, and writes it through the contract

`windmill manifest` now declares a connection's fields under `connections.fields` (`url`, `workspace`, `secret_env`, `folder`, `script_timeout_default`, `script_timeout_min`), each with its label, help, type, whether it is required and whether it is a secret, and `windmill connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `windmill connection remove <id>` takes it out again with the secrets it wrote. Bare `windmill help` ends with a section rendered from these declarations. Nothing else changes.
