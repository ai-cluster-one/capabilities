# Simplbooks

The project's **SimpleBooks bookkeeping access** — reading and writing its account through the `simplbooks` CLI. SimpleBooks has no public API, so the CLI drives the web app over a logged-in browser session: read clients, invoices, purchase invoices, accounts, the PSD2 bank-transaction worklist and journal entries (kanne); create invoices and purchase invoices; record payments and incomings; post balanced kanne; stage a purchase invoice as a bank payment order; and save/delete bank-worklist rows.

> Template note: `<namespace>` fills at install. Replace this role paragraph with how *this* project uses SimpleBooks (which entity's books, what it issues, how it reconciles the bank worklist). Keep this file **lightweight** — role + pointers; the command surface is `simplbooks help`, not here.

## Interaction

Via the `simplbooks` CLI on `PATH`. This is a **stateful** CLI — it holds a login session — so run `simplbooks doctor` first (exit 0 proceed; exit 2 means the session is missing/expired and not auto-recoverable — read the message: a PIN gate needs `simplbooks login --pin <code>`, otherwise `simplbooks refresh` a Copy-as-cURL paste), then `simplbooks help` (the self-documenting source of truth for the command surface, the agent startup protocol, the credential cascade, the tax-class regimes, and the exit codes). See [identifiers.md](identifiers.md) for the account, the chart-of-accounts rows, and the ids this project addresses.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: how the account is selected, the bank/income chart-of-accounts row ids the write paths use, and any clients/invoices it refers to by hand.
- [reference.md](reference.md) — the standing home for project-specific operational context (which regime each client falls under, how a fee or income books to which account, the bank-worklist decision tree, any service/VAT catalogue). Ships empty as a self-describing scaffold; populated as context accrues.

> How this project **maps its books onto SimpleBooks** — which VAT regime a client uses, which chart-of-accounts row income or a fee posts to, how each bank-worklist row is decided and booked — is the **consumer's** domain (DOCTRINE rule 9): it lives here in these assets, not in the `simplbooks` capability itself. If this project runs an automated reconciliation / bank-worklist flow (which rows to process on what trigger, how each is booked, the idempotency policy), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
