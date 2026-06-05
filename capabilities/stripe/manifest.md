# stripe — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `stripe`
- **Summary**: read-only Stripe fetch CLI over the REST API — confirm identity + reachability (`doctor`), produce a neutral JSON contract of what happened on the account over a date range (`sync-plan`), print the contract's own shape (`contract`), list invoices and download their hosted PDFs (`invoices`), and read normalized balance-transactions and payouts (`balance-transactions`, `payouts`). It emits Stripe-domain facts only; every command is a read.
- **Underlying service**: **Stripe** (the SaaS), over its public REST API authenticated by a secret API key. Not bundled — the user supplies a Stripe account and a key that authorizes it.
- **Has authored artifacts**: no.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. Inline script dependencies (`httpx`) are resolved by `uv` on first run.
- **A Stripe secret API key** (hard) — every authenticated call reads it; the CLI is inert without one (only `help` and `contract` run keyless). See Credentials.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/stripe/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/stripe` | `~/.capabilities/stripe/bin/stripe`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `stripe` resolves by name. |
| `stub.md` | `~/.capabilities/stripe/stub.md`, surfaced by symlinking it as `~/.claude/skills/stripe/SKILL.md` — front-matter `name` + `description` load every session, body on demand. |
| `credentials.env.example` | copied to `~/.config/stripe/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). The secret never touches the command line in normal use — a one-shot override is `--api-key`.

| Key | Secret? | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | **yes** | the account's secret API key (`sk_live_…` / `sk_test_…`); read-only use only; never commit a value |

Per-project override: a consuming project may set `STRIPE_SECRET_KEY` in its own `.env` / `.env.local` to report on a different Stripe account than the global default. The account is whatever the resolved key authorizes — there is no separate account id to configure.

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
| `STRIPE_SECRET_KEY` | must-confirm | ask the user for a Stripe secret key (Dashboard → Developers → API keys); a restricted **read-only** key is sufficient and safer | the credentials env file (global) or project `.env` |

A capability is dysfunctional without its must-haves. `stripe`'s only must-have is **`STRIPE_SECRET_KEY`** — resolved at install. The account, its currencies, and any customer/invoice ids are read live from whatever the key authorizes; none are configured and none block install.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `stripe help`).
- **No secret key in markdown** — `STRIPE_SECRET_KEY` lives in env, never in `identifiers.md` or any committed file.
- **Platform model lives in `stripe help`** — the command surface, the cascade, the date-range convention (`--from` inclusive / `--to` exclusive, local-midnight), the read-only guarantee, the event taxonomy, and the I/O envelope + exit codes are all project-agnostic and are not transcribed into the assets.
- **Contract shape lives in `stripe contract`** — the event + envelope structure is printed by the CLI from the same builders the producer uses (DOCTRINE rule 3), so it cannot drift; assets name it ("run `stripe contract`"), they never copy it.
- **The contract → consumer mapping is the consumer's domain** (DOCTRINE rule 9) — how a consumer fills the contract's `resolve` seam with its own client/invoice ids, maps fees to accounts, or applies any tax treatment belongs to the **consumer's** capability and routines, never to stripe's assets. stripe owns only how the account is read and what the contract represents.
- **`identifiers.md` carries placeholders** — no real account, customer, or invoice ids baked into the public registry; the account is selected by the key in env.
- A **reconciliation routine** (which period to sync, on what trigger, how the contract is booked, the idempotency policy) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
