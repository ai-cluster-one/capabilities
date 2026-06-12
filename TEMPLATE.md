# The capability template

What a capability is **comprised of** — the artifact it ships and the envelope it manages. This is the doctrine ([DOCTRINE.md](DOCTRINE.md)) applied to structure: a case study in where each kind of knowledge lives. It is deliberately **abstract** — slots, not any one capability — so a new capability slots in without re-deciding the shape, and `capabilities audit` has a fixed thing to check against. The behavioural invariants and how to validate them live in the doctrine; this file holds the form.

## The mental model: one artifact, two altitudes

The **script is the sole shipped artifact**. Everything a capability declares about itself — its domain verbs, its awareness line, its machine-readable manifest, its documentation access, its identifier management, its gate — is a verb on that one file (the contract, [SHEBANG.md](SHEBANG.md#the-contract-verbs)). What surrounds the script is *managed*, not shipped: the manager snapshots its declaration into the registry at install, and the script maintains its project envelope where it is used. One artifact, one version, no skew between the pieces.

A capability exists at two altitudes, and the split is the whole point:

- **Global (machine-level)** — declared *just enough to be visible*. One generated skill carries awareness of every installed capability; the full contract loads on demand (`<name> help`). One install per machine.
- **Project (repo-level)** — *expansion*. A consuming project enables the capability, holds **its** settings and identifiers, and accrues **its** references. Per consuming project.

Global is the introduction; project is the elaboration. A fact lives at exactly one altitude and is never restated at the other (DOCTRINE rules 1–2).

**Neutrality is tiered.** Specifics earn their way *down* the slots; the declaration stays clean.

- The **spec** (the doctrine, this template, [SHEBANG.md](SHEBANG.md)) is **domain-neutral** — slots and rules, no example domain's fingerprints.
- The **declaration** (the `stub` line, the manifest) is **consumer-neutral** — identical for every project; it names the system and how to load its contract, nothing about any one consumer.
- The **project envelope** (settings, identifiers, references) is where the **specifics live** — concrete values and the operational model. Domain terms here are not bleed; they are the point.

Neutrality is a property of the declaration, not of the whole capability. (Enforced by DOCTRINE rules 2 and 10.)

## Altitude × register: where anything lives

Two questions place any piece of knowledge, and crossing them leaves no blurry middle:

1. **Altitude** — at what scope is it true? *Framework* (every capability; ships once in the doctrine, this template, the executable standard), *capability* (this tool, every project; ships once in the script and its guides), or *project* (only here; the project envelope).
2. **Register** — is it *the product* (the knowledge itself, read and consumed) or *the meta* (how to author the product)?

|  | The product — read and consumed | The meta — how to author it |
|---|---|---|
| **Framework** — every capability | *(none — the framework is meta)* | the doctrine, this template, [SHEBANG.md](SHEBANG.md); the authoring procedures the manager emits (`capabilities new` / `conform` / `groom`) |
| **Capability** — this tool, every project | the CLI + `<name> help`; the `stub` line | the **guide** — `<name> guide [topic]`, fetched live (DOCTRINE rule 14) |
| **Project** — only here | settings + identifiers (values), references (model), routines (procedure) | *— empty: authoring guidance is never project-specific —* |

The project-meta cell is empty on purpose: authoring guidance never varies by project, so it always rises to one of the two agnostic homes — to **framework** meta when it holds for every capability (*how to write any reference* → this template), or to **capability** meta when it is specific to one tool (*how to author X with this tool* → that capability's guide, DOCTRINE rule 14). What does **not** exist is a *project-altitude* usage-guide: a per-project "how to use this here" file is always really the product (→ settings / identifiers / a reference / a routine) or it rises to one of those two meta homes. Procedure that is *executable* is a **routine**; procedure that is *the tool's surface* is the CLI's `help`; everything declarative and project-specific is a **reference**.

## The slots

| Slot | Lives in | Altitude | Holds |
|---|---|---|---|
| The script | `bin/<name>` upstream → registry copy at `~/.capabilities/<name>/bin/<name>`, symlinked onto `PATH` | capability | the whole shipped artifact: domain verbs + the contract verbs |
| The declaration | verbs on the script — `stub`, `manifest --json` — snapshotted by the manager at install | capability | the one-line awareness text; the machine-readable manifest (name, summary, credential scope and keys, docs base, state flag) |
| Guides *(opt)* | `guides/<topic>.md` beside the script upstream, surfaced by `<name> guide` fetched live | capability | consumer-neutral *how to author X with this tool* docs (DOCTRINE rule 14); present only when the tool has authoring depth |
| Settings | `.capabilities/<name>/settings.json` — capability-owned, free shape | project | the non-secret, connection-independent values someone **chose**: behavioural defaults, tuning |
| Connections | `.capabilities/<name>/connections.json` — standard envelope (`default` pointer + `connections` map), entry interior capability-owned | project | the project's named endpoints and identities: per-connection non-secret wiring, secrets by env-key indirection (`secret_env`), the write gate (`allow_write`) |
| Identifiers | `.capabilities/<name>/identifiers.json` — CLI-managed envelope, surfaced by `<name> ids` | project | the non-secret structural values the CLI **discovered**: ids, labels, classifications, breadcrumbs |
| References | `.capabilities/<name>/*.md` — front-matter envelope + free prose, surfaced by `<name> refs` | project | the project-specific operational **model**: mappings, treatments, what output means here |
| State | `.capabilities/<name>/state/` or `$XDG_STATE_HOME/<name>/`, per declared scope | project / user | what credentials mint: sessions, caches, pending logins. Never committed (DOCTRINE rule 16) |
| Deviations *(opt)* | `deviations.md` beside the script upstream | capability | recorded, justified departures from the standard, kept apart so an audit reads them as choices |

The project envelope's resting state is **empty**: `capabilities enable` writes the gate entry and nothing else. Settings appear when the project chooses values, connections when the project names more than the cascade's one implicit `default`, identifiers when the CLI discovers them, references when genuine project context accrues. An empty envelope is conformant, not a gap; never invent content to fill it.

## How each altitude reaches the agent

The capability declares itself host-neutrally; the **manager owns the injection** (DOCTRINE rule 13). For the Claude Code host:

- **Global — the generated skill.** `~/.claude/skills/capabilities/SKILL.md`, regenerated on every install/uninstall/update, with the installed capability names embedded in its description — so naming a capability trigger-matches the skill in any session, with zero project context. The body teaches the protocol: the manager verbs, the contract verbs, the exit-4 rule.
- **Project — the generated rule.** `capabilities context --claude`, wired by `capabilities init` as the project's single `SessionStart` hook line, composes `.claude/rules/CAPABILITIES.md` from the gate, the registry snapshots, and the project envelopes: per enabled capability, the stub line, the connections menu, the identifiers menu, the references menu, and the discovery pointers. Composition is files-only — no subprocess fan-out at session start — and degradation is per-capability, never whole: a broken envelope renders as a warning line, an enabled-but-not-installed capability as a one-line install pointer.
- **CLI on `PATH`** — the host-neutral third route: the registry copy is symlinked onto `PATH`, so any session invokes `<name>` directly. A consuming project never copies the CLI.

Through that same CLI route a capability's **guides** reach the agent: `<name> guide [topic]` resolves against the capability's declared docs base and prints the doc, fetched **live** (the request, cache, and fallback mechanics are the executable standard's — [SHEBANG.md](SHEBANG.md#guides)). A guide is never copied into a consuming project; a project's reference points to it by role (DOCTRINE rule 14).

The Codex host swaps the placement, not the composition: the same skill file at `~/.agents/skills/capabilities/`, and `capabilities context --codex` upserting the managed block in `AGENTS.md`. Another host swaps the injection while the registry and the CLI-on-`PATH` route stay the same.

## Settings vs identifiers vs reference

The project homes split by **provenance and shape**:

- **settings — the values someone chose.** Connection-independent configuration a human or agent decided at setup: behavioural defaults, tuning. Free shape — the standard names the location; the capability owns the schema. Written at configuration time, by whoever configures.
- **connections — the endpoints and identities the project talks to.** Each entry carries its non-secret wiring literally, names its secrets by env-key indirection, and may carry the write gate; a `default` pointer names which entry an unflagged invocation uses. Standard envelope so the manager renders every capability's connections without understanding any of them; the entry interior is the capability's (SHEBANG.md's [Connections](SHEBANG.md#connections)). Written at configuration time, by whoever configures.
- **identifiers — the values the CLI discovered.** Discrete, lookup-able structural facts: ids, codes, account handles, labels, classifications, and breadcrumbs for values not yet pinned. The answer to *"what is the handle for X?"* Thin standard envelope (`{label: {value, note}}`) so the manager renders every capability's menu without understanding any of them; the CLI writes through `<name> ids set`. No prose narrative, no treatment (DOCTRINE rule 4).
- **reference — the model and context.** The prose that is neither a value nor a step: how the system behaves, what its output represents, the mapping or treatment the consumer applies. The answer to *"how does this work, what does it mean, how is it handled?"* Domain-specific terms belong here freely — this is the slot that holds them.

Settings and connections vs identifiers is a provenance split — different writer, different cadence, different git-diff meaning — never merged. The line for the rest: a **value** is settings', connections', or identifiers'; an **explanation or model** is a reference's; a **step-by-step to perform a task** is a routine's — never a reference's (DOCTRINE rule 11).

## References: the envelope and growth

A reference is a markdown file in the capability's project folder, carrying a two-key front-matter envelope:

```markdown
---
name: <kebab-slug>
description: <one line — what this reference holds>
---
```

`<name> refs` and the context build read only the front-matter; the body is free prose, loaded on demand. References are **per-topic files by design**: growth adds a sibling file with its own front-matter, and the menu grows with it — no index file to maintain, no slot to branch. The front-matter `description` is load-bearing the way a skill description is: it is how an agent decides whether to load the body.

## Credentials at install

The manifest declares each credential key as `{key, secret, required, note}` and the capability's **scope** (`project` / `user`). `capabilities install` scaffolds accordingly — the key names with empty values land in the scope's home (project `.env` / user `credentials.env`), placeholders never secrets (DOCTRINE rule 6):

- A **required** key left empty is dysfunction `doctor` names at use-time — the remediation is discovered from the tool, not asserted in a doc.
- An **optional** key scaffolds as a breadcrumb: the empty key sits in its proper place with its note nearby, so a future need stumbles on it with context.
- A declared **post-install** step (e.g. a login ceremony) is offered at install, idempotent, never forced.
