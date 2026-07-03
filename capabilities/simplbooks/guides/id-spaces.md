# Account id-spaces

SimplBooks uses two separate integer id-spaces for "accounts", and different
commands want different ones:

- **Cashbook ids** — from `accounts list cashbook`. Used by
  `bank-transactions save --as incoming|payment --account-id N`, and by
  `incomings create` / `payments create --account-id N`.
- **Chart-of-accounts (COA) ids** — from `accounts list chart`. Used by
  `kanne create --line side=...:account=<coa-id>` (journal legs) and by
  `bank-transactions save --as kanne --counter-account <coa-id>`.

The same logical ledger has a DIFFERENT id in each space, so supplying a
cashbook id where a COA id belongs (or vice-versa) silently posts to the wrong
account rather than erroring. For `bank-transactions save --as kanne`, the CLI
reads the bank leg's account from the worklist row itself, so you supply only
the counter (offsetting) COA id via `--counter-account`.

Resolve the id for a given ledger from `accounts list …` at use-time.
