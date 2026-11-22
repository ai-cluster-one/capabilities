# windmill — change log

## 2026-11-22 — `windmill help` is the menu, and `windmill help <verb>` one verb's whole contract

`windmill help` printed the whole reference, about 21.3 KB with flags on 81 of its lines, and `windmill help <anything>`, an unknown word included, was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 6.4 KB: what the capability is, the identity-free model, where the connection model and the write gate live, the options that stand before the verb, what a path is, every verb on one line saying what it answers under its group, the I/O contract, the exit codes and the notes, then the contract verbs the library renders, with no flag on any line. `windmill help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - for `scripts`, `script-get`, `script-delete`, `deploy`, `schedules`, `schedule-get`, `schedule-set`, `schedule-enable`, `schedule-delete`, `run`, `runs`, `job`, `vars`, `var-set`, `var-delete`, `folder-create`, `folder-ensure`, `triggers`, `trigger-get`, `trigger-set`, `trigger-delete`, `users`, `user-ensure`, `datatables`, `datatable run`, `datatable tables`, `datatable schema`, `datatable-db list`, `datatable-db create`, `datatable-db drop`, `verify` and `provision`, with the options before the verb (connection, URL, token, workspace and acting as another user) and the exit-code table on each verb that reaches the instance, and the write gate on each mutating one. The worker model, the timeout and the lock-after-deploy notes moved to `help deploy`, the worker model and the lock note to `help run` as well, and the provisioning inputs and manifest schema to `help verify` and `help provision`. CONNECTIONS moved to `help connections`, after the library's "Where connections live" and "Where a secret resolves", and doctor's text to `help doctor`; the old contract commands list is the library's, and the identifiers envelope's location follows the library's page on `help ids`. Two statements of the old text that the code does not bear out are corrected on their pages: `var-set` takes its value from `--value` or `--stdin` and never reads a file path, and `provision` and `verify` discover `.ts`, `.py`, `.sh` and `.go` scripts, not `.ts` alone. `windmill help datatable` and `help datatable-db` list their verbs. `windmill --connection <id> help` prints the same menu as bare `windmill help`. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `windmill help`. The operational, provisioning and script-authoring guides point at the verb pages that now hold what they named. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `windmill` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Windmill, and Estonian (`et`), Windmill, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `windmill` declares its title, Windmill

The manifest carries `title`, `Windmill`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `windmill`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — `inventory` names a connection by its label

At store schema major 3 a connection is keyed by an opaque id and called by its label. `inventory` names each connection item, and the `connection` attribute of each script and schedule it reads with `--network`, by that label, where it named the id. A connection kept in files is still named by its key. Nothing else changes.

`doctor` names a connection by its label where its token, URL or workspace is missing, where it named the id.

## 2026-11-01 — `windmill` carries Windmill's own logo as its icon

The bundle's `icon.png` is now Windmill's official icon, `windmill_icon.png` from the brand assets windmill.dev/brand links, unmodified: 1123 x 1123, full colour on a transparent ground, where it was a 32 x 32 image. The mark is Windmill's, used here only to identify the service the capability reaches. `capabilities icon windmill` answers its path and size. Nothing else changes.

## 2026-10-31 — `windmill help` names the agent's vault in the credential cascade

Bare `windmill help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `windmill` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon windmill` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `windmill connections` and `windmill doctor` read the machine's own connections

`windmill` declares `connections` and `doctor` as machine reads. Run outside any project, `windmill connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `windmill doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `windmill` declares what a person is asked for, and writes it through the contract

`windmill manifest` now declares a connection's fields under `connections.fields` (`url`, `workspace`, `secret_env`, `folder`, `script_timeout_default`, `script_timeout_min`), each with its label, help, type, whether it is required and whether it is a secret, and `windmill connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `windmill connection remove <id>` takes it out again with the secrets it wrote. Bare `windmill help` ends with a section rendered from these declarations. Nothing else changes.
