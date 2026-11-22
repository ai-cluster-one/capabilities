# notion — change log

## 2026-11-22 — `notion help` is the menu, and `notion help <verb>` one verb's whole contract

`notion help` printed the whole reference, about 14 KB with flags on 31 of its lines, and `notion help <anything>` was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 4 KB: what the capability is, the connection model, how one is selected and the read-only gate, every verb on one line saying what it answers, how pages, markdown and titles are given, the I/O and the exit codes, then the contract verbs and guides the library renders, with no flag on any line. `notion help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - with the connection selection and `--token-env`, the page references, the markdown input, the title handling, the write gate and Notion's own errors on the pages they govern; the search quirk is on `notion help children` and the direct-children match on `notion help upsert`. The registry shape, its field semantics and `--token-env` are on `notion help connections`, after "Where connections live" and "Where a secret resolves"; the old contract commands list is the library's, and notion's own text for doctor, connections and ids follows the library's page for each. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `notion help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `notion` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Notion, and Estonian (`et`), Notion, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `notion` declares its title, Notion

The manifest carries `title`, `Notion`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `notion`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `notion` reads a connection's token from the agent's vault as well

Every verb resolves its token - a connection's `secret_env`, `NOTION_TOKEN`, or the key `--token-env` names - through the contract's credential cascade, which asks the agent's vault last, after the project's .env files, ~/.config/notion/credentials.env and the process environment; it used to stop at the process environment, so a token `notion connection add` now seals into the vault by default would not have been found. A token a file or the environment holds resolves exactly as before. The help points to the contract-rendered `Where a secret resolves` section instead of listing the tiers by hand, and `missing_token` names the vault.

## 2026-10-30 — `notion` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon notion` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `notion connections` and `notion doctor` read the machine's own connections

`notion` declares `connections` and `doctor` as machine reads. Run outside any project, `notion connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `notion doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `notion` declares what a person is asked for, and writes it through the contract

`notion manifest` now declares a connection's fields under `connections.fields` (`secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `notion connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `notion connection remove <id>` takes it out again with the secrets it wrote. Bare `notion help` ends with a section rendered from these declarations. Nothing else changes.
