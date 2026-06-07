# Simplbooks — identifiers

All fixed, **non-secret structural** values for this project's SimpleBooks setup: how the account is selected and the chart-of-accounts row ids the write paths use. Pure lookup; the operating model and command surface are in `simplbooks help`.

> Template note: fill these at install by discovering them against this project's own account once a session resolves — `simplbooks accounts list cashbook` (own bank account), `simplbooks accounts list chart` (income accounts, only if a regime needs an override), `simplbooks clients list` (clients). Record the durable ones this project addresses; leave the rest as breadcrumbs. **No secrets here** — the login email/password, the session cookie, and the account hash live in env / the user-config dir, never in this file.

## Account

The account is whatever the resolved session authorizes; the 32-char account hash (`SIMPLBOOKS_ACCOUNT`) is written by `simplbooks login` / `refresh`, not configured here. Record only how this project selects it.

| Role | Value |
|---|---|
| Entity whose books this project keeps | `<the entity the session belongs to>` |

## Account config the write paths use (env, set in the project .env)

These feed the config cascade (project `.env(.local)` → user `credentials.env`); the values live in env, only the structural roles are recorded here. **Read live from SimpleBooks, so not configured:** the VAT-type taxonomy + rates (`simplbooks invoices meta` / `expenses meta`), the default income account per vat_type, and the invoice issuer (`creator`, the logged-in user).

| Role | Env var | Required? | Find with |
|---|---|---|---|
| Own bank account on invoices | `SIMPLBOOKS_BANK_ACCOUNT` | for `invoices create` | `simplbooks accounts list cashbook` |
| Invoice issuer name | `SIMPLBOOKS_WORKER` | for `invoices create` | the name shown as the issuer |
| Bank integration for payment orders | `SIMPLBOOKS_BANK_TARGET` | for `expenses send-payment` | the bank the account stages payments to |
| Income override — EU reverse-charge | `SIMPLBOOKS_INCOMEACCT_EU_REVERSE` | optional (books policy) | `simplbooks accounts list chart` |
| Income override — non-EU export | `SIMPLBOOKS_INCOMEACCT_NON_EU` | optional (books policy) | `simplbooks accounts list chart` |
| Income override — EE domestic | `SIMPLBOOKS_INCOMEACCT_EE_DOMESTIC` | optional (books policy) | `simplbooks accounts list chart` |

The income account defaults to the value SimpleBooks returns live for the vat_type; set an override key only for a regime whose revenue books to a different chart row than that default. The sales `vat_type_id` regime anchors are built into the CLI (override per invoice with `--vat-type-id`).

## Stable ids (optional breadcrumbs)

Most ids (clients, invoices, purchase invoices, bank-transaction rows) are read live and need no record here. List one only if this project refers to it by hand repeatedly.

| Role | Id |
|---|---|
| `<e.g. a recurring client this project invoices>` | `<client id>` |

## Connection (values in env, never here)

The CLI resolves the login + session through the cascade `simplbooks help` documents; `simplbooks login` / `refresh` persist the session into the user-config dir. Only non-secret structural roles belong in this file.

| Env var | Holds | Where |
|---|---|---|
| `SIMPLBOOKS_EMAIL` / `SIMPLBOOKS_PASSWORD` | the login (auto-login + recovery) | project `.env` or `~/.config/simplbooks/credentials.env` |
| `SIMPLBOOKS_COOKIE` / `SIMPLBOOKS_ACCOUNT` | the live session + account hash | written by `login` / `refresh` into `~/.config/simplbooks/credentials.env` |
| `SIMPLBOOKS_WORKER` | invoice issuer name (write paths) | project `.env` or `~/.config/simplbooks/credentials.env` |

Global tool: `simplbooks` on `PATH` (`simplbooks help` is the source of truth for the CLI surface).
