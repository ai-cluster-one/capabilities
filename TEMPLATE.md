# The capability template

How every capability in this repo is structured. This is the spec the install and audit procedures enforce, and the shape every capability folder mirrors. It is deliberately **abstract** — it describes slots and rules, not any one capability — so a new capability slots in without re-deciding the structure, and the validator has a fixed thing to check against.

## The mental model: two layers, hierarchical

A capability exists at two altitudes, and the split is the whole point:

- **Global (machine-level)** — declared *just enough to be visible*. The agent learns the capability exists, what it broadly does, and how to load its full contract on demand. One install per host.
- **Project (repo-level)** — *expansion*. A consuming project says how **it** uses the capability, stores **its** identifiers and mappings, and adds project-specific guides or scripts. Per consuming project.

Global is the introduction; project is the elaboration. A fact lives at exactly one altitude and is never restated at the other.

## The five slots (+ one optional)

| Slot | Lives in | Altitude | Holds |
|---|---|---|---|
| 1. Global context | the **stub** (`~/.claude/tools/<name>.md`) | global | tool awareness + how to load the full contract (`<name> help`) |
| 2. Project context | the **capability file** (`.claude/rules/capability/<NAME>.md`) | project | role/purpose **in this project** + a pointer list. Lightweight. |
| 3. Pointers | the pointer list *inside* slot 2 | project | links to slots 4–6, loaded on demand |
| 4. Identifiers | `.assets/<ns>/identifiers.md` | project | **non-secret structural** values only: paths, folders, variable names, gids |
| 5. Usage guide | `.assets/<ns>/<name>-guide.md` | project | how to use/author for this project's needs |
| (opt) Artifacts | `.assets/<ns>/scripts/` etc. | project | sources the capability authors (most have none; Windmill does) |

A sixth, `.assets/<ns>/reference.md`, carries project-specific operational *context* (prose that isn't a value and isn't a how-to). Use it when there's genuine context to hold; skip it when there isn't.

## Standing rules (the validator's invariants)

1. **Non-duplication — one fact, one home.** If a fact is stated in two places, one is wrong the moment the other changes. The audit scans for restated facts and scattered IDs and suggests consolidation.
2. **Just enough at each altitude.** Global = introduction only. Project capability file = expansion on *use*, not a re-teaching of the tool. Detail lives in the on-demand assets, not the always-loaded files.
3. **Discoverable knowledge belongs in the tool, not the docs.** If a fact *should* be answerable by running `<name> help`, put it **into the CLI's own help output** so it self-documents — don't transcribe it into a markdown that goes stale. A one-liner pointer ("run `<name> help`") replaces a copied command reference.
4. **Connection-level / secret identifiers go to env, not markdown.** URLs, tokens, workspaces, hostnames-with-tenant live in the credentials env file (global) or the project's `.env` / `.env.local` (override) — resolved by the cascade below. The identifiers asset holds only non-secret *structural* values and points to env for the rest.
5. **Link platform docs, don't copy them.** For deep usage of the underlying service, point to its official docs (fetchable live) rather than transcribing — copied docs rot silently. Capture only what's specific to *this* capability or *this* project.
6. **Placeholders, never secrets.** Project files install with clearly-marked placeholders; credential files install as `*.example` with empty values. A real secret never enters the repo or a committed file.

## Credential resolution — the cascade

Every capability's executable resolves its credentials with the **same 4-tier cascade** (first non-empty wins). This is a contract the CLI must implement, not just docs — it's how config follows the developer's expectations across a laptop and a deployed host:

1. **Flags** — explicit `--…` overrides, per invocation.
2. **Project env** — `.env.local` then `.env`, discovered by **walking up** from `$CLAUDE_PROJECT_DIR` (else cwd) to the project root (stop at the first dir holding either, or a `.git` root). The project you're working in wins.
3. **User config** — `$XDG_CONFIG_HOME/<name>/credentials.env` (default `~/.config/<name>/`). The persistent default.
4. **Process env** — exported or host-injected variables. The fallback that lets a deployed box (no config files, secrets injected by the platform) resolve correctly.

Two notes that matter: *"system-injected"* and *"ambient export"* are indistinguishable at runtime (both are just process env), so they share tier 4. And process env sits **below** the user file deliberately — files are authoritative on a dev machine; injection governs on the box only because no file is present there. A one-shot override uses a flag, not an `export`.

## Template variables

A capability declares its variables in its `manifest.md`, each classed by how it's resolved:

- **discoverable** — the procedure can find it (e.g. infer the namespace from the project dir). Resolve and fill.
- **must-confirm** — required, not safely guessable (e.g. a Windmill instance URL). The procedure **asks the user** before writing.
- **leave-breadcrumb** — can't be discovered now and isn't blocking. Write the empty env key in its proper place so a future run / first real use stumbles on it with context nearby.

A capability is **dysfunctional without its must-haves** — those are resolved (discovered or confirmed) at install; the rest become breadcrumbs.

## Deviations are allowed — and recorded

The template is a strong default, not a cage. A consuming project may deviate **with justification**. When it does, the deviation + reason is saved locally (machine- or project-scoped) so a later audit reads it as a **deliberate choice or a known edge case**, not drift to "fix." The validator advises; it does not force one path.
