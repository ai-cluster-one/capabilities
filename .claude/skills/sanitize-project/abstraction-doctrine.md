# Abstraction doctrine

The invariant the `sanitize-project` skill enforces. This repo is a **public, capability-agnostic** doctrine and distribution. Its shipped surface reads as abstract principle — grounded in placeholders and role-nouns, illustrated by anonymized examples. A specific external product, value, person, or place that arrived from another project has no home here.

## The one judgment call: native subject vs foreign leak

A concrete name is allowed **only** when it is the subject of a capability that exists in this repo:

- **Allowed.** A capability named in the README catalogue, and the third-party service it wraps named inside that capability's own `<capability>/` folder. There, the service name *is* the subject — `windmill/` documents Windmill. That is description, not leak.
- **A leak.** Any other specific name carried in as an *illustration*: a foreign product or service, a company, a person or seat, a bank / account / place, or a domain term (a tax regime, an accounting concept, a transaction type) borrowed from a consuming or sibling project. Anonymize it to a placeholder or a role-noun.

A name that **looks like it leaked from another project** is the thing to catch — it names something this repo does not itself contain.

Even the native capability name stays out of files that declare themselves **capability-agnostic** — `TEMPLATE.md` and `procedures/**` describe slots and rules, not any one capability, so their examples use placeholders. The real name lives in the catalogue (`README.md`) and the capability's own folder.

## Values never appear

No real token, key, URL-with-tenant, workspace id, gid, account number, IP, host, tax id, namespace, or person name lands anywhere in the tree. Instance templates carry placeholders (`<namespace>`, `<AGENT_IMAGE>`); credentials ship as `*.example` with empty values. (This reinforces `TEMPLATE.md` rules 4 and 6 — secrets to env, placeholders never secrets.)

## Depersonalize

Examples speak of **a consuming project**, **the agent**, **the user** — never a named seat, person, or the author. The doctrine is grounded in principles, not in who happened to write it.

## Affirmative and self-contained

Say what a thing **is**, in the present. No defining by contrast ("unlike X", "never through Y"), no history ("formerly", "moved from"), no reaching over to explain a neighbouring system. Each file declares itself. (The audit procedure already checks this for capability instances; it holds for the doctrine surface too.)

## Placeholder vocabulary

- `<name>` — the capability in question.
- `<other>` — a sibling capability referenced by name.
- `<ns>` / `<namespace>` — the consuming project's namespace.
- **Role-nouns** for an underlying service when an example needs one: "a workflow engine", "a task tracker", "a payments system", "a self-hosted service". Never the product's real name.

## Zones

- **Public surface — abstract, and what this skill scrubs.** `README.md`, `TEMPLATE.md`, `LICENSE`, `procedures/**`, and every `<capability>/**` file. Capability files carry placeholders and name only their own subject service.
- **Private working notes — left untouched.** `.assets/**` (backlog, plans, session recaps) is scratch, deliberately grounded in the cross-project conversations that produced it. It is **gitignored** so the published tree never carries it. The skill verifies that boundary; it does not rewrite the notes, because anonymizing a record of a real conversation would destroy the record.
