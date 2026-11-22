# instagram — change log

## 2026-11-22 — `instagram help` is the menu, and `instagram help <verb>` one verb's whole contract

`instagram help` printed the whole reference, about 12 KB with flags on 34 of its lines, and `instagram help <anything>`, an unknown word included, printed it again with exit 0. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 5 KB: what the capability is, the startup protocol (`instagram doctor` first, what exit 2, 6 and 4 mean and how to recover), the connections registry and session model, how a browser capture signs in, every verb on one line saying what it answers, the Direct folders the conversation reads scan, the write policy, the I/O and the exit codes, then the contract verbs and guides the library renders, with no flag on any line. `instagram help <verb>` takes the whole verb path and prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - with the profile argument, the folder and page bounds, the stored session's failures and the write gate on the pages they govern; `--max-id` on `media list`, which the old help left out, is on its page. The capture steps and the three `refresh` forms are on `instagram help refresh`, the old contract commands list is the library's, and instagram's own text for doctor and connections follows the library's page for each; a group (`instagram help messages`) lists its verbs. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `instagram help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `instagram` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Instagram, and Estonian (`et`), Instagram, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `instagram` declares its title, Instagram

The manifest carries `title`, `Instagram`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `instagram`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — A session made under a connection's former name is found again under its id

The stored session (`session.json`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so a device already signed in is found again after the store's move to connection ids, with no new sign-in. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

`doctor` names a connection by its label where it is not ready or its entry is incomplete, where it named the id. The `connection` key of its output keeps the id.

## 2026-10-30 — `instagram` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon instagram` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `instagram` declares what a person is asked for, and writes it through the contract

`instagram manifest` now declares a connection's fields under `connections.fields` (`expected_username`, `timeout_seconds`), each with its label, help, type, whether it is required and whether it is a secret, and `instagram connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `instagram connection remove <id>` takes it out again with the secrets it wrote. Bare `instagram help` ends with a section rendered from these declarations. Nothing else changes.
