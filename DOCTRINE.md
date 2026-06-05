# Doctrine

The single source of truth for how capabilities — and the routines that consume them — are built. Every principle and rule lives here, **once**. Each rule states what must hold and why, and carries its own **Validate** clause: the test for whether a given capability meets it. Nothing restates these elsewhere — [TEMPLATE.md](TEMPLATE.md) applies them to a capability's structure, [ROUTINES.md](ROUTINES.md) applies them to the procedures that consume capabilities, and [procedures/audit.md](procedures/audit.md) is just the routine that walks this file and applies every Validate clause. Meeting every rule is the bar; an unmet rule is a violation to list.

## The standing rules

1. **One fact, one home.** A fact stated in two places is wrong the moment one copy changes; every fact — a value, a principle, a path — has exactly one home, and everything else references it.
   *Validate:* find any fact (an id, a principle, a command, a path) stated in more than one file; each restatement is a violation, consolidated to the single home the others point to.

2. **Just enough at each altitude.** Global is introduction only; the project capability file is expansion on *use*, not a re-teaching of the tool; detail lives in the on-demand assets, not the always-loaded files. Neutrality is tiered with altitude — the declaration slots (stub, capability file) carry no consumer- or domain-specifics; those belong in the asset slots (identifiers, reference).
   *Validate:* check the global stub for project specifics, the project capability file for re-taught tool detail or content that belongs in an asset, and either declaration slot for domain/consumer specifics that should sit in an asset slot; flag bloat or bleed at the wrong altitude.

3. **Discoverable knowledge belongs in the tool, not the docs.** If a fact *should* be answerable by running `<name> help`, it lives in the CLI's own help output, not transcribed into a markdown that goes stale — including the shape of any structured output the tool emits (a contract's fields and envelope), printed by the CLI (e.g. `<name> contract`) and derived from the producer's own builders so it cannot drift. A consumer doc names the home ("run `<name> contract`"), it never copies the shape.
   *Validate:* find command references or output-shape/schema transcribed into markdown; each belongs in the CLI's help or `contract`, replaced by a one-line pointer.

