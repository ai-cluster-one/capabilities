# simplbooks — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `simplbooks`
- **Summary**: drive a SimpleBooks accounting account over its browser session (there is no public API) — health/recovery (`doctor`), interactive `login` (with PIN gate) and cURL-paste `refresh`; read clients, invoices, purchase invoices (expenses), accounts (chart + cashbooks), the PSD2 bank-transaction worklist, and manual journal entries (kanne); create invoices and purchase invoices, record payments/incomings, post balanced kanne, stage a purchase invoice as a bank payment order, and save/delete bank-transaction rows. Stateful — it holds a login session and auto-recovers an expired one when email+password are present.
- **Underlying service**: **SimpleBooks** (the Estonian accounting SaaS) with **no public API** — the CLI emulates a logged-in browser session against the server-rendered CakePHP app (cookie + per-form `_csrfToken`, form-encoded POSTs, HTML parsed back). Not bundled — the user supplies a SimpleBooks account and authorizes a session.
- **Has authored artifacts**: no.
- **Config dependency**: `global` — resolves credentials + the login session from `~/.config/simplbooks/` (or a project `.env`); usable from any project once a session is authorized.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. Inline script dependencies (`httpx`, `click`, `beautifulsoup4`) are resolved by `uv` on first run; `beautifulsoup4` is required because the only transport is HTML scraping (see [deviations.md](deviations.md)).
- **A SimpleBooks account + an authorized session** (hard) — every authenticated command needs a live session; the CLI is inert without one (only `help` runs keyless). The primary path is `SIMPLBOOKS_EMAIL` + `SIMPLBOOKS_PASSWORD` (auto-login, with a PIN gate on a new IP/device); the fallback is a Copy-as-cURL paste via `simplbooks refresh`. The session (`SIMPLBOOKS_ACCOUNT` + `SIMPLBOOKS_COOKIE`) is written back to `credentials.env` by `login`/`refresh` and reused thereafter.
- **Account config for the write paths** (soft) — `invoices create` needs `SIMPLBOOKS_WORKER` + `SIMPLBOOKS_BANK_ACCOUNT` (the optional `SIMPLBOOKS_INCOMEACCT_*` only override the live default income account); `expenses send-payment` needs `SIMPLBOOKS_BANK_TARGET`. Every read command runs without them. No id is baked into the tool, and the VAT taxonomy / rates / default income account are read live from SimpleBooks — these resolve from config or the live form.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/simplbooks/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/simplbooks` | `~/.capabilities/simplbooks/bin/simplbooks`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `simplbooks` resolves by name. |
| `stub.md` | `~/.capabilities/simplbooks/stub.md`, installed as `~/.claude/tools/simplbooks.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |
| `credentials.env.example` | copied to `~/.config/simplbooks/credentials.env` **with empty values**. |

The CLI also writes **session + cache state** into the same user-config dir at runtime: `credentials.env` is rewritten by `login`/`refresh` to persist `SIMPLBOOKS_ACCOUNT` + `SIMPLBOOKS_COOKIE`; `clients.json`, `sales_meta.json`, and `purchases_meta.json` are scrape caches (the live VAT-type maps + client list); `.pin_pending` holds an in-flight PIN flow. The session cookie is auth material — it grants account access; never commit it.

## Credentials

