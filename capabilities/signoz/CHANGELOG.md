# signoz — change log

## 2026-10-13 — `signoz` declares what a person is asked for, and writes it through the contract

`signoz manifest` now declares a connection's fields under `connections.fields` (`url`, `api_key_env`, `otlp_endpoint`, `ingestion_key_env`, `custom_headers_env`, `deployment`, `environment`, `scope_filter`, `dimensions`, `verify_tls`), each with its label, help, type, whether it is required and whether it is a secret, and `signoz connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `signoz connection remove <id>` takes it out again with the secrets it wrote. Bare `signoz help` ends with a section rendered from these declarations. Nothing else changes.
