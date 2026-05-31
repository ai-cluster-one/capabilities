# Procedure: update an installed capability

You are an LLM agent. The user wants to pull a newer version of an already-installed `<capability>` from this repo into this machine and/or project. Reason about the environment; ask before overwriting anything the user has customised.

## 0. Inputs

- The **capability** (or "all installed").
- The **scope**: machine, project, or both.

## 1. Refresh the source

Make sure you have the latest repo (git pull, or re-fetch it). This repo is the source of truth — the author changes a capability here and pushes; you are the consumer pulling it back down.

## 2. Diff installed vs. repo

For each artifact the manifest declares, compare what's installed against the repo version:

- **Global** (executable, stub, credentials *template*): these are the parts that legitimately change wholesale — a newer script, a richer stub, new env keys. Plan to replace them.
- **Project** (capability file, assets): these carry **filled placeholders and local edits** you must not clobber. Treat the repo version as the new *template* and re-apply it **preserving** the resolved values, the project's identifiers, and any recorded deviations.

## 3. Apply, preserving local truth

- Replace global artifacts; re-`chmod +x` the executable; if the credentials template gained keys, add the empty keys to the existing credentials file without touching existing values.
- For project artifacts, merge the new template structure with the existing filled values. Where the new template adds a slot or rule, apply it; where the project deliberately deviated (per the deviation log), keep the deviation.

## 4. Re-audit

Run [audit.md](audit.md) afterward so any drift the update introduced (or resolved) is surfaced. Report what changed, what you preserved, and anything the user should review.