4. **Identifiers are non-secret, structural, pure lookup.** Connection-level and secret values (URLs, tokens, workspaces, hostnames-with-tenant) live in env, resolved by the cascade; the identifiers asset holds only non-secret structural values — ids, labels, classifications, and breadcrumbs for values not yet pinned — never a secret, and never the narrative of *how* a value is used in a transaction (that treatment is the reference's, rule 11).
   *Validate:* flag any secret or connection value sitting in a committed file, and any treatment prose in an identifiers file.

5. **Link platform docs, don't copy them.** For deep usage of the underlying service, point to its official docs (fetchable live) rather than transcribing; capture only what's specific to *this* capability or *this* project.
   *Validate:* find transcribed third-party documentation; replace with a link, keeping only the capability/project-specific part.

6. **Placeholders, never secrets.** Project files install with clearly-marked placeholders; credential files install as `*.example` with empty values; a real secret never enters the repo or a committed file.
   *Validate:* scan committed files for real secrets or filled credential values; any is a violation.

7. **Addressed by role, declared once.** A file that needs an asset names it by `(capability, slot)` — "the `<name>` identifiers", "the `<name>` reference" — and lets the always-loaded capability file (slot 2, the single home for those paths) resolve where it lives; it never hard-links the literal path. Across capability boundaries the same holds one level up: a capability needing a *sibling capability's* value names that **capability by name** ("resolved from the `<other>` capability") and resolves into its identifiers, never restating the value or hard-linking the sibling's file. (A capability's own sibling assets may link each other directly — they're declared together.)
   *Validate:* find literal asset paths hard-linked by consumers (should collapse to the slot-3 pointer list), and any sibling capability's value restated or hard-linked instead of named-and-resolved.

8. **Pointers flow down; an asset never names its consumer.** A reference or identifiers file describes the capability for whoever uses it — it never names the routine, workflow, or caller that invokes it. Consumers point *down* to the assets they need; assets never point *up*. A bidirectional link is two homes for one relationship and rots from the upper end.
   *Validate:* find any asset that names its consumer (a routine, workflow, or caller), or any bidirectional link; the asset should state what it is and let consumers find it by role.

9. **Domain ownership — which capability a fact belongs to.** A fact lives with the capability whose *domain* it describes, not whichever capability it is *about*. A read-only peer-system capability owns *how the system is read and what its output represents* — the source-side facts. How a **consumer** acts on that output — maps it to the consumer's own accounts, primitives, or treatment — belongs to the **consumer's** capability, even though the fact names the source system. Reading and handling are different domains. This is the horizontal companion to rule 1 (*one* home) and rule 2 (*which* altitude): rule 9 says *which capability*.
   *Validate:* in a capability's docs, find facts from a consumer's domain (the consumer's accounts, primitives, treatment); each is a violation, moved to the consumer's capability — the source capability keeps only how it is read and what its output represents.

10. **A capability never bakes in the consumer's identity.** A capability is portable, so it must not hardcode anything that pins it to one installation — the consuming entity's own name, its organization / tenant / account identifiers, or any value unique to a single consumer. Those live in the consumer's own identity files and resolve at use; the capability refers to the consumer by role ("the consuming entity", "the host project").
    *Validate:* find consumer-specific identity hardcoded in a capability doc; replace with a role reference resolved from the consumer's own files.

11. **Model and procedure each have one home.** The static model — a mapping, a treatment, a taxonomy — lives in the reference (slot 5). The *procedure* that applies it lives in a routine (in the consuming project), or — where it is merely the tool's surface — the CLI's own `help`. The procedure names the model by role and does not restate it; a model copied into both rots from whichever end changes first.
    *Validate:* find a model (a mapping, treatment, taxonomy) restated in both a reference and the procedure — a routine — that applies it; consolidate into the reference, the procedure naming it by role.

12. **Affirmative, self-contained framing.** A doc states what it *is*, in the present — not what it is not, what it *was*, or what a neighbouring system does ("never through X", "unlike Y", "formerly Z"). Minimise cross-referencing; keep a pointer only for a strong, specific routing reason. Change history lives in git, not the prose; a sibling system documents itself.
    *Validate:* find prose that defines by negation, by history ("formerly", "moved from"), or by a neighbour's behaviour; restate it affirmatively.

13. **Host-neutral declaration, host-specific injection.** A capability *declares* itself once, host-neutrally, in the registry — the immutable folder at `~/.capabilities/<name>/`, and `.capabilities/<ns>/` in a consuming project. *Surfacing* that declaration into an agent's sessions is a host-specific **injection**, owned by the host, not the capability. The injection is the same for every capability, so it is stated once per host and never restated per capability. For the Claude Code host: the global stub surfaces as a skill (`~/.claude/skills/<name>/SKILL.md`); the project entry surfaces through a `SessionStart` hook that regenerates `.claude/rules/CAPABILITIES.md` as an `@`-import manifest the harness expands inline (uncapped, where a `SessionStart` echo is capped at ~10k); the CLI surfaces by a symlink onto `PATH`. Another host swaps these for its own equivalents while the registry and the CLI-on-`PATH` route stay the same. This is the host-portability companion to rule 10 (consumer-portability): rule 10 keeps a capability free of one consumer's identity; rule 13 keeps it free of one host's wiring.
    *Validate:* find a capability that bakes a host's injection into itself — a hardcoded skills path, a hook command, or an `@`-import line carried in the capability instead of declared in the registry; and find the injection mechanism transcribed per capability rather than stated once per host. Each is a violation, consolidated to the host's single home for that mechanism.

## The credential cascade

Every capability's executable resolves its credentials with the **same 4-tier cascade** (first non-empty wins) — a contract the CLI must implement, not just document, because it is how config follows the developer across a laptop and a deployed host:

1. **Flags** — explicit `--…` overrides, per invocation.
2. **Project env** — `.env.local` then `.env`, discovered by **walking up** from `$CLAUDE_PROJECT_DIR` (else cwd) to the project root (stop at the first dir holding either, or a `.git` root). The project you're working in wins.
3. **User config** — `$XDG_CONFIG_HOME/<name>/credentials.env` (default `~/.config/<name>/`). The persistent default.
4. **Process env** — exported or host-injected variables. The fallback that lets a deployed box (no config files, secrets injected by the platform) resolve correctly.

*"System-injected"* and *"ambient export"* are indistinguishable at runtime (both are just process env), so they share tier 4. Process env sits **below** the user file deliberately — files are authoritative on a dev machine; injection governs on the box only because no file is present there. A one-shot override uses a flag, not an `export`.

*Validate:* confirm the executable resolves credentials in this order, and that the identifiers asset points to env for secrets rather than holding them (rule 4).

## Deviations are allowed — and recorded

These rules are a strong default, not a cage. A consuming project may deviate **with justification**. When it does, the deviation + reason is saved locally (machine- or project-scoped) so a later audit reads it as a **deliberate choice or a known edge case**, not drift to "fix." The validator advises; it does not force one path. A recorded, justified deviation is not a violation.
