# mailbox — change log

## 2026-10-13 — `mailbox` declares what a person is asked for, and writes it through the contract

`mailbox manifest` now declares a connection's fields under `connections.fields` (`address`, `imap_host`, `imap_port`, `smtp_host`, `smtp_port`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `mailbox connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `mailbox connection remove <id>` takes it out again with the secrets it wrote. Bare `mailbox help` ends with a section rendered from these declarations. Nothing else changes.
