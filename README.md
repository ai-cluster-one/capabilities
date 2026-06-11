# Agent Capabilities

*A convention for packaging the tools an agent installs and drives — its credentials, its project config, and its context all in known places.*

**Agent capabilities** are self-contained tools an LLM coding agent (Claude Code, Codex, …) installs to gain a new ability: drive a workflow engine, talk to a task tracker, read a mailbox, keep a set of books. This repo is two things at once — **the convention** that says how such a tool is packaged, and a **working catalogue** of tools built to it. And it cuts both ways: install a capability from the catalogue, or point the same convention at the scattered scripts you already own and turn them into one.

Hand an agent one link and a name. It reads the tool's own help, proves the tool is wired up, and starts working — no SDK, no server to run, no glue code you write by hand.

## The idea in one minute

A capability lives at two altitudes, and that split is the whole point:

- **Global** — installed once per machine. The agent learns the tool *exists* and how to load its full contract on demand. Always-on cost: a single line of awareness.
- **Project** — per consuming repo. How *this* project uses the tool: its identifiers, its config, its mappings. Loaded only when that project is open.

And the tool describes itself. Every capability ships a CLI with two guarantees an agent can lean on: `<name> help` prints the full surface, and `<name> doctor` proves the credentials resolve and the service answers — readiness checked at use-time, never assumed. The agent *discovers* the tool; it isn't taught it.

That "expose just enough, let the agent find the rest" instinct is borrowed from Claude Code skills — and generalized into a host-neutral discipline that also settles the three things a skill leaves open: a uniform executable contract, a deterministic credential model, and project-scoped configuration.

## Capabilities, then routines — where it gets powerful

A **capability** is a noun: what an agent *knows and can reach* about a system. It's already strong alone — one installed capability lets an agent drive a service end to end.

The real unlock is the **routine**: a repeatable recipe that wires one or more capabilities into a predictable procedure — reconcile the books each morning, triage the inbox, run the weekly report. Capabilities make an agent *able*; routines make it *reliable*. Capability is the noun, routine is the verb, and the magic is in the sentences you build from them. Routines live in the consuming project, beside its work — see [ROUTINES.md](ROUTINES.md).

## Why not just a skill, or an MCP server?

A fair question, and the surfaces overlap. Each fits a different job — here's the honest split:

| | **Capability** | **Skill** | **MCP server** |
|---|---|---|---|
| **What it is** | an installed CLI + its context, creds, and project config | a markdown instruction file (+ optional scripts) | a running server exposing tools over a protocol |
| **Runtime** | none — invoked, runs, exits | none — instructions | a process you start and keep alive |
| **Always-on context** | one awareness line; full surface on demand | name + description; body on demand | tool schemas loaded up front |
| **Credentials** | deterministic cascade, identity-free, laptop→prod unchanged | none defined — ad-hoc | per-server config |
| **Readiness** | proven by `doctor` at use-time | not built in | server up / down |
| **Project scoping** | per-project config + multiple profiles | global | per-server, not project-native |
| **Host portability** | host-neutral by design (Claude Code + Codex today) | Claude Code | broad cross-client *(its strength)* |
| **Best at** | credentialed, scoped tools an agent drives like a CLI | lightweight reusable instructions | rich, shared, structured integrations |

Short version: reach for a **skill** when the reusable thing is *instructions*; an **MCP server** when you need a shared, always-on, structured integration; a **capability** when you want a credentialed, project-scoped tool an agent can install and drive with almost no context cost and nothing left running.

## The catalogue — what's installable today

