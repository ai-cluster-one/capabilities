# resend — change log

## 2026-11-22 — `resend help` is the menu, and `resend help <verb>` one verb's whole contract

`resend help` printed the whole reference, about 12.8 KB with flags on 33 of its lines, and `resend help <anything>` was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 3 KB: what the capability is, the one-way and identity-free model, where the connection model lives, every verb on one line saying what it answers, the write gate and the exit codes, then the contract verbs the library renders, with no flag on any line. `resend help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - with connection selection and the exit-code table on `help send` and `help domains`, and the write gate and allow_to policy on `help send`. CONNECTIONS moved verbatim to `help connections`, after the library's "Where connections live" and "Where a secret resolves", and doctor's text to `help doctor`; the old contract commands list is the library's, the identifiers envelope's location follows the library's page on `help ids`, and the declared connection fields are the library's `help connection`. `resend --connection <id> help` prints the same menu as bare `resend help`. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `resend help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `resend` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Resend, and Estonian (`et`), Resend, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `resend` declares its title, Resend

The manifest carries `title`, `Resend`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `resend`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `resend help` names the agent's vault in the credential cascade

Bare `resend help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `resend` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon resend` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `resend connections` and `resend doctor` read the machine's own connections

`resend` declares `connections` and `doctor` as machine reads. Run outside any project, `resend connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `resend doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `resend` declares what a person is asked for, and writes it through the contract

`resend manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `from`, `allow_to`), each with its label, help, type, whether it is required and whether it is a secret, and `resend connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `resend connection remove <id>` takes it out again with the secrets it wrote. Bare `resend help` ends with a section rendered from these declarations. Nothing else changes.
