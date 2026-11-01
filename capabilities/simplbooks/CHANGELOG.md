# simplbooks — change log

## 2026-11-01 — `simplbooks` never posts a payment or incoming with a negative or zero amount

`simplbooks bank-transactions save <id> --as payment --sum <amount>` drops a minus that matches the row's own outflow and posts the amount unsigned, and refuses with exit 6 a zero sum or a minus on an inflow row. `simplbooks payments create` and `simplbooks incomings create` refuse a `--sum` of zero or below with exit 6 before anything is sent. A positive `--sum`, an omitted one, `--as incoming` and `--as kanne` behave as before.

## 2026-11-01 — `simplbooks clients list --search` no longer shrinks the client cache

`simplbooks clients list --search <text>` merges the clients it finds into the local client cache instead of replacing the cache with them, so a later `invoices create`, `purchases create` or other booking still resolves a client that was not among the hits. An unfiltered `simplbooks clients list` still rebuilds the cache from what it fetched. Output is unchanged.

## 2026-11-01 — `simplbooks` reads back bindings, an invoice's reference and any purchase's view

`simplbooks payments show <id>` and `simplbooks incomings show <id>` read one payment or incoming from its view page: its client, its own label/value fields, and each bound purchase or invoice with the amount it covers (`bound`). `simplbooks invoices show` also returns the invoice's saved reference text as `additional_info`, read from the edit form, or from the copy form when the invoice is payment-locked, with `additional_info_source` naming which. `simplbooks purchases show --view` reads the rendered view page of any purchase, not only a payment-locked one, and the view output now carries the purchase's bound payments and incomings as `bindings`. Nothing else changes.

## 2026-10-30 — `simplbooks` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon simplbooks` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `simplbooks` declares what a person is asked for, and writes it through the contract

`simplbooks manifest` now declares a connection's fields under `connections.fields` (`email`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `simplbooks connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `simplbooks connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`SIMPLBOOKS_WORKER`) are declared under `connections.setup`, and `simplbooks setup` reports them while `simplbooks setup set` writes them to `credentials.env`, a secret only from stdin. Bare `simplbooks help` ends with a section rendered from these declarations. Nothing else changes.
