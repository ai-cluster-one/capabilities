# Routine — overview

A routine is a self-contained, harness-independent recipe for one repeatable procedure, living as a single markdown file in a consuming project's routines directory. The system is three parts: the folder that holds the files, the front matter that indexes them, and a session-start compiler in each harness that surfaces the index into the session's always-on context.

Install the routine capability once per machine/project, then author routines into the consuming project. The products of this capability are producer and processor recipes, task-worker contracts, and other repeatable procedures. Read existing project routines as worked examples.

The CLI helps inspect the project-side routine corpus:

- `routine doctor` checks whether the project has routines and whether their front matter validates.
- `routine list` shows the routines discovered under `.routines/`.
- `routine validate` enforces the indexable front matter contract.
- `routine index` renders the always-on routine menu from front matter.
