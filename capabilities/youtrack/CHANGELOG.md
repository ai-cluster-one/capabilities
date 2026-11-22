# youtrack — change log

## 2026-11-22 — `youtrack help` is the menu, and `youtrack help <verb>` one verb's whole contract

`youtrack help` printed the whole reference, about 22.8 KB with flags on 69 of its lines, and `youtrack help <anything>`, an unknown word included, was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 5 KB: what the capability is, where the connection model lives, every read and write verb on one line saying what it answers, the text-argument and paging model and the exit codes, then the contract verbs the library renders, with no flag on any line. `youtrack help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - for `users me`, `users find`, `projects find`, `projects get`, `projects fields list`, `projects fields get`, `groups find`, `groups members`, `searches list`, `issues get`, `issues search`, `issues create`, `issues update`, `issues comments list`, `issues comments add`, `issues links add`, `issues links remove`, `issues tags add`, `issues tags remove`, `issues work log`, `issues work list`, `articles list`, `articles search`, `articles get`, `articles create`, `articles update`, `articles comments list` and `articles comments add`, with the global `--connection` and `--token-env` options, the paging envelope and the exit codes on each verb that takes them, and the custom-field value rules on both `issues create` and `issues update`. The connection declaration and the machine-read rule moved to `help connections`, after the library's "Where connections live" and "Where a secret resolves", and doctor's text to `help doctor`; the old contract commands list is the library's, and the declared connection fields are the library's `help connection`. `youtrack help issues`, `help projects fields` and the other groups list their verbs. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `youtrack help`. The articles guide names `youtrack help articles <verb>` as the command contract. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `youtrack` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), YouTrack, and Estonian (`et`), YouTrack, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `youtrack` declares its title, YouTrack

The manifest carries `title`, `YouTrack`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `youtrack`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `youtrack` reads a connection's token from the agent's vault as well

Every verb resolves its token - a connection's `secret_env`, or `YOUTRACK_TOKEN` - through the contract's credential cascade, which asks the agent's vault last, after the project's .env files, ~/.config/youtrack/credentials.env and the process environment; it used to stop at the process environment, so a token `youtrack connection add` now seals into the vault by default would not have been found. A token a file or the environment holds resolves exactly as before. The help points to the contract-rendered `Where a secret resolves` section instead of listing the tiers by hand, and `missing_token` names the vault.

## 2026-10-30 — `youtrack` carries its own icon

The bundle holds its icon as `icon.png`, YouTrack's own mark; `capabilities icon youtrack` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `youtrack connections` and `youtrack doctor` read the machine's own connections

`youtrack` declares `connections` and `doctor` as machine reads. Run outside any project, `youtrack connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `youtrack doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `youtrack` declares what a person is asked for, and writes it through the contract

`youtrack manifest` now declares a connection's fields under `connections.fields` (`base_url`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `youtrack connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `youtrack connection remove <id>` takes it out again with the secrets it wrote. Bare `youtrack help` ends with a section rendered from these declarations. Nothing else changes.