Env keys. The **non-secret account config** resolves by the standard [credential cascade](../../DOCTRINE.md#the-credential-cascade) (flag → project `.env(.local)` → user config → process env), so a consuming project can supply these in its own `.env`. The **session** credentials deviate to a two-tier resolution (user config → process env; no flags, no project `.env`), recorded in [deviations.md](deviations.md) — the session is never passed on argv, is written back by `login`/`refresh`, and one machine drives one SimpleBooks login.

| Key | Secret? | Notes |
|---|---|---|
| `SIMPLBOOKS_EMAIL` | **yes** | login email; primary auth path (auto-login) |
| `SIMPLBOOKS_PASSWORD` | **yes** | login password; primary auth path |
| `SIMPLBOOKS_COOKIE` | **yes** | session cookie — the live login; written by `login`/`refresh` |
| `SIMPLBOOKS_ACCOUNT` | no (account-identifying) | 32-char account hash; written by `login`/`refresh`, selects the account in URLs |
| `SIMPLBOOKS_WORKER` | no | invoice issuer name (write paths); no default — set it or pass `--worker` |
| `SIMPLBOOKS_BANK_ACCOUNT` | no | own bank account row id shown on invoices (`accounts list cashbook`); override `--bank-account` |
| `SIMPLBOOKS_BANK_TARGET` | no | bank integration target for `expenses send-payment`; override `--target` |
| `SIMPLBOOKS_INCOMEACCT_EE_DOMESTIC` / `_EU_REVERSE` / `_NON_EU` | no | **optional** books-policy override of the live default income account for a regime; set only when revenue books to a non-default chart row (e.g. EU/export → a 0% revenue account). Override per invoice with `--income-account-id` |
| `SIMPLBOOKS_INVOICE_FOOTER` | no | optional invoice footer line (a courtesy default ships built in) |

**Read live from SimpleBooks, never configured:** the sales/purchase VAT-type taxonomy and each type's VAT **rate** (the `vat_types` map on the add forms — see `invoices meta` / `expenses meta`), and the invoice **creator** (the logged-in user, read from the form). The income **account** defaults to the live map's value; the optional regime keys above only override it where the account's books policy differs from SimpleBooks' default. No chart-of-accounts row id is baked into the tool.

## Project artifacts

The whole `project/` template copies into `.capabilities/<namespace>/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/<namespace>/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/<namespace>/identifiers.md` |
| `project/reference.md` | `.capabilities/<namespace>/reference.md` (self-describing scaffold; populated on demand) |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | discoverable | infer from the project (dir name / existing `.capabilities/` convention); confirm if unsure | the `.capabilities/<namespace>/` path |
| `SIMPLBOOKS_EMAIL`, `SIMPLBOOKS_PASSWORD` | must-confirm | ask the user for the SimpleBooks login (enables auto-login + recovery) | the credentials env file |
| login session | must-confirm | run `simplbooks login` once (auto-login; answer the PIN gate with `simplbooks login --pin <code>` if prompted), or `simplbooks refresh` a Copy-as-cURL paste; it persists `SIMPLBOOKS_ACCOUNT` + `SIMPLBOOKS_COOKIE` | `~/.config/simplbooks/credentials.env` |
| `SIMPLBOOKS_WORKER` | must-confirm (write paths) | the invoice issuer name this install uses; only needed to create invoices | a project `.env` or the credentials env file |
| `SIMPLBOOKS_BANK_ACCOUNT` | leave-breadcrumb | only for `invoices create`; discover with `simplbooks accounts list cashbook` once a session resolves | a project `.env` or the credentials env file |
| `SIMPLBOOKS_INCOMEACCT_EU_REVERSE` / `_NON_EU` / `_EE_DOMESTIC` | leave-breadcrumb | only for `invoices create`, and only for a regime whose income books to a non-default row (the live default covers the rest); discover with `simplbooks accounts list chart` | a project `.env` or the credentials env file |
| `SIMPLBOOKS_BANK_TARGET` | leave-breadcrumb | only for `expenses send-payment`; the bank integration the account stages payments to | a project `.env` or the credentials env file |
| `SIMPLBOOKS_INVOICE_FOOTER` | leave-breadcrumb | only to override the built-in footer line | a project `.env` or the credentials env file |
| client / invoice / account ids | leave-breadcrumb | discovered live (`clients list`, `accounts list`, `invoices list`); pin the ones the project addresses repeatedly | `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `simplbooks`'s must-haves are **`SIMPLBOOKS_EMAIL` + `SIMPLBOOKS_PASSWORD` and a completed session** (or a `refresh`-ed cURL session) — resolved at install. `SIMPLBOOKS_WORKER` and `SIMPLBOOKS_BANK_ACCOUNT` are required the moment an invoice is created; the income-account overrides and bank-target are write-path breadcrumbs; the VAT taxonomy, rates, and the default income account are read live; none block install or the read commands.

## Deviations

Recorded in this capability's dedicated [deviations.md](deviations.md): the scraped-browser-session transport (no public API; `beautifulsoup4` + `click` deps), the stateful persisted-login secret with its two-tier session-credential resolution and PIN-gate ceremony, the exit-code taxonomy collapsed to `0/1/2`, and the human-first output with `--json` where structure matters (no keyless `contract` command). Each realizes the SHEBANG intent in the terms of an API-less, server-rendered service; an audit reads them there as choices, not drift.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `simplbooks help`).
- **No secret values in markdown** — `SIMPLBOOKS_EMAIL`, `SIMPLBOOKS_PASSWORD`, `SIMPLBOOKS_COOKIE` (and the account hash) live in env / the user-config dir, never in `identifiers.md` or any committed file.
- **The operating model lives in `simplbooks help`** — the command surface, the agent startup protocol, the credential cascade, the tax-class regimes, the price/number format, the I/O envelope, and the exit codes are all project-agnostic and are not transcribed into the assets.
- **Identity-free** — no account hash, no real client/invoice/supplier ids, and **no chart-of-accounts row ids** baked into the source or the public registry. The own bank account, any per-regime income override, and the bank target resolve from config; the VAT-type taxonomy, rates, and the default income account are read live from SimpleBooks; the sales `vat_type_id` regime anchors are platform taxonomy (bare ids). `identifiers.md` carries placeholders only.
- **The books-side mapping is the consumer's domain** (DOCTRINE rule 9) — which regime a given client falls under, which chart-of-accounts row income or a fee books to, the bank-transaction worklist decision tree, and any service/VAT catalogue belong to the **consumer's** assets and routines, never to simplbooks. simplbooks owns only how the account is driven and what each command reads or writes.
- A **reconciliation / bank-worklist-processing flow** (which rows to process on what trigger, how each is booked, the idempotency policy) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
- **The deviations in [deviations.md](deviations.md) are deliberate** — the scraped-session transport, the persisted-login secret + two-tier session cascade, the `0/1/2` exit codes, and the human-first output. The audit reads them there as choices, not drift.
