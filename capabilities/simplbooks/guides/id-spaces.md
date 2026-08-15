# Account id-spaces

SimplBooks keeps two disjoint integer id-spaces for things it calls "accounts".
The same real ledger carries a different number in each, and every cashbook id is
also a live chart id — so a value from the wrong space used to resolve to a real
account and post silently. The CLI now makes the two lexically distinct.

## Chart of accounts — addressed by CODE

Four-to-five digit codes (minimum 1010), the CODE column of
`simplbooks accounts list chart`. Journal legs, invoice and purchase row
accounts, and a client's AR/AP/expense defaults are all chart accounts.

- `financial_transactions create --line 'side=debit:coa=<code>:sum=…'`
- `financial_transactions update --set-line '<idx>:coa=<code>'`, `--add-line 'side=…:coa=<code>:…'`
- `bank-transactions save --as kanne --counter-coa <code>`
- `purchases create --line 'coa=<code>:…'`, `purchases update --add-line/--set-line 'coa=<code>'`
- `invoices create --income-coa <code>`, `invoices update --add-line '…:coa=<code>'`
- `clients update --trade-receivables-coa/--trade-creditors-coa/--expense-coa <code>`
- `reports account-ledger <code>`

## Cashbooks — addressed by ID

One- or two-digit ids (1-8 today), the ID column of
`simplbooks accounts list cashbook`. A cashbook is the operational handle a
document attaches to, never a journal leg.

- `incomings create --bank-cashbook-id <id>`, `payments create --bank-cashbook-id <id>`
- `incomings list --bank-cashbook-id <id>`, `payments list --bank-cashbook-id <id>`
- `bank-transactions save --as incoming|payment --bank-cashbook-id <id>`
- `invoices create --bank-cashbook-id <id>` (the own-bank account printed on the invoice)
- `purchases send-payment --from-cashbook-id <id>`

## How the gate works

Two layers, both before anything is written:

1. **Lexical** — a chart-side flag refuses anything below 1010; a cashbook-side
   flag refuses anything above two digits. The three-digit zone, where chart
   internal ids live, belongs to neither space and is refused everywhere. This
   runs at parse time, before any lookup or request.
2. **Membership** — every account value in an outgoing form body is checked
   against its own live listing immediately before the POST, keyed on the full
   form-field path. `income_account_id` is a chart account inside an invoice's
   task rows and a cashbook inside an incoming or payment, so the path
   disambiguates what the field name cannot. This covers the raw `--set` and
   `--set-line` escape hatches too.

Every write then echoes the code and name of each account it touched, so a
mis-pointed booking is visible in its own success output.

Ids 1-8 exist in both spaces. Cashbooks 1 and 2 happen to resolve to chart 1010
Kassa and 1020 SEB — the accounts they were meant to hit — so a wrong mental
model tests clean there and misfires from cashbook 3 onward. Do not reason from
that coincidence; read the listing.

The legacy raw-id flags (`--counter-account`, `--account-id`, `--income-account-id`,
`--bank-account`, `--from-account`, `--expense-account`, and the `acct=` / `account=`
line keys) still bind for callers pinned to them. They take the internal id, are
existence-checked only, and are hidden from `--help` — compose from the names
above.
