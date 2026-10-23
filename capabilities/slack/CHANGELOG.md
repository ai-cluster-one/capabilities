# slack — change log

## 2026-10-23 — The service declares how many nodes run it at once

The service descriptor declares `service.placement` `exclusive`: one node runs the service at a time, the deployment runner's lease holds it there, and `capabilities place slack --node <node>` moves it, leaving `slack` usable on the node it left.

## 2026-10-16 — A moved agent keeps one allow-list in its slack settings

For an agent whose records moved into the store, `slack read` and `post` judge conversations by the `direct_messages`, `allowed_users`, `allowed_channels` and `default_channel_policy` of its slack settings - the allow-list the service admits by - edited with `slack settings set`, and a refusal names the settings verb; `service init` writes the template beside an allow-list set before the service was, and `service status` counts such settings as not yet initialized. The daemon is started without `SLACK_SERVICE_SETTINGS`, which is ignored with a notice. Every other project reads policy.json as before.

## 2026-10-13 — Outside any project, `slack connections` and `slack doctor` read the machine's own connections

`slack` declares `connections` and `doctor` as machine reads. Run outside any project, `slack connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `slack doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `slack` declares what a person is asked for, and writes it through the contract

`slack manifest` now declares a connection's fields under `connections.fields` (none - an entry carries no fields of its own), each with its label, help, type, whether it is required and whether it is a secret, and `slack connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `slack connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) are declared under `connections.setup`, and `slack setup` reports them while `slack setup set` writes them to `credentials.env`, a secret only from stdin. Its settings (project `service/settings.json`, global `service/settings.json`) are declared under `settings`, with the keys a panel shows as controls, and `slack settings` reads every document whole while `settings get|set|unset|put` write one key or one document, checked as slack reads it. Bare `slack help` ends with a section rendered from these declarations. Nothing else changes.

## 2026-10-09 — The service reads its settings straight from files

The service reads its settings from the project's `capabilities/slack/service/settings.json` over the config home's `slack/service/settings.json`, key by key, with the same values as before, and `service/store.py` is no longer shipped.
