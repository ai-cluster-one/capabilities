# Bank worklist model

The bank statement-import integration drops raw bank rows into SimplBooks —
typically overnight, and on demand for past periods. They land in the worklist
(`/bank_transactions/process`) and stay there until each is individually
evaluated and saved as one of:

- `incoming` — an inflow, bound to sales invoices;
- `payment` — an outflow, bound to purchase invoices;
- `kanne` — a manual journal entry, for a movement that has no invoice.

A genuine mis-import may be deleted; a real money movement never is.

The worklist does NOT block imports: new rows keep arriving even when the
oldest unprocessed one is months old, so a deferred pass only lengthens the
queue — it never stalls the sync.

CLI surface: `bank-transactions list | show | delete | save` (run `--help`
for options). `list` returns one row per entry with the fields needed to
decide; the command constructs and POSTs the payload but does not make the
save decision. This is the mechanism; which rows book which way is the
caller's booking policy, not part of the tool.
