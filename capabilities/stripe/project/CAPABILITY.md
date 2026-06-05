---
name: Stripe
description: The project's Stripe access — read-only reads via the `stripe` CLI (REST API) emitting a neutral JSON contract of what happened on the account: doctor, sync-plan (the contract producer), contract (its shape), invoice list + PDF download, and balance-transaction / payout reads. The way this project reads what cleared on Stripe.
---

# Stripe

The project's **Stripe access** — reading what happened on its Stripe account through the read-only `stripe` CLI. The producer command `stripe sync-plan --from … --to …` emits a neutral JSON contract (charges with gross/fee/net + customer + invoice, standalone fees, payouts, refunds) that this project's own logic maps onto its domain. Every `stripe` command is a read.

> Template note: `<namespace>` fills at install; the account is whatever `STRIPE_SECRET_KEY` authorizes (set in env, not here). Replace this role paragraph with how *this* project uses Stripe (which account, which period it reconciles, where the contract is booked). Keep this file **lightweight** — role + pointers; the command surface is `stripe help` and the contract shape is `stripe contract`, not here.

## Interaction

Via the `stripe` CLI on `PATH` — run `stripe help` first (the self-documenting source of truth for the command surface, the credential cascade, the date-range convention, the read-only guarantee, and the event taxonomy), `stripe contract` to see the exact contract shape, and `stripe doctor` to confirm the key resolves. See [identifiers.md](identifiers.md) for the account this project reads and any stable ids it references.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: how the account is selected and any Stripe ids it repeatedly references by hand.
- [reference.md](reference.md) — the standing home for project-specific operational context (which account and why, which period it reconciles, what the contract means here). Ships empty as a self-describing scaffold; populated as context accrues.

> How this project maps the contract onto its own domain — resolving customers/invoices to its own ids, booking fees, applying any tax treatment — is the **consumer's** domain and lives with the project's own bookkeeping capability and routines, not in this stripe asset. If the project runs an automated reconciliation flow (which period to sync, on what trigger, how the contract is booked), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
