# The capability template

What a capability is **comprised of** — the slots it fills and how they are shaped. This is the doctrine ([DOCTRINE.md](DOCTRINE.md)) applied to structure: a case study in where each kind of knowledge lives. It is deliberately **abstract** — slots, not any one capability — so a new capability slots in without re-deciding the shape, and the validator has a fixed thing to check against. The behavioural invariants and how to validate them live in the doctrine; this file holds the form.

## The mental model: two layers, hierarchical

A capability exists at two altitudes, and the split is the whole point:

- **Global (machine-level)** — declared *just enough to be visible*. The agent learns the capability exists, what it broadly does, and how to load its full contract on demand. One install per host.
- **Project (repo-level)** — *expansion*. A consuming project says how **it** uses the capability, stores **its** identifiers and mappings, and adds project-specific context or scripts. Per consuming project.

Global is the introduction; project is the elaboration. A fact lives at exactly one altitude and is never restated at the other (DOCTRINE rules 1–2).

**Neutrality is tiered.** Specifics earn their way *down* the slots; the declaration stays clean.

- The **spec** (the doctrine, this template, the procedures) is **domain-neutral** — slots and rules, no example domain's fingerprints.
- The **global stub** (slot 1) is **consumer-neutral** — install-once, identical for every project; it names the system and how to load its contract, nothing about any one consumer.
- The **project capability file** (slot 2) carries the project's *framing* — what the capability is for this project, plus pointers — but not its detail.
- **identifiers** (slot 4) and **reference** (slot 5) are where the **specifics live** — concrete values and the operational model. Domain terms here are not bleed; they are the point.

Neutrality is a property of the declaration, not of the whole capability. (Enforced by DOCTRINE rules 2 and 10.)

## Altitude × register: where anything lives

Two questions place any piece of knowledge, and crossing them leaves no blurry middle:

1. **Altitude** — is it *agnostic* (true for every project; ships once, in the framework or the CLI) or *project-specific* (true only here; a project asset)?
2. **Register** — is it *the product* (the knowledge itself, read and consumed) or *the meta* (how to author the product)?

|  | The product | The meta — how to author it |
|---|---|---|
| **Agnostic** | the CLI + `<name> help`; the global stub | the doctrine, this template, the procedures |
| **Project-specific** | identifiers (values), reference (model), routines (procedure) | *— nearly empty —* |

The bottom-right cell is nearly empty on purpose: authoring guidance does not vary by project — *how to write a good reference* is the same everywhere — so it collapses **up** into agnostic meta, written once by asset-type, never per capability. This is why there is **no usage-guide slot**: a per-capability "how to use this for my project" file straddles that empty cell, and its content is always really either the product (→ identifiers / reference / a routine) or agnostic meta (→ the framework). Procedure that is *executable* is a **routine**; procedure that is *the tool's surface* is the CLI's `help`; everything declarative is the **reference**.

## The five slots (+ one optional)

| Slot | Lives in | Altitude | Holds |
|---|---|---|---|
| 1. Global context | the **stub** (`~/.capabilities/<name>/stub.md`, surfaced as a skill) | global | tool awareness + how to load the full contract (`<name> help`) |
| 2. Project context | the **capability file** (`.capabilities/<ns>/CAPABILITY.md`, surfaced by the project loader) | project | role/purpose **in this project** + a pointer list. Lightweight. |
| 3. Pointers | the pointer list *inside* slot 2 | project | links to slots 4–5, loaded on demand — **the single home for those asset paths** (consumers address assets by role, not literal path; DOCTRINE rule 7) |
| 4. Identifiers | `.capabilities/<ns>/identifiers.md` | project | **non-secret structural** values only: paths, folders, variable names, gids |
| 5. Reference | `.capabilities/<ns>/reference.md` | project | project-specific operational **context** — the model (see below). Ships as a self-describing scaffold; populated on demand. |
| (opt) Artifacts | `.capabilities/<ns>/scripts/` etc. | project | sources the capability authors (most have none) |

Reference (slot 5) **ships by default as a self-describing scaffold** — a body that states its own purpose — so the home is always present and labeled, and no agent has to rediscover whether it should exist or what belongs in it. Its resting state is empty: populate it only when genuine project context accrues, replacing the scaffold note with that content. An empty reference is conformant, not a gap; never invent content to fill it.

There is no usage-guide slot. Procedure does not live with the capability: an *executable, repeatable* procedure is a **routine** in the consuming project, and a procedure that is merely *the tool's surface* is the CLI's own `<name> help`; the static model it applies stays in the reference (DOCTRINE rule 11). The always-present project assets are the capability file (slot 2), identifiers (slot 4), and the reference scaffold (slot 5); scripts are added only when the capability authors any.

