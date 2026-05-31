# Procedure: uninstall a capability

You are an LLM agent. The user wants `<capability>` removed. Reason about the environment, and **confirm before deleting** anything that holds real values (a filled credentials file) or that you didn't create. Removal is harder to reverse than install — go slowly.

## 0. Inputs

- The **capability** to remove.
- The **scope**: this project, this machine, or both. Default to asking — a machine-level removal affects every project.

## 1. Read the manifest

Open `<capability>/manifest.md` for the full list of artifacts and their homes, so you remove exactly what was installed and nothing adjacent.

## 2. Project layer

- Remove `.claude/rules/capability/<NAME>.md` and the `.assets/<namespace>/` tree.
- **Scan first** for references to the capability elsewhere in the project (other rules files, pointers, scripts). Report them so no dangling links are left behind.

## 3. Global layer

Only if the user asked for machine-level removal:

- Remove the executable from `PATH` and the stub from its auto-load location; remove the `@`-import line from the user-level `CLAUDE.md`.
- **Credentials**: the file may hold real secrets. Show the user what you're about to remove and confirm explicitly before deleting it. Offer to keep it (orphaned but harmless) if they're unsure.

## 4. Record and report

If the capability had recorded deviations, note that their log entries are now stale. Report everything removed and everything intentionally left (e.g. a credentials file the user kept), plus any references found in step 2 that a human should clean up.
