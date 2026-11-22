# railwayc — change log

## 2026-11-22 — `railwayc help` is the menu, and `railwayc help <verb>` one verb's whole contract

`railwayc help` printed the whole reference, about 18.7 KB with flags on 47 of its lines, and `railwayc help <word>` forwarded to `railway help <word>`, an unknown word included. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 4 KB: what railwayc is and its forwarding model, its own verb `backups`, the forwarded common reaches (status, logs, variables, redeploy, restart, up, down, domain, run) each saying what it answers and whether it is a write, the exit codes of railwayc's own layer, then the contract verbs and guides the library renders, with no flag on any line. `railwayc help <verb>` prints that verb's whole contract from `HELP_VERBS`: `backups` with its GraphQL reads, JSON shape, exit codes and examples; each forwarded common reach with its synopsis, its write gate, railway's exit code once forwarded and railwayc's before, and an example; and railwayc's own text for help, doctor (both probe paths, the `api` block, `expect_project` and exit 7), connections (the registry, the write-gated verbs, selection and RAILWAYC_EXPECT_PROJECT) and ids following the library's page for each. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `railwayc help`; railway's own help for a command is `railway <cmd> --help`, or `railwayc <cmd> --help`, which forwards with the token. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. Every word other than `help` forwards to `railway` exactly as before. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `railwayc` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Railway, and Estonian (`et`), Railway, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `railwayc` declares its title, Railway

The manifest carries `title`, `Railway`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `railwayc`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `railwayc help` names the agent's vault in the credential cascade

Bare `railwayc help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `railwayc` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon railwayc` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `railwayc connections` and `railwayc doctor` read the machine's own connections

`railwayc` declares `connections` and `doctor` as machine reads. Run outside any project, `railwayc connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `railwayc doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `railwayc` declares what a person is asked for, and writes it through the contract

`railwayc manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `expect_project`), each with its label, help, type, whether it is required and whether it is a secret, and `railwayc connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `railwayc connection remove <id>` takes it out again with the secrets it wrote. Bare `railwayc help` ends with a section rendered from these declarations. Nothing else changes.
