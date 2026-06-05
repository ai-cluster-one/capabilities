# capabilities

A catalogue of **agent capabilities** — small, self-contained tools an LLM coding agent (Claude Code, Codex, …) can install into a machine and a project to gain a new ability: drive a workflow engine, talk to a task tracker, read a mailbox, keep a set of books.

Each capability is one folder under [`capabilities/`](capabilities/) — the catalogue. The repo root holds the **doctrine** (the rules and procedures); `capabilities/` holds the **capabilities** those rules govern. The repo is **source and distribution** at once: a capability is authored/changed here, pushed to GitHub, and pulled into any consuming project by pointing an agent at this repo and running a procedure. No installer binary — the procedures are **plain-English instructions an LLM executes**, adapting to the environment by reasoning, not by hardcoded branches.

> **Public repo.** Never commit a secret. Credentials ship only as `*.example` templates and empty-valued env breadcrumbs. A real token, key, URL-with-tenant, or account number must never land here.

## Capability index

| Capability | What it gives you | Has authored scripts? |
|---|---|---|
| [capabilities/windmill/](capabilities/windmill/) | drive a Windmill instance (deploy scripts, cron, jobs, vars) + the SSH-dispatch script pattern | yes |
| [capabilities/directo/](capabilities/directo/) | drive a Directo ERP database over its browser-session endpoints (login ceremony, location selection, authed reads) | no |
| [capabilities/mail/](capabilities/mail/) | read and draft mail across Mail.app's configured accounts over macOS Automation (read/search/show/links/attachments/draft/export; never sends) | no |
| [capabilities/mailbox/](capabilities/mailbox/) | IMAP/SMTP adapter for one mailbox — list/show/fetch/flag/move and **send**; profiles in mailbox.json, app-password in .env | no |

*(more get appended as they're extracted.)*

## Doctrine — the rules

Every capability and routine obeys one set of rules, stated once in [DOCTRINE.md](DOCTRINE.md). Each rule carries its own **Validate** clause — so the [audit](procedures/audit.md) is just the routine that walks the doctrine and applies them. Read the doctrine for *what must hold and why*; read the template for *what a capability is made of*.

## How a capability is shaped

Every capability fills one structural template — read [TEMPLATE.md](TEMPLATE.md); its executable fills one code template — read [SHEBANG.md](SHEBANG.md). In short, two layers:

- **Global** (machine-level, install once per host): the executable, its context **stub**, a credentials **template**. The source folder `capabilities/<name>/` installs to the host registry `~/.capabilities/<name>/` (undotted catalogue in the repo → dotted registry in `$HOME`); the CLI is symlinked onto `PATH` and the stub is surfaced as a skill (`~/.claude/skills/<name>/SKILL.md`). Declared *just enough to be visible*.
- **Project** (repo-level, per consuming project): a lightweight `CAPABILITY.md` + `identifiers` + a self-describing `reference` scaffold, with `scripts/` if the capability authors any, under `.capabilities/<namespace>/`. A `SessionStart` hook regenerates `.claude/rules/CAPABILITIES.md` — an `@`-import manifest the harness expands inline — so they load each session. This is where a consuming project **expands** on how it uses the capability.

A capability folder here mirrors that:

The capability folder is **flat**, and the installer copies it verbatim — the folder *is* the install image:

```
capabilities/<capability>/
  manifest.md                 the declarative spec the procedures read
  stub.md                     global stub (skill front-matter); → ~/.capabilities/<name>/stub.md, symlinked as ~/.claude/skills/<name>/SKILL.md
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
