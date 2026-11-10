# directo — change log

## 2026-11-10 — `directo` declares its title, Directo

The manifest carries `title`, `Directo`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `directo`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — A live session made under a connection's former name is found again under its id

The live session (`session.env`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so a session already made survives the store's move to connection ids. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

`doctor` and the login remedy name a connection by its label where it resolves no database or names its login keys, where each named the id.

## 2026-10-31 — `directo help` names the agent's vault in the credential cascade

Bare `directo help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `directo` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon directo` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `directo` declares what a person is asked for, and writes it through the contract

`directo manifest` now declares a connection's fields under `connections.fields` (`db`, `koht`, `username_env`, `password_env`), each with its label, help, type, whether it is required and whether it is a secret, and `directo connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `directo connection remove <id>` takes it out again with the secrets it wrote. Bare `directo help` ends with a section rendered from these declarations. Nothing else changes.
