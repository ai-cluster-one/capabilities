# Routine — overview

A routine is a self-contained, harness-independent recipe for one repeatable procedure, living as a single markdown file in a consuming project's routines directory. The system is three parts: the folder that holds the files, the front matter that indexes them, and a session-start compiler in each harness that surfaces the index into the session's always-on context.

Install the routine capability once per machine/project, then author routines into the consuming project. The products of this capability are producer and processor recipes, task-worker contracts, and other repeatable procedures. Read existing project routines as worked examples.

The CLI is the source of truth for its own command surface. Use the overview only to understand what routine is: a small project-side mechanism that keeps repeatable procedures as files, validates their indexable metadata, and lets the harness surface only the compact routine menu into session context. Creation, review, and harness surfacing each have their own guide because they answer different questions: how to write one, how to judge an existing corpus, and how the generated index reaches an agent.
