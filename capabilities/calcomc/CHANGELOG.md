# calcomc — change log

## 2026-10-31 — `calcomc help` names the agent's vault in the credential cascade

Bare `calcomc help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `calcomc` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon calcomc` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `calcomc connections` and `calcomc doctor` read the machine's own connections

`calcomc` declares `connections` and `doctor` as machine reads. Run outside any project, `calcomc connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `calcomc doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `calcomc` declares what a person is asked for, and writes it through the contract

`calcomc manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `api_url`, `expect_email`), each with its label, help, type, whether it is required and whether it is a secret, and `calcomc connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `calcomc connection remove <id>` takes it out again with the secrets it wrote. Bare `calcomc help` ends with a section rendered from these declarations. Nothing else changes.
