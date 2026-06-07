# Procedure: package a capability (raw script → canonical)

You are an LLM agent. The user has a working shebang script and wants it formalized into a full capability in **this repo** — the authoring side of the loop, not an install. You produce the capability folder under `capabilities/<name>/`: a SHEBANG-conformant executable plus the doc slots, leak-free and audit-clean. Reason about the script; ask before guessing a name or namespace. This repo is public — **never commit a real secret**, and scrub every consumer specific.

## 0. Inputs

- The **raw script** (its path) and the **`<name>`** the capability takes — the command users will type and the folder name.
- **What it wraps**: the underlying service and how it authenticates (this shapes the credential keys and the cascade).

## 1. Seat the executable

Place the script at `capabilities/<name>/bin/<name>`. The folder is the install image — flat, copied verbatim by [INSTALL.md](../INSTALL.md).

## 2. Conform the executable to SHEBANG.md

Read [../SHEBANG.md](../SHEBANG.md) — it is the authority and holds every pattern. Bring the script to it, point by point: the PEP-723 `uv run --script` shebang with inline deps; agent-first `<name> help` as the single source of the surface (the module docstring, emitted verbatim); the four-tier credential cascade in order (flags → project `.env(.local)` → user config → process env, first non-empty wins) using the shared code shape; identity-free (no consumer value baked in, placeholders in help examples); the I/O contract (`_emit`/`_die` — JSON on stdout, error envelope on stderr); the exit-code taxonomy (0/2/3/5/6, plus 7+ for a domain outcome, each named in the help table); resilient HTTP (bounded backoff, 429 `Retry-After`, 4xx → 2/3/6); and a `doctor` — the cheapest authenticated round-trip that proves the chain. If the tool emits structured output, add a keyless command that prints its shape from the producer's own builders so it cannot drift (DOCTRINE rule 3 — e.g. `<name> contract`).

Realize the *intent* where the protocol genuinely differs, and record any deliberate departure in the capability's own **deviation file** ([../DOCTRINE.md](../DOCTRINE.md#deviations-are-allowed--and-recorded)) so the audit reads it as a choice, not drift.

## 3. Scrub for leaks — the public-repo gate

A script parked from a real machine usually carries its consumer's fingerprints. Run the [sanitize-project](../.claude/skills/sanitize-project/SKILL.md) sweep over the script **and** every file you author, and apply DOCTRINE rules 8–10: strip the consumer's name, any routine or caller that invokes it, account/tenant/host values, and terms from the *consumer's* domain; refer to the consumer by role and let specifics resolve from env. A source capability keeps only how the system is read and what its output represents — consumer-side mapping is not its to hold (rule 9).

## 4. Author the doc slots

Read [../TEMPLATE.md](../TEMPLATE.md) and fill the slots, modeling on a canonical capability (e.g. `capabilities/notion/`). Keep neutrality tiered — the declaration slots carry no specifics; values live in the asset slots.

- `manifest.md` — the declarative spec the procedures read: identity, dependencies, global + project artifacts, credentials (keys, which are secret), template variables (discoverable / must-confirm / leave-breadcrumb), and capability-specific validator notes.
- `stub.md` — the global stub: front-matter `name` + `description` (the always-loaded awareness line) plus a short body naming the executable, the credential, and "run `<name> help`". Consumer-neutral.
- `credentials.env.example` — the env keys with **empty** values and the cascade header; never a real secret.
- `project/CAPABILITY.md` — lightweight entry: a role-in-the-project paragraph + a pointer list, no re-teaching of the surface.
- `project/identifiers.md` — non-secret structural placeholders only; secrets point to env.
- `project/reference.md` — ships as the self-describing scaffold; empty is conformant.

## 5. Audit in fresh, independent context — then fix and re-audit

The author is blind to its own drift: an agent that just wrote the capability while following every guideline will still pass its own review, yet a reader given only the doctrine and the files, cold, reliably surfaces what the author glossed. So the audit is **always run by an independent reading with fresh context — never self-run**. The principle this leans on: *detection runs in fresh, independent context; the agent running this procedure decides and writes* — which is why authoring stays here and the check goes out. The host supplies the fresh context however it can (a sub-agent in Claude Code; a separate session or a second reviewer elsewhere).

1. **Smoke-test first** — `<name> help`, any shape command, and `<name> doctor` if a credential resolves. A broken executable isn't worth auditing.
2. **Hand off the audit** — give [audit.md](audit.md) to a fresh, independent context to run against the new capability. It reads cold and returns an advisory findings list; it touches no files.
3. **Apply the findings** — back here, fix what that audit surfaced. In this packaging pass you may apply them directly, no per-finding say-so: invoking this procedure *is* the standing consent to converge on conformance (the deliberate departure from audit.md's advisory default, which holds everywhere outside packaging). A finding the user calls intentional is recorded as a deviation instead of fixed.
4. **Re-audit in fresh context again** to confirm the fixes hold and nothing major was introduced or remains. Repeat 3–4 until a clean validating pass — no structural findings (cosmetics are the user's call). Use a new context each round, not the one that found the previous findings.

## 6. Land it

Add the capability to the catalogue in [../README.md](../README.md). Report what you created, which template variables are breadcrumbs the user must still fill, the audit rounds it took to converge, and any recorded deviations. The capability is now installable by [INSTALL.md](../INSTALL.md) and pullable by [update.md](update.md).
