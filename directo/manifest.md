# directo — manifest

The declarative spec the [procedures](../procedures/) read to install / update / uninstall / audit this capability. Section set follows the manifest schema established by `windmill/manifest.md`.

## Identity

- **Name**: `directo`
- **Summary**: drive a Directo ERP instance (login.directo.ee) over the browser-session endpoints its web UI uses — login ceremony, location selection, and authed reads of the `components_api.asp` / `*.asp` surface.
- **Underlying service**: a Directo ERP database, hosted at `https://login.directo.ee/<db>/`. Not bundled — the user supplies the database and a login. No public API exists; the CLI reproduces the authenticated browser session.
- **Has authored artifacts**: no.

## Dependencies

None beyond the Directo database and a valid login.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/directo/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/directo` | `~/.capabilities/directo/bin/directo`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `directo` resolves by name. |
| `stub.md` | `~/.capabilities/directo/stub.md`, surfaced by symlinking it as `~/.claude/skills/directo/SKILL.md` — front-matter `name` + `description` load every session, body on demand. |
| `credentials.env.example` | copied to `~/.config/directo/credentials.env` **with empty values**. |

## Credentials

Env keys, resolved by the standard [4-tier cascade](../DOCTRINE.md#the-credential-cascade) (flags > project `.env(.local)` > user config > process env). Directo has no public endpoint scheme — the database segment must be named by the user; the login is the user's own.

| Key | Secret? | Notes |
|---|---|---|
| `DIRECTO_USERNAME` | no | the Directo login name (the `nimi` field) |
| `DIRECTO_PASSWORD` | **yes** | the login password (the `sala` field); never commit a value |
| `DIRECTO_DB` | no | database segment in the URL path (`login.directo.ee/<db>/`) |
| `DIRECTO_KOHT` | no | active location code — a per-session choice; `directo login` writes it back |
| `DIRECTO_COOKIE` | **yes** | the live session (`chuser` + `ASPSESSIONID*`); `directo login` writes it back, never commit |

Per-project override: a consuming project may set these in its own `.env` / `.env.local` to point at a different database / login than the global default.

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
| `DIRECTO_USERNAME` / `DIRECTO_PASSWORD` / `DIRECTO_DB` | must-confirm | ask the user | the credentials env file (global) or project `.env` |
| `DIRECTO_KOHT` | leave-breadcrumb | a per-session runtime choice; the CLI defaults it and `login` writes the active one back | the credentials env file |
| `DIRECTO_COOKIE` | leave-breadcrumb | minted by `directo login`, not set by hand | the credentials env file |

A capability is dysfunctional without its must-haves: here, `DIRECTO_USERNAME`, `DIRECTO_PASSWORD`, `DIRECTO_DB`. Resolve those at install; the location and cookie are filled at first login.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `directo help`).
- **No connection or secret values in markdown** — database, login, cookie live in env, not `identifiers.md`.
- **The `project/` template carries placeholders** — no real database, location codes, or entity taxonomy baked into the public registry; those fill in the consuming project at install.
- **The whole operating contract lives in `directo help`** — auth ceremony, session model, report architecture, the one-session-per-login caveat. It is project-agnostic, so directo carries **no slot-5 guide** (the CLI self-documents; any procedure is a routine). Its **slot-6 reference ships as a self-describing scaffold**, empty until genuine project context accrues — present so the home is labeled, not a transcription of `directo help`. Identifiers holds the project values (db handle, locations, report params).
