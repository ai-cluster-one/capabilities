# emta — change log

## 2026-11-07 — A stored session made under a connection's former name is found again under its id

The stored session (`session.json`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so a session already ingested survives the store's move to connection ids. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

## 2026-10-30 — `emta` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon emta` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `emta` declares what a person is asked for, and writes it through the contract

`emta manifest` now declares a connection's fields under `connections.fields` (none - an entry carries no fields of its own), each with its label, help, type, whether it is required and whether it is a secret, and `emta connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `emta connection remove <id>` takes it out again with the secrets it wrote. Bare `emta help` ends with a section rendered from these declarations. Nothing else changes.
