# instagram — change log

## 2026-11-10 — `instagram` declares its title, Instagram

The manifest carries `title`, `Instagram`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `instagram`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — A session made under a connection's former name is found again under its id

The stored session (`session.json`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so a device already signed in is found again after the store's move to connection ids, with no new sign-in. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

`doctor` names a connection by its label where it is not ready or its entry is incomplete, where it named the id. The `connection` key of its output keeps the id.

## 2026-10-30 — `instagram` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon instagram` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `instagram` declares what a person is asked for, and writes it through the contract

`instagram manifest` now declares a connection's fields under `connections.fields` (`expected_username`, `timeout_seconds`), each with its label, help, type, whether it is required and whether it is a secret, and `instagram connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `instagram connection remove <id>` takes it out again with the secrets it wrote. Bare `instagram help` ends with a section rendered from these declarations. Nothing else changes.
