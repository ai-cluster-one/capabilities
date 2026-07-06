# Routine — groom

Use this to review an existing routine or routine corpus for semantic quality. `routine validate` checks the index contract; grooming checks whether the routine is still worth carrying and whether it says the right thing at the right altitude.

## Flow

1. Run `routine validate`.
2. Load `routine guide authoring`.
3. Read the target routine body.
4. Judge only against the authoring contract and the routine's current job in the consuming project.

## Checks

- **Earned** — the procedure still repeats and is heavier than a note, prompt, or one CLI call.
- **Description** — the front-matter description is enough for a fresh session to decide whether to load the file.
- **Scope boundary** — the file says where the routine starts and stops.
- **Role fit** — producer, processor, or top-level placement matches what the routine does.
- **Idempotency and outcome** — producer/processor routines state the dedup or idempotency guard and the definite result or surfaced unresolved state.
- **Model split** — the routine applies models; it does not duplicate capability help, guides, references, identifiers, account mappings, live ids, or platform documentation.
- **Harness independence** — the routine does not depend on one host's session mechanics unless that host state is the substrate.
- **No rot markers** — no stale history framing, temporary to-dos, unresolved migration notes, or old names used as doctrine.

## Output

Return `no findings` when the routine is clear, earned, and well-placed.

For findings, group by file. Each finding includes the quoted fragment, the violated contract by role, and the minimal proposed fix or move. Patch only when the caller asked for edits; otherwise report findings.
