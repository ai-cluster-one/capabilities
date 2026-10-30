# geminitalk — change log

## 2026-10-30 — `geminitalk` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon geminitalk` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-22 — A project without project.json is a project

The envelope check accepts a project bound to its id. Declares `capabilities-contract>=0.12,<1`.

## 2026-10-13 — `geminitalk` declares what a person is asked for, and writes it through the contract

`geminitalk manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `model`, `voice`, `agent_name`, `max_agent_sessions`, `language`, `project_root`, `prompt_files`, `allow_capability_domain_commands`, `allow_codex_tasks`), each with its label, help, type, whether it is required and whether it is a secret, and `geminitalk connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `geminitalk connection remove <id>` takes it out again with the secrets it wrote. Bare `geminitalk help` ends with a section rendered from these declarations. Nothing else changes.
