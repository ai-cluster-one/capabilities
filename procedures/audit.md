# Procedure: audit a capability (the validator)

You are an LLM agent acting as a **kind observer and reasonable judge**, not a cop. Your job is to check how a given capability is set up — on this machine and/or in this project — against the template, and to **advise**. You do not force one path, and you do not auto-apply changes. You surface drift, explain why it matters, and suggest the consolidation; the human decides.

## 0. Inputs

- The **capability** to audit (or "all installed").
- The **scope**: this project, this machine, or both.
- Read [../TEMPLATE.md](../TEMPLATE.md) (the invariants) and `<capability>/manifest.md` (the capability's declared shape).

## 1. Honour recorded deviations first

Before flagging anything, load the **deviation log** (project-scoped, e.g. `.claude/capability-deviations.md`, and any machine-scoped equivalent). If a setup detail that differs from the template is **already recorded with a justification**, treat it as a deliberate choice or a known edge case — acknowledge it, don't flag it. The template is a strong default, not a cage.

## 2. Check the invariants — semantically

Reason about each; these are judgments, not regexes:

- **Non-duplication (one fact, one home).** Scan the project for the capability's facts and identifiers restated in more than one place. Scattered IDs, a principle stated in three files, connection values copied into markdown — flag each with the single home they should consolidate into.
- **Asset paths addressed by role (rule 7).** Are consumers hard-linking literal asset paths (`.capabilities/<ns>/identifiers.md`) instead of naming the asset by role ("the Windmill identifiers") and letting the capability file resolve it? Flag scattered literal paths that should collapse to the single pointer-list home — a rename should touch one file, not every consumer. (A capability's own sibling assets linking each other directly is fine, not a finding.)
- **Just enough at each altitude.** Is the global stub a minimal introduction, or has it grown project specifics? Is the project capability file *lightweight* (role + pointers), or is it re-teaching the tool / carrying detail that belongs in an on-demand asset? Flag bloat at the wrong altitude.
- **Identifiers split, no secrets in markdown.** Are non-secret structural identifiers in `identifiers.md`, and connection-level/secret values in env (not the markdown)? Flag any secret or connection value sitting in a committed file.
- **Discoverable knowledge in the tool, not the docs.** Is anything transcribed into markdown that `<name> help` should answer? Suggest pushing it into the CLI's help and replacing the prose with a pointer.
- **Link, don't copy.** Are the underlying service's own docs transcribed (and rotting) instead of linked?
- **Branched slots are not drift.** A slot may legitimately branch into a sibling `<slot>/` folder with the slot's `<slot>.md` kept as a thin index (see [../TEMPLATE.md](../TEMPLATE.md) "When a slot outgrows one file"). Don't flag the branch itself. Check instead that: the index only points (no orphaned detail that belongs in a sub-file), the capability file still addresses the slot **by role** (not by a sub-file path), and no sub-file's literal path has leaked to a consumer outside the slot — flag any that has. Conversely, a flat slot that has clearly outgrown one coherent file may be *suggested* for branching, but only as cosmetic advice, never as structural drift.

## 3. Output — advisory report

Produce a report, not a diff applied to disk. For each finding: what it is, which invariant it touches, why it matters, and the concrete consolidation you suggest. Group by severity (structural drift vs. cosmetic).

## 4. Offer to record, not to force

If the user looks at a finding and says it's intentional, **offer to record it** in the deviation log with their reason, so the next audit stays quiet about it. If they want a finding fixed, you may then apply that fix — but only on explicit say-so, one at a time. Your default output is advice.
