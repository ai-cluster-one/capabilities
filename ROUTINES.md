# Routines

The primary consumer of capabilities. A **capability** is what a seat *knows and can reach* about a system — how it is read, its identifiers, its model, its tool. A **routine** is a repeatable *procedure* that orchestrates one or more capabilities to get a recurring job done. Capability is the noun; routine is the verb.

Routines do not live in this repo — they live in the consuming project, beside that project's work. The concept lives here because the routine is the capability's nearest neighbour: it is what *consumes* capabilities, and the line where the two meet is where most authoring mistakes happen. Drawing the boundary here, on both sides, is what keeps a capability from quietly absorbing a procedure (or a routine from absorbing a model). This section is the start of that; it grows as the routine construct does.

## What a routine is

A routine is a self-contained, harness-independent recipe — one file, written so an agent can run it start to finish. It names the capabilities it needs, walks a procedure, and produces a result. It is repeatable and, where it mutates state, idempotent — deriving "already done" from the system of record, not a local cursor.

A routine is **not** a capability. It holds no identifiers, no model of a system, no tool surface; it reaches all of those through the capabilities it consumes. When a routine starts carrying a system's identifiers or re-explaining how a system is read, that content belongs in a capability — the routine has overreached.

## How a routine consumes a capability

A routine reaches a capability the way any consumer does — **by role, never by literal path**:

- **The tool** — it invokes the capability's CLI, whose surface is self-documenting (`<name> help`). It does not transcribe the commands.
- **The model** — it leans on the capability's reference for the treatment or mapping, naming it by role ("the handling model for the X contract"). It does not restate the model.
- **The identifiers** — it resolves them through the capability ("the X-side accounts live in the X capability identifiers"), never hardcoding the values.

This is the consuming end of the capability's own addressing rules ([DOCTRINE.md](DOCTRINE.md) rules 7 and 9): a routine names capabilities and their assets by role and lets the always-loaded capability files resolve them. Pointers flow one way — the routine points *down* to the capabilities it uses; a capability never points *up* to the routine (rule 8).

## The capability / routine split — what goes where

The split is the reading-vs-handling, model-vs-procedure line drawn through [DOCTRINE.md](DOCTRINE.md) rules 9 and 11:

| Belongs to the **capability** | Belongs to the **routine** |
|---|---|
| How a system is read; what its output represents | The procedure that acts on that output |
| Identifiers, the model / mapping, the tool surface | The ordered steps, their dependency order, escalation |
| Source-side facts (a peer system's contract shape) | Consumer-side treatment, applied in sequence |

When in doubt: a *fact or model* is a capability's; a *sequence of actions* is a routine's. A capability that grows a procedure has overreached; a routine that grows a model or a set of identifiers has absorbed what should be a capability's. This is exactly why the boundary clarifies what a capability *should not be*: anything that is "how to perform a recurring task" is a routine.

## Principles

The doctrine ([DOCTRINE.md](DOCTRINE.md)) governs the shared ones: a routine is **self-contained and resolved by role** (rules 7, 8), holds **procedure, not model** (rule 11), and is **affirmative and self-contained** (rule 12). Two are routine-specific:

1. **The description is load-bearing.** A routine's one-line description is surfaced into every session before its body is ever opened. It must state what the routine *does* and where its scope ends — affirmatively, in one line. It is how an agent decides whether to load the body.
2. **Idempotent from the source.** A routine that mutates state derives "already done" from the system of record, not a local file, so re-running over an overlapping range is safe.

## The formalization ladder

Not every recurring need becomes a routine. The gate, lightest to heaviest — promote only when the need has earned the next rung:

- **Nothing** — a one-off; do it and move on.
- **A recipe note** — a few lines captured where the work happens, for next time.
- **A skill / prompt** — a reusable prompt asset, when the steps are stable but light.
- **A routine** — a full recipe, when the procedure is recurring, multi-step, and worth surfacing into every session.

A routine authored before its time is overhead that rots; a procedure run often enough to deserve one but left as scattered notes is a recurring re-derivation tax. The ladder is the judgment between them.

## Template

```
---
name: <capability>-<verb>-<object>          # capability-bound; or a bare <verb>-<object> for cross-cutting
description: <one line — what it does and where scope ends, affirmative; surfaced into every session>
---

# <Title>

<One paragraph: what this routine accomplishes and which capabilities it consumes.>

## <Procedure section(s)>

<The ordered steps. Invoke capability CLIs by name; lean on capability references for the
model by role; resolve identifiers through the capability. State each step as an action.>

## <Confidence / escalation — if it mutates state>

<What is done deterministically vs. what is escalated for a human decision, and how.>
```

**Naming.** Capability-bound procedures take `<capability>-<verb>-<object>` (e.g. `gmail-mail-worker`); cross-cutting ones take a bare `<verb>-<object>` or a domain noun (e.g. `daily-digest`). The prefix groups routines in the sorted index by the system they drive.
