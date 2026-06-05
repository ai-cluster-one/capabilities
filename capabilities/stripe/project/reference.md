# Stripe — reference

> **Purpose.** The standing home for *project-specific* operational context: prose about how **this project** uses Stripe, where that's neither a value nor the command surface. Which account it reads and why, which period it reconciles, what the contract means for this project.
>
> Structural values (the account selection, any stable Stripe ids) live in `identifiers.md`; how the CLI and Stripe behave — the command surface, the credential cascade, the date-range convention, the read-only guarantee, the event taxonomy, the I/O envelope + exit codes — lives in `stripe help`, and the contract's field + envelope shape in `stripe contract` (both project-agnostic, never copied here).
>
> How this project **maps the contract onto its own domain** — filling the `resolve` seam with its own client/invoice ids, booking fees to accounts, applying any tax treatment — is the **consumer's** domain (DOCTRINE rule 9): it lives with the project's own bookkeeping capability and routines, not in this stripe reference. An *executable* reconciliation flow (which period to sync, on what trigger, how it's booked) is a **project routine** in `.routines/`, not this file; point to it from here, don't embed it.
>
> Ships empty on purpose, so the home is always labeled and no agent has to decide whether it should exist or what goes in it. Populate it only when real context accrues — an empty reference is conformant, not a gap. Replace this note with that content when it arrives.
