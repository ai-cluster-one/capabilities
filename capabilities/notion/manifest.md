# notion — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability.

## Identity

- **Name**: `notion`
- **Summary**: publish markdown to Notion pages over the REST API — identity (`whoami`), list child pages, fetch a page as markdown, replace an existing page's body + title, create a page under a parent, and upsert by exact title under a parent. The local markdown H1 is the source of truth for the page title.
- **Underlying service**: **Notion** (the SaaS), over its public REST API authenticated by an internal-integration token (PAT). Not bundled — the user supplies a Notion workspace, an integration, and the page-shares that grant it access.
- **Has authored artifacts**: no.
- **Config dependency**: `global` — resolves a token from `~/.config/notion/` (or a project `.env`); usable from any project. Project identifiers only pin page/parent UUIDs.

## Dependencies

- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. Inline script dependencies (`httpx`) are resolved by `uv` on first run.
- **A Notion integration token** (hard) — every call authenticates with a token; the CLI is inert without one. The integration must additionally be **shared into** each page it touches (Notion access is per-page, not workspace-wide). See Credentials.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/notion/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/notion` | `~/.capabilities/notion/bin/notion`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `notion` resolves by name. |
| `stub.md` | `~/.capabilities/notion/stub.md`, installed as `~/.claude/tools/notion.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |
| `credentials.env.example` | copied to `~/.config/notion/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). The CLI reads the token by **key name** — `--token-env <KEY>` selects which key holds the PAT (default `NOTION_TOKEN`); the value never touches the command line.

| Key | Secret? | Notes |
|---|---|---|
| `NOTION_TOKEN` | **yes** | the default identity's integration token (PAT); never commit a value |
| `<ALT>_TOKEN` | **yes** | optional alternate-identity PAT, named on the call via `--token-env`; one per integration/workspace the project acts as |

Per-project override: a consuming project may set `NOTION_TOKEN` (or alternate keys) in its own `.env` / `.env.local` to act as a different integration than the global default. The **name → identity** map is structural and lives in the project's `identifiers.md`; only the token values live in env.

## Project artifacts

The whole `project/` template copies into `.capabilities/<namespace>/`; the project's `build-capabilities-rule.sh` regenerates `.claude/rules/CAPABILITIES.md` with an `@`-import of the entry file, which the harness expands inline each session.

| Source (repo) | Destination |
|---|---|
| `project/CAPABILITY.md` | `.capabilities/<namespace>/CAPABILITY.md` (entry file — `@`-imported into `.claude/rules/CAPABILITIES.md`) |
| `project/identifiers.md` | `.capabilities/<namespace>/identifiers.md` |
| `project/reference.md` | `.capabilities/<namespace>/reference.md` (self-describing scaffold; populated on demand) |

## Template variables

| Variable | Class | Resolve by | Written into |
|---|---|---|---|
| `<namespace>` | discoverable | infer from the project (dir name / existing `.capabilities/` convention); confirm if unsure | the `.capabilities/<namespace>/` path |
| `NOTION_TOKEN` | must-confirm | ask the user for an integration token (create at notion.so/my-integrations, then share the target pages with it) | the credentials env file (global) or project `.env` |
| page / parent UUIDs | discoverable / leave-breadcrumb | `notion children <known parent>` once the token resolves (PATs can't search workspace-wide); fill the parents/pages this project uses, breadcrumb the rest | `project/identifiers.md` |
| `<ALT>_TOKEN` | leave-breadcrumb | only if the project publishes as more than one integration/workspace | the credentials env file / project `.env`; mapped in `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `notion`'s only must-have is **`NOTION_TOKEN`** (plus the page-shares that grant it reach) — resolved at install. Page/parent UUIDs are discovered against the live workspace by walking from a known root, so they are filled when the project actually uses them; none block install.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `notion help`).
- **No token values in markdown** — `NOTION_TOKEN` and any alternate-identity PATs live in env, not `identifiers.md`; identifiers carry only the **name → identity** map and non-secret page UUIDs, never a token.
- **Platform model lives in `notion help`** — the command surface, the cascade, the H1-as-title source-of-truth convention, the page-reference forms (UUID or URL), the markdown stdin/file/`-` contract, the I/O envelope + exit codes, and the PAT constraints (no `POST /v1/search` so no workspace-wide name lookup; `upsert` matches direct children only; access is per shared page) are all project-agnostic and are not transcribed into the assets.
- **`identifiers.md` carries placeholders** — no real page/parent UUIDs baked into the public registry; those are discovered against the consuming project's own Notion workspace at install. No secret can appear here.
- A **publishing routine** (which local docs sync to which pages, on what trigger, upsert-vs-publish policy) is a **project routine** (governed by [ROUTINES.md](../../ROUTINES.md)), not part of this capability — the assets may point to it but must not embed it.
