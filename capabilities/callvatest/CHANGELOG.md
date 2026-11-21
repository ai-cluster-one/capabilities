# callvatest — change log

## 2026-11-21 — `callvatest help` is the menu, and `callvatest help <verb>` one verb's whole contract

`callvatest help` is the short complete menu of SHEBANG.md, "Agent-first help is the surface": what the harness does, the write gate, where CONNECTIONS, SCENARIOS, JUDGES and EXPLORING now live, every verb on one line under Calls, Sessions and Judges, and the exit codes, then the contract verbs the library renders, with no flag on any line. `callvatest help <verb>` takes the whole verb path (`help run`, `help session say`, `help fixtures`) and prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, what it does, its output, its exit codes and its example - with the GLOBAL OPTIONS, connection selection and the exit-code table on each verb that takes them; a group (`help session`) lists its verbs, and an unknown path, which argparse refused with exit 2, exits 6 `unknown_verb` with nothing on stdout and the hint to run `callvatest help`. SCENARIOS moved verbatim to `help run` and `help suite`, JUDGES to `help fixtures` and `help settings`, EXPLORING and the `explore` setting to `help explore` (the setting also on `help settings`), SESSIONS to each `help session <verb>`, CONNECTIONS to `help connections` after the library's connections home and secret cascade, and doctor's text to `help doctor`. The options block now names `--with-results`, which every verb already took, and `help suite` names `--path`, which SCENARIOS already described. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, JSON key, output or exit code changes.

## 2026-11-21 — `--via test-panel` opens the room the way the dashboard's Test button does

`run`, `suite`, `probe`, `explore` and `session start` take `--via connect|test-panel`. `connect`, the default, is the agent's public link as before. `test-panel` signs in as a dashboard user (`POST /auth/login`), finds the agent's tenant and project among that user's memberships, opens the room through `POST /agents/{id}/test-connect`, which marks it a preview, and revokes the session token straight after; the call then runs exactly as before, `--text` included. The user's sign-in is named on the connection by two new optional fields, `dashboard_email_env` and `dashboard_password_env`, because that surface takes a user's token and never a tenant API key. Reports and session meta record the path as `via`. Nothing changes for a run without the flag.

## 2026-11-16 — `callvatest` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Тест CallVA, and Estonian (`et`), CallVA test, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `callvatest` declares its title, CallVA Test

The manifest carries `title`, `CallVA Test`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `callvatest`. Nothing else changes.

## 2026-10-31 — `callvatest help` names the agent's vault in the credential cascade

Bare `callvatest help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-13 — Outside any project, `callvatest connections` and `callvatest doctor` read the machine's own connections

`callvatest` declares `connections` and `doctor` as machine reads. Run outside any project, `callvatest connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `callvatest doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `callvatest` declares what a person is asked for, and writes it through the contract

`callvatest manifest` now declares a connection's fields under `connections.fields` (`api_url`, `secret_env`, `test_call_field`), each with its label, help, type, whether it is required and whether it is a secret, and `callvatest connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `callvatest connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`ANTHROPIC_API_KEY`, `CEREBRAS_API_KEY`) are declared under `connections.setup`, and `callvatest setup` reports them while `callvatest setup set` writes them to `credentials.env`, a secret only from stdin. Its settings (project `service/settings.json`, global `service/settings.json`) are declared under `settings`, with the keys a panel shows as controls, and `callvatest settings` reads every document whole while `settings get|set|unset|put` write one key or one document, checked as callvatest reads it. Bare `callvatest help` ends with a section rendered from these declarations. Nothing else changes.