## How each altitude reaches the agent

A capability is *declared* in the registry and *surfaced* by a host-specific **injection**: the declaration is host-neutral, the injection is the host's job. For the Claude Code host:

- **Global — skill.** The stub is symlinked to `~/.claude/skills/<name>/SKILL.md`. Claude Code auto-discovers every skill and surfaces its front-matter `name` + `description` into every session; the body loads on demand. No central file is edited.
- **Project — generated rule.** A capability-agnostic `SessionStart` hook, `.claude/hooks/build-capabilities-rule.sh`, writes `.claude/rules/CAPABILITIES.md` — a manifest of `@`-imports, one per `.capabilities/<ns>/CAPABILITY.md`. The harness auto-loads the rule file and expands the imports inline, so each stub loads **in full, uncapped** (a `SessionStart` hook echoing into the session is capped at ~10k characters; a rule file is not). Wired once per project; after that, every capability is drop-the-folder.

The CLI reaches the agent by a third, host-neutral route: the executable is symlinked from `~/.capabilities/<name>/bin/<name>` onto `PATH`, so any session invokes `<name>` directly. A consuming project never copies the CLI — it calls the one centralized executable by name.

Another host swaps the two injections for its own equivalents; the registry folder and the CLI-on-PATH route stay the same.

## What lives in identifiers vs. reference

The two project assets split by *shape of knowledge*:

- **identifiers (slot 4) — the values.** Discrete, lookup-able structural facts: ids, codes, account/tenant handles, gids, paths, folder and variable names, the label or classification of each, and breadcrumbs for values not yet pinned. The answer to *"what is the handle for X?"* If it belongs in a key–value list or a table row, it's an identifier. No prose narrative, no how-to, no treatment (DOCTRINE rule 4).
- **reference (slot 5) — the model and context.** The prose that is neither a value nor a step: how the system behaves, what its output represents, the operational model, the mapping or treatment the consumer applies. The answer to *"how does this work, what does it mean, how is it handled?"* Domain-specific terms belong here freely — this is the slot that holds them.

The line: a **value** (or a labelled set of values) is identifiers'; an **explanation or model** is the reference's; a **step-by-step to perform a task** is a routine's — never the reference's (DOCTRINE rule 11).

## Project layout

In a consuming project, capability assets live under `.capabilities/<ns>/` — one folder per installed capability: the entry file `CAPABILITY.md` (slot 2) beside the slot assets (`identifiers.md`, `reference.md`, optional `scripts/`). `ls .capabilities/` lists the installed capabilities. At `SessionStart` the project's `.claude/hooks/build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` — an `@`-import manifest the harness expands inline, so each `CAPABILITY.md` loads in full. Dropping a folder is the whole install.

## When a slot outgrows one file

The slots above are **flat by default** — one `identifiers.md`, one `reference.md`. A capability whose project usage is small keeps them that way.

When a single slot's content grows past one coherent file, **branch that slot into a sibling `<slot>/` folder** of focused files, and keep the slot's `<slot>.md` at its canonical path as a **thin index** — a pointer list into the sub-files, nothing more. (So `reference.md` stays put and gains a `reference/` folder beside it; `reference/<topic>.md` holds the detail.) This is DOCTRINE rule 7 applied one level down:

- The capability file (slot 2) still addresses the slot **by role** ("the reference") — it never learns the sub-file names. Only the slot's index knows them.
- Sub-files **within** a slot may link each other directly — they're declared together (the sibling allowance).
- `(capability, slot)` stays a **computable address**: the index's path never moved, so every consumer that named "the X reference" keeps resolving — the index absorbs the fan-out. A sub-file's literal path must never leak past its index.

Branch on genuine growth, not anticipation: a two-paragraph reference doesn't need a folder; a reference that has become five distinct topics does. Identifiers branch the same way when they earn it.

## Template variables

A capability declares its variables in its `manifest.md`, each classed by how it's resolved:

- **discoverable** — the procedure can find it (e.g. infer the namespace from the project dir). Resolve and fill.
- **must-confirm** — required, not safely guessable (e.g. a self-hosted service's base URL). The procedure **asks the user** before writing.
- **leave-breadcrumb** — can't be discovered now and isn't blocking. Write the empty env key in its proper place so a future run / first real use stumbles on it with context nearby.

A capability is **dysfunctional without its must-haves** — those are resolved (discovered or confirmed) at install; the rest become breadcrumbs.
