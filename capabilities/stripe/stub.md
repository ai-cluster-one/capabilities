---
name: stripe
description: Stripe CLI — read-only Stripe reads over the REST API, emitting a neutral JSON contract of what happened on the account (sync-plan), plus identity/reachability (doctor), the contract's own shape (contract), invoice list + hosted-PDF download, and normalized balance-transaction / payout reads. Identity-free and read-only — every command is a Stripe read. Run `stripe help` for the full command surface before the first subcommand in a session.
---

Stripe CLI — read-only Stripe reads over the REST API, emitting a neutral JSON contract of what happened on the account.

- Executable: `stripe` (on `PATH`)
- Credentials: `STRIPE_SECRET_KEY` = a Stripe secret API key, in `~/.config/stripe/credentials.env` or a project `.env`. A one-shot override is `--api-key`.
- Load full reference: `stripe help` — the command surface, the credential cascade, the date-range convention, the read-only guarantee, and the event taxonomy all live there. The contract's field + envelope shape is `stripe contract` (keyless, built from the producer's own builders so it never drifts).

Read-only — every command is a Stripe read. Run `stripe help` before issuing any subcommand the first time in a session, and `stripe doctor` to confirm the key resolves. Project-scoped context (which account, how the contract is booked downstream) lives in each project's own assets, not here.
