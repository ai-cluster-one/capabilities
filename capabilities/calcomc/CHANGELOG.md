# calcomc — change log

## 2026-11-21 — `calcomc help` is the menu, and `calcomc help <verb>` one verb's whole contract

`calcomc help` is the short complete menu of SHEBANG.md, "Agent-first help is the surface": the model, every verb on one line under Bookings, Scheduling and Escape hatch, the write gate, the quirks and the exit codes, then the contract verbs the library renders, with no flag on any line. `calcomc help <verb>` takes the whole verb path (`help bookings list`, `help calendars busy-times`, `help api`) and prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, what it does, its output, its exit codes and its example - with connection selection, the output shape, the rate limit and the exit-code table on each; a group (`help bookings`) lists its verbs, and an unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `calcomc help`. CONNECTIONS moved verbatim to `help connections`, after the library's connections home and secret cascade, and doctor's text to `help doctor`; the write gate is on `help bookings cancel` and `help api` as well as the menu, `api`'s version-header sentence on `help api`, the `--json` compatibility sentence on the pages of the verbs that take it, and each example on its verb's page; the declared connection fields are the library's `help connection`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, JSON key, output or exit code changes.

## 2026-11-16 — `calcomc` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Cal.com, and Estonian (`et`), Cal.com, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `calcomc` declares its title, Cal.com

The manifest carries `title`, `Cal.com`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `calcomc`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `calcomc help` names the agent's vault in the credential cascade

Bare `calcomc help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `calcomc` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon calcomc` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `calcomc connections` and `calcomc doctor` read the machine's own connections

`calcomc` declares `connections` and `doctor` as machine reads. Run outside any project, `calcomc connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `calcomc doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `calcomc` declares what a person is asked for, and writes it through the contract

`calcomc manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `api_url`, `expect_email`), each with its label, help, type, whether it is required and whether it is a secret, and `calcomc connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `calcomc connection remove <id>` takes it out again with the secrets it wrote. Bare `calcomc help` ends with a section rendered from these declarations. Nothing else changes.
