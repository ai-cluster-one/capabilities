# Routine — file format

## Structural Values

- **Location**: one `.md` per routine under `.routines/` in the consuming project. Producers live in `producer/`, processors in `processor/`, and the rest can live at the top level.
- **Index**: the harness compiler reads `name` and `description` from the front matter of every file recursively and emits the session-start index. Add a conforming file and it appears next session.
- **Front matter**: exactly two fields, `name` and `description`.

```yaml
---
name: <kebab-case>
description: <one line>
---
```

The filename stem equals the routine's `name`. The description is load-bearing: it is what every session sees before opening the file, so it must state what the routine does and where its scope ends, in one affirmative line.

## Naming And Grouping

Placement carries the type; the filename names the subject, not a verb prefix.

- **Producers** enumerate a source into tasks; they live in `.routines/producer/`, named for what they enumerate.
- **Processors** work one item; they live in `.routines/processor/`, named for the item, singular.
- **Everything else** takes a bare verb-object or domain noun and lives flat at the top level. Capability-bound procedures may keep a capability prefix when it improves scanning.

## Body Conventions

State what the routine is, affirmatively and in the present. Then:

- Open with the procedure and its scope boundary.
- Name the model; do not copy it. The routine is the how-to-run; mappings, account ids, and system models live in capability references or identifiers.
- Be self-contained against the harness, not against the repo. A reader executes the routine from this file plus declared capability context, regardless of how it was invoked.
- Keep scheduled entrypoints semantic. A scheduler names the routine and substrate; the procedure stays in the routine.
- Derive "already done" from the source system, never from repo state.
- Say one-item-at-a-time explicitly where each item deserves a real decision.

## Mechanics

1. Decide whether the need has earned a routine.
2. Pick the name and placement.
3. Write the two-field front matter and the body.
4. Run `routine validate`.
5. Let the harness index it on the next context rebuild.
