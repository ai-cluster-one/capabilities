# capabilities

A catalogue of **agent capabilities** — small, self-contained tools an LLM coding agent (Claude Code, Codex, …) can install into a machine and a project to gain a new ability: talk to Asana, drive Windmill, read a mailbox, keep books in SimplBooks.

Each capability is one top-level folder. The repo is **source and distribution** at once: a capability is authored/changed here, pushed to GitHub, and pulled into any consuming project by pointing an agent at this repo and running a procedure. No installer binary — the procedures are **plain-English instructions an LLM executes**, adapting to the environment by reasoning, not by hardcoded branches.

> **Public repo.** Never commit a secret. Credentials ship only as `*.example` templates and empty-valued env breadcrumbs. A real token, key, URL-with-tenant, or VAT number must never land here.

## Capability index

| Capability | What it gives you | Has authored scripts? |
|---|---|---|
| [windmill/](windmill/) | drive a Windmill instance (deploy scripts, cron, jobs, vars) + the SSH-dispatch script pattern | yes |

*(more get appended as they're extracted — Asana, mailbox, SimplBooks, Telegram, …)*

## How a capability is shaped

Every capability follows one template — read [TEMPLATE.md](TEMPLATE.md). In short, two layers:

- **Global** (machine-level, install once per host): the executable, its context **stub**, a credentials **template**. Declared *just enough to be visible*.
- **Project** (repo-level, per consuming project): a lightweight capability file + `identifiers` / `reference` / `guide` assets (+ optional `scripts/`). This is where a consuming project **expands** on how it uses the capability.

A capability folder here mirrors that:

```
<capability>/
  manifest.md                 the declarative spec the procedures read
  global/
    bin/<name>                the executable (must end up on PATH, executable)
    stub.md                   the context stub (must end up auto-loaded every session)
    credentials.env.example   env keys, no values
  project/
    capability.md             → .claude/rules/capability/<NAME>.md (auto-loaded)
    assets/                    → .assets/<namespace>/
      identifiers.md          non-secret structural identifiers only
      reference.md            seat/project-specific operational context
      <name>-guide.md         the usage / authoring guide
      scripts/                authored sources (optional; Windmill has them)
```

## Procedures (the SOPs)

Point an agent at the relevant file and let it run. Each is environment-aware and interactive (it asks before it guesses).

- [procedures/install.md](procedures/install.md) — add a capability to this machine and/or the current project.
- [procedures/update.md](procedures/update.md) — pull newer versions of already-installed capabilities.
- [procedures/uninstall.md](procedures/uninstall.md) — remove a capability cleanly.
- [procedures/audit.md](procedures/audit.md) — the **validator**: a semantic, advisory drift check against the template.

## Source → distribution → consumer

The author's machine is both ends of the loop:

1. **Author / change** a capability in this repo (the motherland).
2. **Push** to GitHub.
3. From any project, **run `procedures/update.md`** naming the capability — it pulls and re-applies, so every consumer benefits.

So a change starts at the source here, travels through GitHub, and lands back in each consumer on demand.
