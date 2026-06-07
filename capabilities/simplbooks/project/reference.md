# Simplbooks — reference

> **Purpose.** The standing home for *project-specific* operational context: prose about how **this project** keeps its books in SimpleBooks, where that's neither a value nor the command surface. Which VAT regime each client falls under, how a fee or a kind of income books to which chart-of-accounts row, the bank-worklist decision tree (which rows get an invoice / a kanne / a delete), any service or VAT catalogue.
>
> Structural values (the account selection, the bank/income chart-of-accounts row ids the write paths use, stable client/invoice ids) live in `identifiers.md`; how the CLI and SimpleBooks behave — the command surface, the agent startup protocol, the credential cascade, the tax-class regimes, the price/number format, the I/O envelope + exit codes — lives in `simplbooks help` (project-agnostic, never copied here).
>
> How this project **maps its books onto SimpleBooks** — which regime a client uses, which account income or a fee posts to, how each bank-worklist row is decided and booked — is the **consumer's** domain (DOCTRINE rule 9): it belongs here, not in the `simplbooks` capability. An *executable* reconciliation / bank-worklist flow (which rows to process on what trigger, how each is booked, the idempotency policy) is a **project routine** in `.routines/`, not this file; point to it from here, don't embed it.
>
> Ships empty on purpose, so the home is always labeled and no agent has to decide whether it should exist or what goes in it. Populate it only when real context accrues — an empty reference is conformant, not a gap. Replace this note with that content when it arrives.
