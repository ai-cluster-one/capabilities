# Routine — authoring

Use this when creating a new routine or substantially rewriting an existing one.

## When To Author

A routine is earned when a procedure repeats, carries enough sequencing that re-deriving it is waste or risk, and is harness-independent. Do not make a routine for a single CLI call, a small treatment note, or a situation whose shape genuinely differs every time.

The lightweight ladder is: inline judgment, recipe note, skill or prompt, routine. Pick the lightest rung that preserves the work.

## File Contract

One routine is one markdown file under `.routines/` in the consuming project. Producers live in `producer/`, processors live in `processor/`, and other procedures live at the top level.

Front matter has exactly two fields:

```yaml
---
name: <kebab-case>
description: <one line>
---
```

The filename stem equals `name`. The description is load-bearing session-start context: one affirmative line saying what the routine does and where its scope ends.

## Naming And Grouping

Placement carries the type; the filename names the subject.

- A producer enumerates a source into jobs. Name it for what it enumerates.
- A processor works one item. Name it for the item, singular.
- Other routines use a plain domain noun or verb-object name at top level.

## Body Contract

Open with the procedure and its scope boundary. Name the model the routine applies; do not copy mappings, account ids, live values, or capability command surfaces. Those live in capability help, guides, references, or identifiers.

Be self-contained against the harness, not against the whole repository: a worker should execute the routine from this file plus declared capability context, regardless of which host invoked it. Derive "already done" from the source system, never from repo state.

## Producers And Processors

A producer states the source it enumerates, where production stops, how "already produced" derives from source and tracker, the dedup key, the loop grain, and the job it compiles: substrate embedded or attached, plus the processor named in plain words.

A processor states the item it works, the idempotency guard, the recipe, and the result. Every item ends in a definite state; one that cannot be handled confidently is surfaced, never guessed.

Ordinary processors should have no task, board, session, or source awareness beyond the substrate plus recipe. A lifecycle or conveyor processor may name board stages when board state is its substrate.

## Mechanics

Write the file, run `routine validate`, then refresh the consuming project's generated context for the host that will use it.
