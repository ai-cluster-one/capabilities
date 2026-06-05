# Procedure: update an installed capability

You are an LLM agent. The user wants to pull a newer version of an already-installed `<capability>` from this repo into this machine and/or project. Reason about the environment; ask before overwriting anything the user has customised.

## 0. Inputs

- The **capability** (or "all installed").
- The **scope**: machine, project, or both.

## 1. Re-fetch the global folder

Re-fetch the capability folder from the repo into `~/.capabilities/<name>/`, overwriting the immutable copy in place (the same fetch step [INSTALL.md](../INSTALL.md) uses). This repo is the source of truth — the author changes a capability here and pushes; you are the consumer pulling it back down.

- The registry folder is immutable, so a wholesale overwrite is correct. The PATH symlink and the skill symlink already point **into** the folder, so they need no change.
- Re-`chmod +x` the executable. If `credentials.env.example` gained keys, add the empty keys to `~/.config/<name>/credentials.env` without touching existing values.

## 2. Re-apply the project template, preserving local truth

The project assets under `.capabilities/<namespace>/` carry **filled placeholders and local edits** you must not clobber. Treat the refreshed `~/.capabilities/<name>/project/` as the new *template* and merge:

- Where the new template adds a slot, asset, or pointer, apply it; preserve the resolved values, the project's identifiers, and any recorded deviations.
- The `CAPABILITY.md` `@`-import regenerates automatically next session — no manual rule edit.

## 3. Re-audit

Run [audit.md](audit.md) afterward so any drift the update introduced (or resolved) is surfaced. Report what changed, what you preserved, and anything the user should review.
