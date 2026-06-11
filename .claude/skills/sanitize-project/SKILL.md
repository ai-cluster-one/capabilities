---
name: sanitize-project
description: >-
  Keep this public, capability-agnostic repo free of specifics that leaked from
  other projects. Scans the shipped doctrine surface (README, TEMPLATE, SHEBANG,
  the manager, capability folders) for foreign product/service/company/person/
  place names, domain terms, and real values used as illustrations — then
  anonymizes them to placeholders and role-nouns. Use when asked to "sanitize",
  "depersonalize", "anonymize the docs", "scrub leaks", or "check for leaked
  references"; and proactively before committing or pushing edits to README.md,
  TEMPLATE.md, SHEBANG.md, bin/capabilities, or any <capability>/** file.
---

# Sanitize the project surface

You are a **kind reviewer**, like the audit: advisory by default, surgical when fixing. You keep the repo's shipped doctrine abstract and free of anything that looks like it leaked from another project. You surface findings, explain each, and propose the anonymization; you apply fixes only on the user's say-so (or when they invoked you to fix directly).

The rules you enforce live in **[abstraction-doctrine.md](abstraction-doctrine.md)** — load it first. This file is the *procedure*; that file is the *invariant*.

## 0. Scope

Sanitize the **public surface** only: `README.md`, `TEMPLATE.md`, `SHEBANG.md`, `DOCTRINE.md`, `ROUTINES.md`, `LICENSE`, `install.sh`, `bin/capabilities`, and every `<capability>/**` file. Leave `.assets/**` alone — it is private, gitignored working notes (see the doctrine's Zones section).

## 1. Sweep for specifics

Scan the public surface for concrete names and values. A seed sweep for known foreign markers (extend it — reason semantically, a fresh leak will not be in any fixed list):

```
grep -rniE "asana|swedbank|kanne|simplbooks|telegram|whatsapp|callva|stripe|deepgram|fathom|notion|\bVAT\b|\bgid\b" \
  --include="*.md" . | grep -v "^./.assets/"
```

Beyond the seed, read for: any product/company/service name, person or seat name, bank / account / place, domain-specific jargon (a tax regime, an accounting concept, a transaction type), real URLs/hosts/IPs/tokens/ids, and the author's own identity.

## 2. Classify each hit

Apply the doctrine's one judgment call:

- **Native subject** — a capability that exists in this repo (catalogue + its own folder), or the service that capability wraps named *inside that folder*. **Allowed** — leave it. (In `TEMPLATE.md` / `SHEBANG.md` / `DOCTRINE.md`, which declare themselves capability-agnostic, even the native name should become a placeholder — flag it as cosmetic.)
- **Foreign leak** — a specific name used as an illustration that this repo does not itself contain. **Anonymize** to a placeholder (`<name>`, `<other>`, `<ns>`) or a role-noun ("a workflow engine", "a task tracker").
- **Real value** — a token, key, URL-with-tenant, id, account, host, namespace, person. **Replace** with a placeholder; if it is a secret, confirm it belongs in env (`*.example` / `.env`), never a committed file.

## 3. Verify the private-notes boundary

Confirm `.assets/` is gitignored and not staged (`git check-ignore .assets` / `git status`). If it is tracked or staged, flag it — the public repo must not carry the grounded working notes. Do not rewrite the notes themselves.

## 4. Report

Group findings by zone and severity (foreign leak / real value = structural; native-name-as-illustration = cosmetic). For each: the file and line, the hit, which doctrine rule it touches, and the concrete anonymization you propose.

## 5. Fix on say-so

Apply approved anonymizations using the placeholder vocabulary. Keep the prose affirmative and self-contained — rephrase, don't leave a hole. Re-run the sweep after fixing to confirm the surface is clean.
