# Procedure: conform a script into a capability folder

You are an LLM agent. You have a single runnable script and you are bringing it to the convention — a SHEBANG-conformant executable plus the doc slots, leak-free and audit-clean. This is the **mechanical authoring step**: the [package.md](package.md) funnel calls it once discovery and the suitability gate have green-lit a capability and produced a runnable script, and you can run it directly when you already have a clean shebang script in hand. Reason about the script; ask before guessing a name or namespace. A capability is **identity-free and portable** wherever it lands — never bake in a consumer's identity, and never write a real secret into a committed file.

## 0. Inputs

- The **runnable script** (its path) and the **`<name>`** the capability takes — the command users will type and the folder name.
- **What it wraps**: the underlying service and how it authenticates (this shapes the credential keys and the cascade).
- The **build location** — any folder on disk where you assemble the capability; the `<name>/` folder you produce is the flat install image that [INSTALL.md](../INSTALL.md) copies into the registry.

## 1. Seat the executable

Place the script at `<name>/bin/<name>` in your build location. That `<name>/` folder is the install image — flat, copied verbatim by [INSTALL.md](../INSTALL.md).

## 2. Conform the executable to SHEBANG.md

Read [../SHEBANG.md](../SHEBANG.md) — it is the authority and holds every pattern. Bring the script to it, point by point: the PEP-723 `uv run --script` shebang; agent-first `<name> help`; the credential cascade via the shared code shape; identity-freedom; the I/O contract; the exit-code taxonomy; resilient HTTP; a `doctor`; and, where the tool emits structured output, the keyless `contract` command (DOCTRINE rule 3). Each of these is defined in SHEBANG.md — realize it there; restate none of it here.

Realize the *intent* where the protocol genuinely differs, and record any deliberate departure in the capability's own **deviation file** ([../DOCTRINE.md](../DOCTRINE.md#deviations-are-allowed--and-recorded)) so the audit reads it as a choice, not drift.

## 3. Scrub for leaks and identity

A script parked from a real machine carries its origin's fingerprints. Run the [sanitize-project](../.claude/skills/sanitize-project/SKILL.md) sweep over the script **and** every file you author, and apply DOCTRINE rules 8–10: strip any consumer's name, the routine or caller that invokes it, account/tenant/host values, and terms from the consumer's domain; refer to the consumer by role and let specifics resolve from env. This holds wherever the capability lives — identity-freedom is what makes a capability portable, not merely what keeps a public repo clean. A source capability keeps only how the system is read and what its output represents — consumer-side mapping is not its to hold (rule 9).

## 4. Author the capability's files

Read [../TEMPLATE.md](../TEMPLATE.md) for the doc-slot shapes, then author the capability's files — modeling on any conformant capability in `capabilities/`. Keep neutrality tiered — the declaration slots carry no specifics; values live in the asset slots.

- `manifest.md` — the declarative spec the procedures read: identity (including the **config-dependency class** — `none` / `global` / `project-required`), dependencies, global + project artifacts, credentials (keys, which are secret), template variables (discoverable / must-confirm / leave-breadcrumb), and capability-specific validator notes.
- `stub.md` — the global stub: a front-matter-free awareness paragraph (the `@`-imported, always-loaded line) — what the tool is and "run `<name> help`", nothing about credentials, readiness, or usage. Consumer-neutral.
- `credentials.env.example` — the env keys with **empty** values and the cascade header; never a real secret.
- `project/CAPABILITY.md` — a **front-matter-free** lightweight entry: a role-in-the-project paragraph + a pointer list, no re-teaching of the surface.
- `project/identifiers.md` — non-secret structural placeholders only; secrets point to env.
- `project/reference.md` — ships as the self-describing scaffold; empty is conformant.

## 5. Audit in fresh, independent context — then fix and re-audit

The author is blind to its own drift: an agent that just wrote the capability while following every guideline will still pass its own review, yet a reader given only the doctrine and the files, cold, reliably surfaces what the author glossed. So the audit is **always run by an independent reading with fresh context — never self-run**. The principle this leans on: *detection runs in fresh, independent context; the agent running this procedure decides and writes* — which is why authoring stays here and the check goes out. The host supplies the fresh context however it can (a sub-agent in Claude Code; a separate session or a second reviewer elsewhere).

1. **Smoke-test first** — `<name> help`, any shape command, and `<name> doctor` if a credential resolves. A broken executable isn't worth auditing.
2. **Hand off the audit** — give [audit.md](audit.md) to a fresh, independent context to run against the new capability. It reads cold and returns an advisory findings list; it touches no files.
3. **Apply the findings** — back here, fix what that audit surfaced. In this authoring pass you may apply them directly, no per-finding say-so: invoking this procedure *is* the standing consent to converge on conformance (the deliberate departure from audit.md's advisory default, which holds everywhere outside authoring). A finding the user calls intentional is recorded as a deviation instead of fixed.
4. **Re-audit in fresh context again** to confirm the fixes hold and nothing major was introduced or remains. Repeat 3–4 until a clean validating pass — no structural findings (cosmetics are the user's call). Use a new context each round, not the one that found the previous findings.

## 6. The folder is complete

The capability folder `<name>/` now exists, conformant. Report what you created, which template variables are breadcrumbs the user must still fill, the audit rounds it took to converge, and any recorded deviations. It is now installable by [INSTALL.md](../INSTALL.md) — for a locally-authored capability, via its local-source path. Contributing it to the public catalogue is optional: a PR that adds the folder and lists it in that repo's README index. A private capability simply stays where you authored it.