| Capability | What it gives you |
|---|---|
| [Asana](capabilities/asana/) | drive Asana projects & tasks over the REST API (tasks/subtasks, comments with @mentions, sections-as-status, tags, dependencies, the API-only `external` field, attachments) |
| [Windmill](capabilities/windmill/) | drive a Windmill instance (deploy scripts, cron, jobs, vars) + the SSH-dispatch script pattern |
| [Directo](capabilities/directo/) | drive a Directo ERP database over its browser-session endpoints (login ceremony, location selection, authed reads) |
| [Mail](capabilities/mail/) | read and draft mail across Mail.app's configured accounts over macOS Automation (read/search/show/links/attachments/draft/export; never sends) |
| [Mailbox](capabilities/mailbox/) | IMAP/SMTP adapter for one mailbox — list/show/fetch/flag/move and **send**; profiles in its project settings, app-password in .env |
| [Telegram](capabilities/telegram/) | drive a personal Telegram account over MTProto (a full user account) — read/search a chat, send, export a chat's full history to JSON with voice/audio + photos/stickers, and transcribe voice/audio via Deepgram; stateful login session |
| [Notion](capabilities/notion/) | publish markdown to Notion pages over the REST API (whoami, list child pages, fetch a page as markdown, replace a page's body+title, create under a parent, upsert by exact title); the local markdown H1 is the source of truth for the page title |
| [Stripe](capabilities/stripe/) | read-only Stripe fetch CLI over the REST API — a neutral JSON contract of account activity over a date range (doctor, sync-plan, contract, invoices + hosted-PDF download, balance-transactions, payouts); emits Stripe-domain facts only, every command a read |
| [SimpleBooks](capabilities/simplbooks/) | drive a SimpleBooks accounting account over its browser session (no public API) — read clients/invoices/expenses/accounts/bank-transactions/kanne, create invoices & purchase invoices, record payments/incomings, post balanced journal entries, stage bank payment orders, and process the PSD2 bank worklist; stateful login session, identity-free (account ids resolve from env) |
| [Railway](capabilities/railwayc/) | control a Railway project through the official `railway` CLI — a credential harness that injects a **project-scoped** token and forwards every command (status, logs, variables, redeploy, domain, …) straight to `railway` with I/O untouched; `doctor` proves the token and reports the project; requires the token and refuses account-wide/ambient auth, so access stays scoped to the one project + environment the token was minted for |
| [WhatsApp](capabilities/whatsapp/) | read a WhatsApp account through a self-hosted **WAHA** (WhatsApp HTTP API) bridge — onboard a session via the GOWS phone-number pairing code (no QR), doctor + instance-level health (engine/mode/account), list chats, fetch/search a chat, contact lookup, export a chat's full history to JSON with voice/audio + photos (idempotent), transcribe voice notes via Deepgram, render a markdown log; profile-indirect creds in its project settings (like mailbox) with a single-instance env fallback; reads only — sending is mode-gated and planned |
| [CallVA](capabilities/callva/) | drive the CallVA voice-AI platform over its External API (bearer key) — full CRUD across voice agents, assets (prompts), call records, transcripts, recordings, call analytics, custom fields & groups, webhook schedules, automations (Windmill scripts) and their runs, webhook logs, variables, settings, projects, phone numbers, and providers; JSON-by-default with `-f key=value` list filters and `--full` for raw responses, `--key-env` to act as another account, `doctor` proves the key + reports the visible project |

*The catalogue is actively growing — new capabilities are added as more tools and workflows get converted to the convention.*

## Grounded in a doctrine — the meta source of truth

The convention isn't vibes. Four files hold the concept, and everything in the catalogue is measured against them:

- [DOCTRINE.md](DOCTRINE.md) — the rules (one fact one home, just-enough-at-each-altitude, identity-free, …), each carrying its own *Validate* clause.
- [TEMPLATE.md](TEMPLATE.md) — what a capability is made of: the five slots, and where each kind of knowledge lives.
- [SHEBANG.md](SHEBANG.md) — what the executable is made of: the credential cascade, the I/O contract, `help`/`doctor`, the exit-code taxonomy.
- [ROUTINES.md](ROUTINES.md) — the consumer: how recipes orchestrate capabilities, and the boundary that keeps the two clean.

These are the alignment signal — the standard `capabilities audit` walks rule by rule. Want a deep dive? Read them, or point your own Claude Code at this repo and ask it to weigh the convention against whatever you use today, honestly.

## Try it

Bootstrap installs **one** thing — the `capabilities` manager:

```sh
curl -fsSL https://raw.githubusercontent.com/ai-cluster-one/capabilities/main/install.sh | sh
```

- **Install one** → `capabilities install asana` fetches the script into the registry, symlinks it onto `PATH`, snapshots its declaration, and scaffolds credentials per its declared scope. In a consuming project, `capabilities init` wires the session injection and `capabilities enable asana` makes it visible there.
- **Package your own** → the convention works on *your* tools, not just this catalogue. `capabilities new` emits the authoring procedure with your context filled in — what you have, whether it should be a capability at all, and how to build a conformant one; `capabilities conform` does the same for a script you already own. Install the result from disk: `capabilities install <name> --from <path>`.
- **Maintain** → `capabilities update` pulls newer versions, `capabilities uninstall` removes cleanly, `capabilities doctor` reconciles the machine, `capabilities audit <name>` checks a capability against the contract.

The manager is itself a shebang CLI — one file, `uv run`, no daemon — and every capability it installs is the same.

## How it's shaped

The capability **script is the sole shipped artifact** — a single-file CLI that carries its own declaration as verbs:

```
capabilities/<capability>/
  bin/<name>          the executable — domain verbs + contract verbs (stub, manifest, guide, ids, refs)
  guides/             upstream guide topics, fetched live by `<name> guide`    (optional)
  deviations.md       recorded departures from the standard                    (optional)
```

The manager installs the script into the registry (`~/.capabilities/<name>/`), symlinks it onto `PATH`, and snapshots its `stub` + `manifest --json` output. In a consuming project, `.capabilities/` holds the gate and each capability's envelope — settings, identifiers, references, state. Surfacing all of that into a session is the **manager's** job (`capabilities context`), generated per host — Claude Code and Codex are the two generated targets today. Swap the injection for another host's own and the registry plus the CLI-on-PATH route stay identical.
