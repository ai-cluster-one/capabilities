# Stripe — identifiers

All fixed, **non-secret structural** values for this project's Stripe setup. Pure lookup; the operating model and command surface are in `stripe help`, the contract shape in `stripe contract`.

> Template note: Stripe has almost no project-side structural config — the account is selected entirely by the secret key in env, and customers / invoices / transactions are read live. Fill only what this project refers to by hand; leave the rest as breadcrumbs. **No secret key here** — it lives in env (see the connection note below).

## Account

The account is whatever `STRIPE_SECRET_KEY` authorizes; there is no separate account id to configure. Record only how this project selects it.

| Role | Value |
|---|---|
| Account / entity this project reads | `<the entity the resolved key belongs to>` |
| Mode | `<live \| test>` |

## Stable Stripe ids (optional breadcrumbs)

Most Stripe ids (`cus_…`, `in_…`, `txn_…`) are read live from the contract and need no record here. List one only if this project refers to it by hand repeatedly.

| Role | Stripe id |
|---|---|
| `<e.g. a recurring customer this project watches>` | `<cus_…>` |

## Key (value in env, never here)

The CLI resolves the secret key from env (project `.env` / the stripe `credentials.env`) per the credential cascade `stripe help` documents — only the structural fact that the account follows the key is recorded here.

| Env var | Holds | Use |
|---|---|---|
| `STRIPE_SECRET_KEY` | the account's secret API key | every authenticated call; selects the account |

Global tool: `stripe` on `PATH` (`stripe help` is the source of truth for the CLI surface; `stripe contract` for the contract shape).
