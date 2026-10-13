# mailbox — change log

## 2026-10-13 — Outside any project, `mailbox connections` and `mailbox doctor` read the machine's own connections

`mailbox` declares `connections` and `doctor` as machine reads. Run outside any project, `mailbox connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `mailbox doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `mailbox` declares what a person is asked for, and writes it through the contract

`mailbox manifest` now declares a connection's fields under `connections.fields` (`address`, `imap_host`, `imap_port`, `smtp_host`, `smtp_port`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `mailbox connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `mailbox connection remove <id>` takes it out again with the secrets it wrote. Bare `mailbox help` ends with a section rendered from these declarations. Nothing else changes.
