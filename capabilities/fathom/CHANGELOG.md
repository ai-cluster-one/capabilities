# fathom — change log

## 2026-11-22 — `fathom help` is the menu, and every verb answers `fathom help <verb>` with its whole contract

`fathom help` printed the whole 15 KB reference, and `fathom help <anything>`, an unknown word included, was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 5 KB: what the capability is, CONNECTIONS and DATABASE in brief, every verb on one line under COMMANDS saying what it answers, each write verb marked WRITE, the I/O contract and the exit codes, then the contract verbs and guides the library renders, with no flag on any line. `fathom help <verb>` takes the whole verb path (`help vocab add`) and prints that verb's whole contract from `HELP_VERBS`: its synopsis with every flag, what each flag does, the text the old help held for it, its output, exit codes and an example, followed by the vocabulary rule on the verbs that apply or change it, the write gate on a write verb, the missing-table answer, connection selection and the I/O and exit-code tables; the table columns and metadata keys are on `help schema` and `help read`. `help vocab` lists its three verbs. The old CONNECTIONS section - the keys, the registry's shape, the write gate, selection and machine reads - follows the library's page on `help connections`, after the connections home and the secret cascade; fathom's doctor text follows the library's page on `help doctor`; the old CONTRACT VERBS table is the library's. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `fathom help`, and `fathom --connection <id> help` prints the same menu as bare `help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help, and is out of HELP_MENU_PENDING, so the audit holds it to the layered help law. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `fathom` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Fathom, and Estonian (`et`), Fathom, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `fathom` declares its title, Fathom

The manifest carries `title`, `Fathom`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `fathom`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `fathom help` names the agent's vault in the credential cascade

Bare `fathom help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `fathom` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon fathom` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `fathom connections` and `fathom doctor` read the machine's own connections

`fathom` declares `connections` and `doctor` as machine reads. Run outside any project, `fathom connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `fathom doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `fathom` declares what a person is asked for, and writes it through the contract

`fathom manifest` now declares a connection's fields under `connections.fields` (`db_host`, `db_port`, `db_user`, `db_name`, `db_sslmode`, `fathom_key_env`, `db_password_env`), each with its label, help, type, whether it is required and whether it is a secret, and `fathom connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `fathom connection remove <id>` takes it out again with the secrets it wrote. Bare `fathom help` ends with a section rendered from these declarations. Nothing else changes.
