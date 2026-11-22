# vapic — change log

## 2026-11-22 — `vapic help` is the menu, and `vapic help <verb>` one verb's whole contract

`vapic help` printed the whole reference, about 15.9 KB with flags on 42 of its lines, and `vapic help <anything>` fell through to forwarding, where it needed a connections registry and was then sent to the official CLI as `vapi help <anything>`. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 4.1 KB: what the harness is, the forwarded `vapic <command> [args...]`, `bootstrap`, `connections` and `doctor` each on one line, the model, what a connection is, the write gate and the exit codes of vapic's own layer, then the contract verbs and guides the library renders, with no flag on any line. `vapic help <verb>` prints that verb's whole contract from `HELP_VERBS`: `vapic help forward` holds the forwarding contract - the `--connection` selector, MODEL, the narrow read-list fallback, how the official CLI's own help is reached, the write gate and which commands a read-only connection passes, the common reaches, the output and the exit codes; `vapic help bootstrap` holds `--yes`, its JSON answers and its exit codes; CONNECTIONS, the registry shape, the entry fields, selection and the machine reads follow the library's page on `vapic help connections`; doctor's probe, its report and its exit codes follow the library's page on `vapic help doctor`; the identifiers envelope's location follows the library's page on `vapic help ids`; and the note that `vapi --help` is the real CLI's surface follows the library's page on `vapic help help`. The old contract commands list is the library's own pages. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `vapic help`, so `vapic help <cmd>` no longer reaches the official CLI's help; `vapic <cmd> --help` still does. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `vapic` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Vapi, and Estonian (`et`), Vapi, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `vapic` declares its title, Vapi

The manifest carries `title`, `Vapi`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `vapic`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `vapic help` names the agent's vault in the credential cascade

Bare `vapic help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `vapic` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon vapic` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `vapic connections` and `vapic doctor` read the machine's own connections

`vapic` declares `connections` and `doctor` as machine reads. Run outside any project, `vapic connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `vapic doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `vapic` declares what a person is asked for, and writes it through the contract

`vapic manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `environment`, `api_base_url`, `dashboard_url`), each with its label, help, type, whether it is required and whether it is a secret, and `vapic connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `vapic connection remove <id>` takes it out again with the secrets it wrote. Bare `vapic help` ends with a section rendered from these declarations. Nothing else changes.
