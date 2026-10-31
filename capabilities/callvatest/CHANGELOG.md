# callvatest — change log

## 2026-10-31 — `callvatest help` names the agent's vault in the credential cascade

Bare `callvatest help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-13 — Outside any project, `callvatest connections` and `callvatest doctor` read the machine's own connections

`callvatest` declares `connections` and `doctor` as machine reads. Run outside any project, `callvatest connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `callvatest doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `callvatest` declares what a person is asked for, and writes it through the contract

`callvatest manifest` now declares a connection's fields under `connections.fields` (`api_url`, `secret_env`, `test_call_field`), each with its label, help, type, whether it is required and whether it is a secret, and `callvatest connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `callvatest connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`ANTHROPIC_API_KEY`, `CEREBRAS_API_KEY`) are declared under `connections.setup`, and `callvatest setup` reports them while `callvatest setup set` writes them to `credentials.env`, a secret only from stdin. Its settings (project `service/settings.json`, global `service/settings.json`) are declared under `settings`, with the keys a panel shows as controls, and `callvatest settings` reads every document whole while `settings get|set|unset|put` write one key or one document, checked as callvatest reads it. Bare `callvatest help` ends with a section rendered from these declarations. Nothing else changes.
