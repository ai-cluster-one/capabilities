# asana — change log

## 2026-11-21 — `asana help` is the menu, and every verb answers `asana help <verb>` with its whole contract

`asana help` printed the whole 28 KB reference, and `asana help <anything>` was refused by the parser as an unrecognised argument. `asana help` is now a menu under 8 KB: what the capability is and how its connections work, every verb on one line under Account, Projects, Tasks, Comments and followers, Tags, Machine state, Board sections, Dependencies, Attachments and Webhooks, each write verb marked WRITE, the I/O and exit codes, then the contract verbs and guides, with no flag on any line. `asana help <verb>` takes the whole verb path and prints that verb's whole contract from HELP_VERBS: its synopsis with every flag, the text the old help held for it, its output, exit codes and an example, followed by the references, the global options, the I/O contract and the exit codes; CONNECTIONS and the global identity option are on `asana help connections`, and an unknown path exits 6 unknown_verb with nothing on stdout and the menu as its hint. asana needs capabilities-contract 0.50 and leaves HELP_MENU_PENDING, so the audit holds it to the layered help law. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `asana` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Asana, and Estonian (`et`), Asana, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `asana` declares its title, Asana

The manifest carries `title`, `Asana`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `asana`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `asana` reads a connection's token from the agent's vault as well

Every verb resolves its token - a connection's `secret_env`, `ASANA_TOKEN`, or the key `--token-env` names - through the contract's credential cascade, which asks the agent's vault last, after the project's .env files, ~/.config/asana/credentials.env and the process environment; it used to stop at the process environment, so a token `asana connection add` now seals into the vault by default would not have been found. A token a file or the environment holds resolves exactly as before. The help points to the contract-rendered `Where a secret resolves` section instead of listing the tiers by hand, and `missing_token` names the vault.

## 2026-10-30 — `asana` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon asana` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `asana connections` and `asana doctor` read the machine's own connections

`asana` declares `connections` and `doctor` as machine reads. Run outside any project, `asana connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `asana doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `asana` declares what a person is asked for, and writes it through the contract

`asana manifest` now declares a connection's fields under `connections.fields` (`secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `asana connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `asana connection remove <id>` takes it out again with the secrets it wrote. Bare `asana help` ends with a section rendered from these declarations. Nothing else changes.
