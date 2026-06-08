# capabilities

A catalogue of **agent capabilities** — small, self-contained tools an LLM coding agent (Claude Code, Codex, …) can install into a machine and a project to gain a new ability: drive a workflow engine, talk to a task tracker, read a mailbox, keep a set of books.

Each capability is one folder under [`capabilities/`](capabilities/) — the catalogue. The repo root holds the **doctrine** (the rules and procedures); `capabilities/` holds the **capabilities** those rules govern. The repo is **source and distribution** at once: a capability is authored/changed here, pushed to GitHub, and pulled into any consuming project by pointing an agent at this repo and running a procedure. No installer binary — the procedures are **plain-English instructions an LLM executes**, adapting to the environment by reasoning, not by hardcoded branches.

> **Public repo.** Never commit a secret. Credentials ship only as `*.example` templates and empty-valued env breadcrumbs. A real token, key, URL-with-tenant, or account number must never land here.

## Capability index

| Capability | What it gives you |
|---|---|
| [capabilities/asana/](capabilities/asana/) | drive Asana projects & tasks over the REST API (tasks/subtasks, comments with @mentions, sections-as-status, tags, dependencies, the API-only `external` field, attachments) |
| [capabilities/windmill/](capabilities/windmill/) | drive a Windmill instance (deploy scripts, cron, jobs, vars) + the SSH-dispatch script pattern |
| [capabilities/directo/](capabilities/directo/) | drive a Directo ERP database over its browser-session endpoints (login ceremony, location selection, authed reads) |
| [capabilities/mail/](capabilities/mail/) | read and draft mail across Mail.app's configured accounts over macOS Automation (read/search/show/links/attachments/draft/export; never sends) |
| [capabilities/mailbox/](capabilities/mailbox/) | IMAP/SMTP adapter for one mailbox — list/show/fetch/flag/move and **send**; profiles in mailbox.json, app-password in .env |
| [capabilities/telegram/](capabilities/telegram/) | drive a personal Telegram account over MTProto (a full user account) — read/search a chat, send, export a chat's full history to JSON with voice/audio + photos/stickers, and transcribe voice/audio via Deepgram; stateful login session |
| [capabilities/notion/](capabilities/notion/) | publish markdown to Notion pages over the REST API (whoami, list child pages, fetch a page as markdown, replace a page's body+title, create under a parent, upsert by exact title); the local markdown H1 is the source of truth for the page title |
| [capabilities/stripe/](capabilities/stripe/) | read-only Stripe fetch CLI over the REST API — a neutral JSON contract of account activity over a date range (doctor, sync-plan, contract, invoices + hosted-PDF download, balance-transactions, payouts); emits Stripe-domain facts only, every command a read |
| [capabilities/simplbooks/](capabilities/simplbooks/) | drive a SimpleBooks accounting account over its browser session (no public API) — read clients/invoices/expenses/accounts/bank-transactions/kanne, create invoices & purchase invoices, record payments/incomings, post balanced journal entries, stage bank payment orders, and process the PSD2 bank worklist; stateful login session, identity-free (account ids resolve from env) |
| [capabilities/railwayc/](capabilities/railwayc/) | control a Railway project through the official `railway` CLI — a credential harness that injects a **project-scoped** token and forwards every command (status, logs, variables, redeploy, domain, …) straight to `railway` with I/O untouched; `doctor` proves the token and reports the project; requires the token and refuses account-wide/ambient auth, so access stays scoped to the one project + environment the token was minted for |

*(more get appended as they're extracted.)*

## Doctrine — the rules

Every capability and routine obeys one set of rules, stated once in [DOCTRINE.md](DOCTRINE.md). Each rule carries its own **Validate** clause — so the [audit](procedures/audit.md) is just the routine that walks the doctrine and applies them. Read the doctrine for *what must hold and why*; read the template for *what a capability is made of*.

## How a capability is shaped

Every capability fills one structural template — read [TEMPLATE.md](TEMPLATE.md); its executable fills one code template — read [SHEBANG.md](SHEBANG.md). In short, two layers:

- **Global** (machine-level, install once per host): the executable, its context **stub**, a credentials **template**. The source folder `capabilities/<name>/` installs to the host registry `~/.capabilities/<name>/` (undotted catalogue in the repo → dotted registry in `$HOME`); the CLI is symlinked onto `PATH` and the stub is surfaced by `@`-import (installed to `~/.claude/tools/<name>.md`, listed once in the host's global `CLAUDE.md`). Declared *just enough to be visible*.
- **Project** (repo-level, per consuming project): a lightweight `CAPABILITY.md` + `identifiers` + a self-describing `reference` scaffold, with `scripts/` if the capability authors any, under `.capabilities/<namespace>/`. A `SessionStart` script regenerates `.claude/rules/CAPABILITIES.md` — an `@`-import manifest the harness expands inline (script-generated, never hand-edited by the agent) — so they load each session. This is where a consuming project **expands** on how it uses the capability.

**Awareness, readiness, and usage are three different things** — and only the first is global. The **stub** is *awareness*: it tells any session the tool exists; it never promises the tool is usable here. **Readiness** — is it wired up in *this* context? — is resolved at use-time by the [credential cascade](DOCTRINE.md#the-credential-cascade) and self-reported by `<name> doctor`, which on failure names what's missing and where to put it. **Usage** — which targets, what model, project overrides — lives in the project files (`CAPABILITY.md` + identifiers + reference). So a capability may be fully usable from its global install alone (global `~/.config/<name>/` creds, or none at all), or it may need project-side config (e.g. `mailbox`'s project `mailbox.json`); either way the stub only announces it, and `doctor` reports whether it's ready.

A capability folder here mirrors that:

The capability folder is **flat**, and the installer copies it verbatim — the folder *is* the install image:

```
capabilities/<capability>/
  manifest.md                 the declarative spec the procedures read
  stub.md                     global stub (awareness; no front-matter); → ~/.capabilities/<name>/stub.md, installed as ~/.claude/tools/<name>.md and @-imported in the host CLAUDE.md
  bin/<name>                  the executable; → ~/.capabilities/<name>/bin/<name>, symlinked onto PATH
  credentials.env.example     env keys, no values; → ~/.config/<name>/credentials.env
  project/                    → .capabilities/<namespace>/ in a consuming project
    CAPABILITY.md             the entry file (role + pointers); @-imported into .claude/rules/CAPABILITIES.md
    identifiers.md            non-secret structural identifiers only
    reference.md              project-specific operational context (ships as a self-describing scaffold; populate on demand)
    scripts/                  authored sources (optional; Windmill has them)
```

Slots are **flat by default**. When one outgrows a single file, keep its `<slot>.md` as a thin index and move the focused files into a sibling `<slot>/` folder — see TEMPLATE's [*When a slot outgrows one file*](TEMPLATE.md#when-a-slot-outgrows-one-file).

## Routines — the neighbouring consumer

Capabilities exist to be *used*, and their primary consumer is the **routine**: a repeatable procedure — living in the consuming project, not here — that orchestrates one or more capabilities to do recurring work. The capability/routine boundary (model vs. procedure, reading vs. handling) is half of what defines a capability, so it's drawn here too: read [ROUTINES.md](ROUTINES.md).

## Procedures (the SOPs)

Point an agent at the relevant file and let it run. Each is environment-aware and interactive (it asks before it guesses).

- [procedures/package.md](procedures/package.md) — **the authoring entry point.** Turn a working shebang script into a full capability here: conform the executable to SHEBANG.md, scrub consumer specifics, author the doc slots, audit clean.
- [INSTALL.md](INSTALL.md) — **the install entry point.** Give an agent this one link; it asks which capability and places everything (registry folder, CLI on PATH, stub, project assets, the loader). The only file a consumer needs to start.
- [procedures/update.md](procedures/update.md) — pull newer versions of already-installed capabilities.
- [procedures/uninstall.md](procedures/uninstall.md) — remove a capability cleanly.
- [procedures/audit.md](procedures/audit.md) — the **validator**: a semantic, advisory drift check that applies every rule in the doctrine.

## Source → distribution → consumer

The author's machine is both ends of the loop:

1. **Author / change** a capability in this repo (the motherland).
2. **Push** to GitHub.
3. From any project, **run `procedures/update.md`** naming the capability — it pulls and re-applies, so every consumer benefits.

So a change starts at the source here, travels through GitHub, and lands back in each consumer on demand.

This repo — and its GitHub mirror — is the **single source of truth** for every capability, the **executable included**. The CLI symlinked onto a machine's `PATH` (e.g. `~/bin/<name>`) is a *downstream install*, not a second master: a change is authored **here** and reaches a machine only when `procedures/update.md` pulls it — never edited in place on `PATH`. If a live copy has drifted ahead (edited directly on a machine), that edit is first folded **back into the repo** to restore it as the single master, then flows outward again on the next update. The hierarchy is one-directional: repo → GitHub → installed CLI.
