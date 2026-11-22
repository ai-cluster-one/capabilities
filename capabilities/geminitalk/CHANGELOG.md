# geminitalk — change log

## 2026-11-22 — `geminitalk help` is the menu, and `geminitalk help <verb>` one verb's whole contract

`geminitalk help` printed the whole reference, about 15 KB with flags on 29 lines, and `geminitalk help <anything>`, an unknown word included, printed it again. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 4 KB: the agent startup protocol, what Gemini Talk is, every verb on one line saying what it answers under COMMANDS, a short CONNECTIONS orientation, the I/O contract, the exit codes and the examples, then the contract verbs and the guides the library renders, with no flag on any line. `geminitalk help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, what each flag does, the text the old help held for it, its output, its exit codes and an example - with the I/O contract and the exit-code table on each; `--connection` is described on every page that takes it. The optional env keys, the registry's form and the entry fields move to `geminitalk help connections`, after the library's page for it, which carries where connections live and where a secret resolves; geminitalk's own text for `doctor`, with `--audio`, follows the library's page. The old CONTRACT COMMANDS list is the library's. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `geminitalk help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `geminitalk` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Разговор с Gemini, and Estonian (`et`), Gemini vestlus, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `geminitalk` declares its title, Gemini Talk

The manifest carries `title`, `Gemini Talk`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `geminitalk`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `geminitalk help` names the agent's vault in the credential cascade

Bare `geminitalk help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `geminitalk` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon geminitalk` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-22 — A project without project.json is a project

The envelope check accepts a project bound to its id. Declares `capabilities-contract>=0.12,<1`.

## 2026-10-13 — `geminitalk` declares what a person is asked for, and writes it through the contract

`geminitalk manifest` now declares a connection's fields under `connections.fields` (`secret_env`, `model`, `voice`, `agent_name`, `max_agent_sessions`, `language`, `project_root`, `prompt_files`, `allow_capability_domain_commands`, `allow_codex_tasks`), each with its label, help, type, whether it is required and whether it is a secret, and `geminitalk connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `geminitalk connection remove <id>` takes it out again with the secrets it wrote. Bare `geminitalk help` ends with a section rendered from these declarations. Nothing else changes.
