# Procedure: uninstall a capability

You are an LLM agent. The user wants `<capability>` removed. Reason about the environment, and **confirm before deleting** anything that holds real values (a filled credentials file) or that you didn't create. Removal is harder to reverse than install — go slowly.

## 0. Inputs

- The **capability** to remove.
- The **scope**: this project, this machine, or both. Default to asking — a machine-level removal affects every project.

## 1. Read the manifest

Open `<capability>/manifest.md` for the full list of artifacts and their homes, so you remove exactly what was installed and nothing adjacent.

## 2. Project layer

- Remove the `.capabilities/<namespace>/` tree. Next session's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` without it — the capability's `@`-import drops on its own.
- If this was the **last** capability in the project, you may also remove `.claude/hooks/build-capabilities-rule.sh`, its `SessionStart` entry in `.claude/settings.json` (leaving sibling hooks intact), and `.claude/rules/CAPABILITIES.md`. Otherwise leave the wiring — it's capability-agnostic.
- **Scan first** for references to the capability elsewhere in the project (other rules files, pointers, scripts). Report them so no dangling links are left behind.

## 3. Global layer

Only if the user asked for machine-level removal:

- Remove the PATH symlink (`~/bin/<name>` or `~/.local/bin/<name>`), the skill symlink (`~/.claude/skills/<name>/`), and the registry folder (`~/.capabilities/<name>/`).
- **Credentials**: `~/.config/<name>/credentials.env` may hold real secrets. Show the user what you're about to remove and confirm explicitly before deleting it. Offer to keep it (orphaned but harmless) if they're unsure.

## 4. Record and report

If the capability had recorded deviations, note that their log entries are now stale. Report everything removed and everything intentionally left (e.g. a credentials file the user kept), plus any references found in step 2 that a human should clean up.
