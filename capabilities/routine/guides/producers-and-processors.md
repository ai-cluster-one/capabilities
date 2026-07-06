# Routine — producers and processors

A producer routine states:

- the source it enumerates and where production stops;
- how "already produced" derives from the source and tracker;
- the dedup key;
- the loop, one item at a time when judgment is needed;
- the job it compiles: substrate embedded or attached, plus the processor named in plain words.

A processor routine states:

- the item it works;
- the idempotency guard;
- the recipe;
- the result: every item ends in a definite state, and one that cannot be handled confidently ends surfaced, never guessed.

Ordinary processors should have no task, board, session, or source awareness beyond the substrate plus recipe. A lifecycle or conveyor processor may name board stages when the board state is its substrate.
