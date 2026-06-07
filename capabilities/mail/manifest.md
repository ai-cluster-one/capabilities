# mail — manifest

The declarative spec the [procedures](../../procedures/) read to install / update / uninstall / audit this capability. Section set follows the manifest schema established by `windmill/manifest.md`.

## Identity

- **Name**: `mail`
- **Summary**: read and draft mail across Mail.app's already-configured accounts — list accounts/mailboxes, read, search, show a message, list its links, save attachments, fetch a linked URL to disk, draft (never send), and export a mailbox to JSON.
- **Underlying service**: the local **Mail.app** on macOS, driven over macOS Automation (JXA). Not bundled and not a remote service — it reads whatever accounts the user's Mail.app is already signed into.
- **Has authored artifacts**: no.
- **Config dependency**: `none` — authenticates via macOS Automation (a one-time per-terminal grant); no credentials, no config file. Usable from any directory.

## Dependencies

- **macOS + Mail.app** (hard) — the CLI is a JXA wrapper around Mail.app; it runs only on macOS with Mail.app configured. No-op on any other platform.
- **uv** (hard) — the executable is a `uv run --script` shebang (`#!/usr/bin/env -S uv run --script`); `uv` must be on `PATH`. No third-party Python dependencies (stdlib only).
- **macOS Automation grant** (hard, one-time) — first run prompts for permission to control Mail.app; until granted, every command fails. System Settings → Privacy & Security → Automation → the terminal → Mail.

## Global artifacts

The capability folder installs, immutable, at `~/.capabilities/mail/`; the rows below are surfaced from there (Claude Code host).

| Source (repo) | Destination (requirement) |
|---|---|
| `bin/mail` | `~/.capabilities/mail/bin/mail`, **executable** (`chmod +x`), symlinked into a `PATH` dir (`~/bin` or `~/.local/bin`) so `mail` resolves by name. |
| `stub.md` | `~/.capabilities/mail/stub.md`, installed as `~/.claude/tools/mail.md` and `@`-imported in the host `CLAUDE.md` — a front-matter-free awareness line, loaded every session. |

There is **no `credentials.env.example`** — see Credentials. The install **skips step 2d** (credentials) for this capability.

## Credentials

**None — recorded deviation from the [4-tier cascade](../../DOCTRINE.md#the-credential-cascade).** The cascade governs capabilities that hold a secret or connection value; `mail` holds neither. It authenticates by **macOS Automation**: the operating system gates access to Mail.app behind a one-time per-terminal grant, and the CLI then reads whatever accounts Mail.app is already signed into. There is no token, URL, or config file to resolve, so no `credentials.env`, no env keys, and no `--…` connection flags.

The single first-use requirement is the OS grant (System Settings → Privacy & Security → Automation → the terminal → enable Mail), which is the user's action in the OS, not a value this capability stores. This is a deliberate, justified deviation (DOCTRINE — *Deviations are allowed — and recorded*), not drift.

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
| account selector(s) | discoverable / leave-breadcrumb | which Mail.app account(s) this project acts on; `mail accounts` lists them. Fill the ones the project uses, breadcrumb the rest | `project/identifiers.md` |
| mailbox names | leave-breadcrumb | the mailboxes the project reads/exports; `mail mailboxes <account>` lists them | `project/identifiers.md` |

A capability is dysfunctional without its must-haves. `mail` has **no credential must-haves** — the only hard prerequisite is the OS Automation grant, performed once by the user. The project values (accounts, mailboxes) are filled when the project actually uses them; none block install.

## Validator notes

Capability-specific conformance the audit should check, on top of the [template invariants](../../TEMPLATE.md):

- `CAPABILITY.md` is **lightweight** — role + pointer list, not a re-teaching of the command surface (that's `mail --help`).
- **No `credentials.env.example` and no env keys** — this is the recorded no-credentials deviation, not a missing file. The Credentials section above is its single justification; the audit reads it as a deliberate choice.
- **The whole operating contract lives in `mail --help`** — the command surface, the `<account>` matching rule, the read/draft/no-send boundary, the JXA/Automation model. It is project-agnostic, so `mail` carries **no guide**; its **reference (slot 5) ships as a self-describing scaffold**, empty until genuine project context accrues.
- **`identifiers.md` carries placeholders** — no real account addresses, mailbox names, or export paths baked into the public registry; those fill in the consuming project at install. No secret can appear here because the capability has none.
