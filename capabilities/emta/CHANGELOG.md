# emta — change log

## 2026-11-22 — `emta help` is the menu, and `emta help <verb>` one verb's whole contract

`emta help` printed the whole reference, about 9 KB with flags on its command and contract tables, and `emta help <anything>`, an unknown word included, printed it again. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 6.5 KB: AUTH MODEL, WHICH REQUEST TO COPY, the STARTUP PROTOCOL, SELF-HEAL, SECRETS DISCIPLINE, IDENTIFIERS and CONNECTIONS, every verb on one line saying what it answers under COMMANDS, and the exit codes, then the contract verbs and guides the library renders, with no flag on any line. `emta help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, the EMTA call it makes, its output, its exit codes and an example - with the connection selection, the self-heal rule for the data verbs and the exit-code table on each; `emta help session` lists ingest, show and clear. The old CONTRACT COMMANDS table is the library's, emta's own text for doctor, connections, guide, ids, refs and inventory follows the library's page for each, and the connections registry and "Where connections live" are on `emta help connections`. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `emta help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `emta` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), EMTA, and Estonian (`et`), EMTA, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `emta` declares its title, EMTA

The manifest carries `title`, `EMTA`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `emta`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — A stored session made under a connection's former name is found again under its id

The stored session (`session.json`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so a session already ingested survives the store's move to connection ids. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

## 2026-10-30 — `emta` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon emta` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `emta` declares what a person is asked for, and writes it through the contract

`emta manifest` now declares a connection's fields under `connections.fields` (none - an entry carries no fields of its own), each with its label, help, type, whether it is required and whether it is a secret, and `emta connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `emta connection remove <id>` takes it out again with the secrets it wrote. Bare `emta help` ends with a section rendered from these declarations. Nothing else changes.
