# Account id-spaces

SimplBooks keeps two disjoint integer id-spaces for things it calls "accounts", and the same real ledger carries a different number in each. Swedbank is cashbook 3 and chart account 1025 (internal id 211); Stripe is cashbook 7 and chart account 1024 (internal id 248).

Every cashbook id is also a live chart internal id, so a value from the wrong space used to resolve to a real account and post silently. The CLI now makes the two lexically distinct: chart accounts are addressed by their four-to-five digit CODE, cashbooks by their one-or-two digit ID, and the ranges cannot overlap.

## Which number addresses which ledger

`simplbooks accounts ledgers` is the discovery point. It derives the join live — one row per cashbook with the chart account it posts to and that account's code, read off each cashbook's own edit form, never matched by name and never declared anywhere. It also shows what each cashbook id resolves to when handed to a chart-side field, which is the whole failure mode in one table.

Read it to look an account up. Do not rely on having read it: the typed flags and the pre-POST validator hold whether or not you did.

`accounts list chart` and `accounts list cashbook` are the two raw listings behind it.

## Chart of accounts — addressed by CODE

Four-to-five digit codes, minimum 1010, the CODE column of `simplbooks accounts list chart`. Journal legs, invoice and purchase row accounts, and a client's AR/AP/expense defaults are all chart accounts.

- `financial_transactions create --line 'side=debit|credit:coa=<code>:sum=<amt>[:note=…]'`
- `financial_transactions update --set-line '<idx>:coa=<code>[:debit=…][:credit=…]'`, `--add-line 'side=…:coa=<code>:sum=…'`
- `bank-transactions save <id> --as kanne --counter-coa <code>`
- `purchases create --line 'coa=<code>:…'`
- `purchases update --add-line 'coa=<code>:…'`, `--set-line '<idx>:coa=<code>'`
- `invoices create --income-coa <code>`
- `invoices update --add-line '…:coa=<code>'`
- `clients update --trade-receivables-coa <code>`, `--trade-creditors-coa <code>`, `--expense-coa <code>`
- `reports account-ledger <code>`

## Cashbooks — addressed by ID

One- or two-digit ids (1-8 on this account), the ID column of `simplbooks accounts list cashbook`. A cashbook is the operational handle a document attaches to — never a journal leg.

- `incomings create --bank-cashbook-id <id>`, `incomings list --bank-cashbook-id <id>`
- `payments create --bank-cashbook-id <id>`, `payments list --bank-cashbook-id <id>`
- `bank-transactions save <id> --as incoming|payment --bank-cashbook-id <id>`
- `bank-transactions import --bank-cashbook-id <id>`
- `invoices create --bank-cashbook-id <id>` (the own-bank account printed on the invoice)
- `purchases send-payment --from-cashbook-id <id>`

## The 1-8 overlap

Cashbook ids 1-8 are all live chart internal ids too. Cashbooks 1 and 2 happen to resolve to chart 1010 Kassa and 1020 SEB — the very accounts they were meant to hit — so a wrong mental model tests clean on those two and misfires from cashbook 3 onward, where the lists diverge:

| value | as a cashbook | as a chart internal id |
| --- | --- | --- |
| 1 | Kassa | 1010 Kassa — same ledger, by coincidence |
| 2 | SEB | 1020 SEB Pangakonto — same ledger, by coincidence |
| 3 | Swedbank | 1030 Raha teel |
| 4 | Tasaarveldus | 1110 Aktsiad ja muud väärtpaberid |
| 5 | Swedbank krediidikonto | 1210 Nõuded ostjate vastu |
| 6 | Arveldused omanikuga | 1290 Ebatõenäoliselt laekuvad nõuded |
| 7 | Stripe | 1310 Nõuded emaettevõtte ja teiste grupi ettevõtete vastu |
| 8 | Binance | 1320 Nõuded sidusettevõtete vastu |

That coincidence is what made the error stable rather than random. Do not reason from it; run `accounts ledgers`, which re-derives the whole table live.

## How the gate works

Two layers, both before anything is written:

1. **Lexical** — a chart-side flag refuses anything below 1010; a cashbook-side flag refuses anything above two digits. The three-digit zone, where chart internal ids live, belongs to neither space and is refused everywhere. This runs at parse time, before any lookup and before any request leaves.
2. **Membership** — every account value in an outgoing form body is checked against its own live listing immediately before the POST, keyed on the full form-field path. `income_account_id` is a chart account inside an invoice's task rows and a cashbook inside an incoming, a payment or a bank re-import, so the path disambiguates what the field name cannot. This covers the raw `--set` and `--set-line` escape hatches and scraped round-trips too.

A rejection distinguishes unknown from inactive, since the chart lookup reads active accounts only. Every write then echoes the code and name of each account it touched, so a mis-pointed booking is visible in its own success output.

## The legacy raw-id path

`--counter-account`, `--account-id`, `--income-account-id`, `--bank-account`, `--from-account`, `--expense-account`, `--trade-receivables`, `--trade-creditors`, and the `acct=` / `account=` line keys still bind for callers pinned to them. They take the internal id, are existence-checked only, and are hidden from `--help`. Compose from the names above.
