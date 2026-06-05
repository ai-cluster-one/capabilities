# Procedure: audit a capability (the validator)

You are an LLM agent acting as a **kind observer and reasonable judge**, not a cop. You check how a capability is set up — on this machine and/or in this project — against the doctrine, and you **advise**. You do not force one path and you do not auto-apply changes: you surface drift, explain why it matters, and suggest the consolidation; the human decides.

The doctrine ([../DOCTRINE.md](../DOCTRINE.md)) holds every rule and, with each, its own **Validate** clause. This routine adds no rules of its own — it names the unit, the scope, and applies the doctrine.

## The unit and the scope

- **Unit** — the measuring unit is one **capability**: its files across the slots (the stub, `CAPABILITY.md`, identifiers, reference, any scripts), on this machine and/or in a consuming project. "All installed" runs the routine once per capability.
- **Scope** — this project, this machine, or both.

## Run

1. **Read [../DOCTRINE.md](../DOCTRINE.md) and the capability's `manifest.md`.** The doctrine's rules and their Validate clauses are the entire measuring stick.
2. **Honour recorded deviations first.** Load the deviation log (project-scoped, e.g. `.claude/capability-deviations.md`, and any machine-scoped equivalent). A difference from the doctrine that is **already recorded with a justification** is a deliberate choice, not a finding — acknowledge it and move on.
3. **Apply every rule.** Walk the doctrine top to bottom and run each rule's Validate clause against the unit — reasoning semantically, these are judgments, not regexes. **Every rule must be met.** For each rule that isn't, record a violation: what it is, which rule, and the consolidation the rule's Validate clause implies. A branched slot (a `<slot>/` folder with a thin `<slot>.md` index) is not itself a finding — check that the index only points, the capability file still addresses the slot by role, and no sub-file path has leaked to an outside consumer.

## Output — advisory report

Produce a report, not a diff applied to disk. List every violation found, grouped by severity (structural drift vs. cosmetic); for each, state what it is, the rule it breaches, why it matters, and the concrete consolidation. If a rule is met cleanly, it needs no mention — the report is the list of what isn't met.

## Offer to record, not to force

If the user looks at a finding and says it's intentional, **offer to record it** in the deviation log with their reason, so the next audit stays quiet about it. If they want a finding fixed, you may then apply that fix — but only on explicit say-so, one at a time. Your default output is advice.
