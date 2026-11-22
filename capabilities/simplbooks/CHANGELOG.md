# simplbooks — change log

## 2026-11-22 — `simplbooks help` is the menu, and `simplbooks help <verb>` one verb's whole contract

`simplbooks help` printed the whole reference, about 44 KB with flags on 146 of its lines, and `simplbooks help <anything>` fell through to the domain parser: outside a project the connection resolver refused it with exit 6 `connections_required`, and past it the parser refused the extra argument with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 7.3 KB: what the capability is, the startup protocol (doctor first, what exit 2 means, login, the PIN and the cURL fallback), every verb on one line saying what it answers, with its write verbs marked, the account id-spaces rule, the write gate and the exit codes, then the contract verbs the library renders, with no flag on any line. `simplbooks help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - for each of its 57 domain verbs, including the five `financial_transactions` verbs and every `payments` and `incomings` verb, whose flags the old help left to `--help`. Text several verbs share is one constant added to each of their pages: connection selection and the exit-code table on every domain page, the write gate on every write verb, the account id-spaces section on the verbs that take a chart code or cashbook id, the worklist note on the `bank-transactions` verbs, and the hidden legacy aliases on the verbs that still bind them. The full startup protocol is on `help doctor`, `help login` and `help refresh`; the tax classes, price format and optional pins are on `help invoices create`, and the pins on `help setup` too; the connections paragraph follows the library's page on `help connections`, and the identifiers envelope's location on `help ids`. The connections home, the declared connection and setup fields and the secret cascade are the library's pages of `connections`, `connection` and `setup`. A group, such as `simplbooks help kmd`, lists its verbs. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `simplbooks help`, and `simplbooks --connection <id> help [<verb>]` answers the same as without the selector. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `simplbooks` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), SimplBooks, and Estonian (`et`), SimplBooks, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `simplbooks` declares its title, SimplBooks

The manifest carries `title`, `SimplBooks`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `simplbooks`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — A named connection's session made under a connection's former name is found again under its id

The minted session of a named connection is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so a session already made survives the store's move to connection ids. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

A connection missing its `email` or `secret_env` is named by its label in the refusal, `doctor`'s included, where it was named by the id.

## 2026-11-02 — `simplbooks` reads back every column of a binding and a purchase's reference

`simplbooks payments show <id> --json` and `simplbooks incomings show <id> --json` give each `bound` row a `columns` object: every cell of that row of the bound-documents table under the page's own header label, with an unlabelled or repeated header keyed by its position. `simplbooks purchases show <id>` read from the edit form also returns the saved reference number (`reference`), the payment description (`payment_description`) and the payments and incomings bound to the purchase (`bindings`, read from its view page; `null` when that page cannot be read). The view-page read (`--view`, or a payment-locked purchase) returns every label/value pair the page shows as `fields`. Every existing key keeps its name and value.

## 2026-11-01 — `simplbooks` never posts a payment or incoming with a negative or zero amount

`simplbooks bank-transactions save <id> --as payment --sum <amount>` drops a minus that matches the row's own outflow and posts the amount unsigned, and refuses with exit 6 a zero sum or a minus on an inflow row. `simplbooks payments create` and `simplbooks incomings create` refuse a `--sum` of zero or below with exit 6 before anything is sent. A positive `--sum`, an omitted one, `--as incoming` and `--as kanne` behave as before.

## 2026-11-01 — `simplbooks clients list --search` no longer shrinks the client cache

`simplbooks clients list --search <text>` merges the clients it finds into the local client cache instead of replacing the cache with them, so a later `invoices create`, `purchases create` or other booking still resolves a client that was not among the hits. An unfiltered `simplbooks clients list` still rebuilds the cache from what it fetched. Output is unchanged.

## 2026-11-01 — `simplbooks` reads back bindings, an invoice's reference and any purchase's view

`simplbooks payments show <id>` and `simplbooks incomings show <id>` read one payment or incoming from its view page: its client, its own label/value fields, and each bound purchase or invoice with the amount it covers (`bound`). `simplbooks invoices show` also returns the invoice's saved reference text as `additional_info`, read from the edit form, or from the copy form when the invoice is payment-locked, with `additional_info_source` naming which. `simplbooks purchases show --view` reads the rendered view page of any purchase, not only a payment-locked one, and the view output now carries the purchase's bound payments and incomings as `bindings`. Nothing else changes.

## 2026-10-30 — `simplbooks` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon simplbooks` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — `simplbooks` declares what a person is asked for, and writes it through the contract

`simplbooks manifest` now declares a connection's fields under `connections.fields` (`email`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `simplbooks connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `simplbooks connection remove <id>` takes it out again with the secrets it wrote. The machine's prerequisites (`SIMPLBOOKS_WORKER`) are declared under `connections.setup`, and `simplbooks setup` reports them while `simplbooks setup set` writes them to `credentials.env`, a secret only from stdin. Bare `simplbooks help` ends with a section rendered from these declarations. Nothing else changes.
