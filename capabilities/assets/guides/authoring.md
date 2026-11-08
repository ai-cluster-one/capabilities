# Assets — authoring

Use this when keeping something as an asset - a plan, research, a session record, meeting notes, a journal entry, a digest or a report - or when deciding whether something found in one should govern later work. An asset is evidence: what was planned, found, said or done, as it stood when it was written.

## What Belongs Here

Evidence belongs in assets, and live doctrine does not. A rule, a decision that governs work now, or a procedure that repeats has its own home (Promoting Deliberately, below); an asset may be the record of how it was reached.

Search before writing: `assets search <terms>` finds the record that already covers a subject, so it gains a version rather than a rival.

## Naming

A document's name is a path whose top folder gives its kind: `plans/`, `research/`, `sessions/`, `meetings/`, `journal/`, `digests/` and `reports/`; any other folder is a note. Put a dated record's date first, as in `plans/2026-10-20_rollout`, so the names sort in time and say what they hold. The body's first `# ` heading is its title.

A file that is not markdown - an image, a log, a data file - is kept with `assets files add <name> --from FILE --document <document>`, beside the document it belongs to.

## Evidence Stays Evidence

A record keeps the tense and the facts of the time it was written. `assets save` adds a version and never edits one, so the history of a record is its versions: correct a mistake by saving a new version whose `--note` says why, and never rewrite a record to say what is true now.

`assets retire <name>` takes a record out of list, search and export; its versions stay readable.

## Promoting Deliberately

When evidence settles something that should govern later work, promote it on purpose to its one live home and leave the asset as the evidence for it:

- a standing rule or decision to a doctrine document, through `context save` (`context guide authoring`);
- a repeatable procedure to a routine, through `routine save` (`routine guide authoring`);
- one conclusion that later work finds by search to a memory entry, through `memory add`.

Write the promoted text fresh for its home, in the present tense, rather than copying the record or its history into it. Nothing is promoted only because it was written down.

## Saving

1. `assets show <name>` gives the active version and its id; a name not in use starts a new document.
2. Write the body to a file, each paragraph and each list item on one physical line.
3. `assets save <name> --from FILE --base <id> --note "<why>"`. A `stale_base` refusal means another save came first: read it again and make the change on that version.
