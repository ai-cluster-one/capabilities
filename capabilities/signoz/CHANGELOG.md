# signoz — change log

## 2026-10-30 — `signoz` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon signoz` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `signoz connections` and `signoz doctor` read the machine's own connections

`signoz` declares `connections` and `doctor` as machine reads. Run outside any project, `signoz connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `signoz doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `signoz` declares what a person is asked for, and writes it through the contract

`signoz manifest` now declares a connection's fields under `connections.fields` (`url`, `api_key_env`, `otlp_endpoint`, `ingestion_key_env`, `custom_headers_env`, `deployment`, `environment`, `scope_filter`, `dimensions`, `verify_tls`), each with its label, help, type, whether it is required and whether it is a secret, and `signoz connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `signoz connection remove <id>` takes it out again with the secrets it wrote. Bare `signoz help` ends with a section rendered from these declarations. Nothing else changes.
