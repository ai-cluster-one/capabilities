# The shebang CLI

What a capability's **executable** is comprised of — the code shape every `bin/<name>` fills. The executable is the capability's public contract: one self-contained CLI carrying its domain verbs and the **contract verbs** that make it self-describing and self-gating. A capability may also ship a bundle of helper files beside the executable, but those helpers are reached through the CLI's declared surface, not copied into consuming projects as engine code. This file is the contract's specification — the verbs and their output shapes, the shared code patterns (the project walk, the gate, the credential cascade, connection selection, state resolution), the I/O envelope, and the exit codes. The behavioural invariants — the credential cascade, identity-freedom, discoverable knowledge, host-neutrality — and how to validate them live in the doctrine ([DOCTRINE.md](DOCTRINE.md)); this file holds the **code patterns that realize them**, distilled from the CLIs that already embody them so a new executable slots in without re-deriving the house style.

A capability's CLI is the **only** thing a consuming project runs — symlinked onto `PATH` by the manager, knowing nothing of hosts or sibling capabilities — so its surface, its contract, and its failure behaviour are the capability's public API. The patterns below are what make that API uniform across every tool an agent picks up.

## One executable, `uv run`, PEP-723

The executable is a single script with its dependencies declared inline, run by `uv` with no venv to provision and no install step. Most capabilities stop there. When a capability needs helper assets, templates, or a service engine, those ship as an installed bundle beside the executable; the executable remains the stable contract and dispatch point. Every CLI opens identically:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
# ]
# ///
```

The `-S` passes `--script` through `env`; the `# /// script` block is PEP-723 inline metadata `uv` reads to build an ephemeral environment. One file is the whole program — copyable, symlinkable, with no sibling `requirements.txt` or lockfile to keep in sync. Dependencies stay minimal: an HTTP CLI needs only `httpx`; reach for a protocol-specific client only when the protocol genuinely demands it.

## The contract verbs

Every script implements the same contract verbs alongside its domain verbs. This document is the contract specification. The code that realizes what every capability shares is a **library each capability depends on, never a copy carried in each script**: `capabilities-contract`, whose `capabilities_contract.capability` holds three tiers - the core (the contract verbs, the gate, the project walk, the I/O envelope), the store (the records adapter) and, for a capability with connections, the connections tier (the credential cascade and connection selection). A script declares the library in its PEP 723 header by a version range the manager supports (`capabilities-contract>=0.8,<1` today) and, after its own declarations, binds the tiers it carries into its own namespace, in order - `_contract_bind(globals(), "core")`, `"store"`, then `"connections"` - so the shared code reads its declarations and calls its hooks as its own code would. A change to the shared code is a release of the library, which every script whose range admits it picks up with no edit of its own - the manager brings the library in an installed capability's environment to the newest release its range admits whenever it installs or updates it; within the range the library only adds and fixes, and a change that would break a script is released outside it. Installation and `capabilities audit` refuse a script that does not bind the core and store tiers or whose range admits a version older than the first to carry the shared code or none the manager supports, and `capabilities audit` verifies conformance by calling the verbs and validating output shapes. No script imports from the manager, so a manager update can never break a deployed script.

| Verb | Returns |
|---|---|
| `help` | The full usage contract (the module docstring), verbatim. |
| `doctor` | The cheapest authenticated round-trip, per connection; proves credentials, reachability, identity. |
| `connections` | The resolution report: every connection, every value, its winning tier and source, secrets masked. Local only — no call to any system a connection names; in store mode reading the store is the resolution. `connections show <id> --reveal <KEY>` prints one of those values in the clear ([revealing one value](#revealing-one-value)). |
| `stub` | The one-line awareness text. |
| `manifest --json` | The machine-readable declaration (schema below). |
| `guide` / `guide <topic>` | The shipped guide menu with previews / one guide's body, fetched live from the docs base. |
| `ids list\|get\|set\|rm` | The project identifiers envelope, managed. |
| `refs` | The menu of the project's reference files, from front-matter. |
| `inventory` | What the capability holds in this project: headline metrics and the things inside. Local only by default — no network, no writes. |
| `connection add\|remove` | Only for a capability that declares `CONN_FIELDS`: write or remove one connection from its declared fields, its secrets from stdin (see [what a person is asked for](#what-a-person-is-asked-for)). |
| `setup` | Only for a capability that declares `SETUP_FIELDS`: report, write or remove the machine's prerequisites. |
| `settings` | Only for a capability that declares `SETTINGS`: read every settings document whole, and write one key or one document, wherever it lives. |
| `migrate` / `migrate status` | Only for a capability that declares `TABLES`: apply its pending migration steps to its tables in the project's database / report where they stand, without a lock, changing nothing (see [runtime state in the store](#runtime-state-in-the-store)). |
| `migrate files plan\|write\|verify` | Every capability, for the manager's `capabilities migrate`: move the capability's own configuration files of a project into the store through its `_migrate_files_mover`, report what would be or was written and every question, or verify it; reads no policy row. |

The declaration facts feed the contract from **constants at the top of the script** — one home each. Guide topics are the exception: their one home is the set of shipped Markdown filenames. Contract verbs render from these sources so they cannot disagree with one another:

```python
NAME = "asana"
SUMMARY = "Asana CLI over the REST API — list/read/create tasks, comment, complete."
SCOPE = "project"   # credential scope: "project" | "user"
CRED_KEYS = [
    {"key": "ASANA_TOKEN", "secret": True, "required": True, "note": "personal access token"},
]
WRITE_VERBS = {"create", "comment", "complete"}   # domain verbs that mutate the remote system
WRITE_DEFAULT = True    # a connection's allow_write when its entry is silent; False when writes leave the system
DOCS_BASE = "https://raw.githubusercontent.com/<org>/capabilities/main/capabilities/asana/guides/"
STATE = False       # True when the capability writes session/cache state
TABLES = False      # True when it keeps tables in the project's database, with a _tables() builder
INVENTORY = None    # or {"network": "none"|"optional"|"required"} with an _inventory(network) builder
POST_INSTALL = []   # [{"cmd": …, "note": …}] steps the manager offers at install
SERVICE = None      # or {"name", "summary", "verbs", ...} when a bundled service ships
CONN_FIELDS = [     # a connection entry's own fields; turns on `connection add|remove`
    {"key": "secret_env", "label": "Personal access token", "type": "string",
     "secret": True, "required": True, "env": "ASANA_{ID}_TOKEN",
     "help": "An Asana personal access token."},
]
SETUP_FIELDS = []   # the machine's prerequisites, set once; turns on `setup`
LINK_METHODS = []   # interactive ceremonies that link a connection, each with --events
SETTINGS = None     # or {"homes": [...], "keys": [...]}; turns on `settings`
```

## Agent-first help is the surface

`<name> help` prints a hand-written usage contract that is the **single source of truth** for the surface (DOCTRINE rule 3 — discoverable knowledge lives in the tool, not in docs that go stale). It is the module docstring, emitted verbatim:

```python
if args.cmd == "help":
    sys.stdout.write((__doc__ or "").lstrip("\n")); return
```

The docstring carries everything an agent needs to drive the tool without reading the source: a one-line statement of what the CLI is and that it is agent-invoked, the credential keys it resolves, every subcommand with its arguments and flags — the contract verbs included — the I/O contract, and the exit-code table. It is written *for an agent loading it on demand* — prescriptive, exhaustive, structured with visual headers — not as terse `--help` output. A stateful CLI (one with a login ceremony) opens its help with the startup protocol the agent must follow (`<name> doctor` first, what each exit code means, how to recover).

When the help text outgrows the docstring, it may live in a `HELP` constant printed the same way; the contract is identical — `<name> help` is the canonical surface, and nothing about a command is documented anywhere a `<name> help` could have answered.

## `stub` — the awareness line

`<name> stub` prints the one-line awareness text a host injects into sessions: the `SUMMARY`, closed by the discovery pointer.

```python
if args.cmd == "stub":
    _emit(f"{SUMMARY} Run `{NAME} help`."); return
```

```
Asana CLI over the REST API — list/read/create tasks, comment, complete. Run `asana help`.
```

The line is awareness, not a promise the tool is usable here — readiness is `doctor`'s question. It carries no project specifics, no front-matter, nothing the tool itself answers.

## `manifest --json` — the declaration

`<name> manifest --json` prints the machine-readable declaration, rendered verbatim from the constants (`--json` is accepted for explicitness; the output is always this JSON):

```json
{
  "name": "asana",
  "summary": "Asana CLI over the REST API — list/read/create tasks, comment, complete.",
  "credentials": {
    "scope": "project",
    "keys": [
      { "key": "ASANA_TOKEN", "secret": true, "required": true, "note": "personal access token" }
    ]
  },
  "docs": {
    "base": "https://raw.githubusercontent.com/<org>/capabilities/main/capabilities/asana/guides/",
    "topics": ["authoring", "boards"]
  },
  "state": false,
  "inventory": { "network": "optional" },
  "post_install": [],
  "tables": false,
  "connections": {
    "fields": [
      { "key": "secret_env", "label": "Personal access token", "type": "string",
        "secret": true, "required": true, "env": "ASANA_{ID}_TOKEN",
        "help": "An Asana personal access token." }
    ],
    "setup": [],
    "link": []
  },
  "settings": null,
  "service": {
    "name": "assistant",
    "summary": "Project-local assistant daemon using the bundled service engine.",
    "verbs": ["init", "doctor", "run", "start", "stop", "status", "logs"],
    "deploy": {
      "schema": "capabilities.service.deploy.v1",
      "default_policy": "auto",
      "command": ["asana", "service", "run"],
      "environment": {
        "required": ["ASANA_SERVICE_TOKEN"],
        "optional": [{"key": "ASANA_SERVICE_MODE", "default": "worker"}]
      },
      "mounts": [
        {"name": "asana_state", "kind": "state", "target": "{agent_home}/.local/state/asana"},
        {"name": "codex_state", "kind": "shared", "target": "{agent_home}/.codex"}
      ],
      "restart": "unless-stopped",
      "doctor": ["asana", "service", "doctor"]
    }
  }
}
```

| Field | Meaning |
|---|---|
| `name`, `summary` | The constants, verbatim. |
| `credentials.scope` | `project` or `user` — where the secret lives (see [the credential cascade](#the-credential-cascade)). |
| `credentials.keys[]` | Every key the cascade resolves: `key`, `secret`, `required`, `note`. Install scaffolding and `doctor`'s remediation both derive from this list. |
| `docs.base` | The upstream guides base URL; `""` when no guides ship. Overridable through the cascade as `<NAME>_DOCS_BASE`, so storage can move without touching the contract. |
| `docs.topics` | The sorted stems of the shipped `guides/*.md` files; `[]` when no guides ship. |
| `state` | `true` declares the capability writes session/cache state (see [state](#state)). |
| `inventory` | `null` when the capability reports nothing of its own; otherwise `{ "network": "none" \| "optional" \| "required" }` — how much of its report needs the remote system (see [inventory](#inventory--what-the-capability-holds-here)). |
| `post_install[]` | `{ "cmd", "note" }` steps the manager **offers** at install — idempotent, never auto-run. |
| `tables` | `true` declares the capability keeps tables in the project's database and answers `migrate`; the manager runs `<name> migrate` where the payload arrives (see [runtime state in the store](#runtime-state-in-the-store)). |
| `connections` | `null` for a capability without the connections tier; otherwise `{ "fields", "setup", "link" }` from `CONN_FIELDS` (`null` where it declares none), `SETUP_FIELDS` and `LINK_METHODS` ([what a person is asked for](#what-a-person-is-asked-for)). A capability whose connections are sessions minted on a device rather than portable values adds `"device_bound": true`, and its `connections --json` then gives each connection a `session` `{live, account}` judged on this machine without network I/O. |
| `settings` | `null` for a capability that keeps no settings; otherwise `{ "homes", "keys" }` from `SETTINGS` ([settings](#settings)). |
| `setup` *(optional)* | Present only where the capability declares `SETUP`: its `title`, `guide` and `verify`, each where declared ([`setup`](#setup)). The manager validates it during audit, source check, and install. |
| `contribution` *(optional)* | The capability's context contribution (DOCTRINE rule 2): `{ "path", "max_bytes" }`, and any other key is refused. `path` names a Markdown file relative to the capability's own folder in the project envelope and inside its `state/` folder — `state/context.md` — with no leading `/`, no backslash, and no empty, `.` or `..` segment. `max_bytes` is an integer from 1 to 32768, the most UTF-8 bytes the manager composes from that file. The capability keeps the file rendered and answers `<name> render`, which rewrites it. The manager reads that file and nothing else when it composes the project context, placing it directly after the stub line under the capability's own second-level heading, so the file opens no heading above the third level; an empty file adds nothing, and a missing, unreadable, malformed or oversized one is replaced by a single line pointing to `<name> render`. The manager validates the declaration during audit, source check, and install. |
| `programs` *(optional)* | The programs the capability needs that the operating system does not carry: a list of `{ "name", "feature" }`, `name` the executable's file name, each declared once, and `feature` one sentence on what stops working without it; any other key is refused. A capability that declares nothing needs nothing of the kind. It runs a declared program from PATH where there is one; otherwise it fetches the program itself the first time a feature needs it, never up front - as the user, without root and without a system package manager, through uv or the vendor's standalone build - into `$XDG_STATE_HOME/<name>/programs/<program>`, which an agent node keeps on a volume, and uses that copy from then on. The manager's `doctor` lists each declared program found in neither place as `program_missing`, naming the feature it stops, as information rather than drift. The manager validates it during audit, source check, and install. |
| `service` *(optional)* | Metadata for a bundled service: at minimum `name`, `summary`, and `verbs[]`. The CLI owns its lifecycle contract under `<name> service ...`. |
| `service.placement` *(optional)* | How many nodes run the service at once, for a project whose records are in the store: `exclusive`, the default, one node at a time - the deployment runner's lease holds it there, and placing it on a node takes it off every other - for a service that holds one identity (an account, a connection); or `shared`, every node it is placed on, for one safe beside itself (work claimed row by row, writes under a lock). Any other value is refused. The manager validates it during audit, source check, and install. |
| `service.sign_ins` *(optional)* | The tool sign-ins the service needs on a node it is placed on, so that node is expected to hold them (`capabilities node state`): an object of `tools`, the sorted unique names from `claude`, `codex` and `gh` it needs whatever its configuration, and `harness`, a command whose first element is the capability's own name which, run in an agent's folder, exits 0 and prints an object whose `harness` lists the harness tools (`claude`, `codex`) of the profiles the service's turns run on there, starting no turn and changing nothing. At least one of them is given. The manager validates it during audit, source check, and install. |
| `service.deploy` *(optional)* | Versioned, provider-neutral deploy descriptor. Its v1 contract declares an argv `command`, service-only `environment.required[]` and optional `{key, default?}` entries, named `state`/`shared` mounts, Compose restart policy, optional doctor argv, and `default_policy` (`auto` or `disabled`). Mount targets are absolute or start with `{agent_home}` / `{project_root}` and are resolved by the consuming runtime. Generic CLI credentials do not become service requirements unless the descriptor declares them. |
| `service.machine` *(optional)* | Declares that the service also runs once per machine for every project that opted in to it (see [machine mode](#machine-mode)). Its v1 contract (`capabilities.service.machine.v1`) names the argv `command` that runs it in the foreground and the argv `doctor` that proves it, each starting with the capability's name and carrying `--machine`; `projects`, the file under the capability's own config home where it lists the projects that opted in; and optionally `config` and `state`, the machine homes of its settings and its runtime files. Any other key is refused. A capability declaring it lists `join` and `leave` among its service verbs. The manager validates it during audit, source check, and install. |

`default_policy: "auto"` makes an explicitly project-enabled capability eligible
when the deployment runtime's `service_policy.auto_include` is true. `disabled`
requires that runtime to name the capability with an `enabled` override. Neither
value authorizes a service: explicit project capability enablement remains the
outer gate. That holds for a project whose gate is in files; a project whose records are in the store compiles every installed descriptor whatever its `default_policy`, and its service runs on a node only where the project's policy row naming that node places it (`deployment guide profiles`). The manager validates v1 descriptors during audit, source check, and install; capabilities without `service` or without `service.deploy` remain valid.

## Guides

`<name> guide` prints a JSON list derived from the shipped `guides/*.md` files;
`<name> guide <topic>` prints one guide's body. Every menu entry has exactly
`topic`, `title`, `preview`, and `command`: the filename stem supplies the topic,
the first H1 supplies the title, the first prose paragraph supplies the preview,
and the CLI constructs the read command. No front matter or separately maintained
topic list exists. A capability with guides therefore ships the full bundle.

The script is the **door** to the docs, never the authoritative storage: the
menu is computed from what the capability ships so it cannot drift, and a body
resolves **live** against the declared base so an upstream edit reaches every
consumer at once.

```json
[
  {
    "topic": "boards",
    "title": "Working with Asana boards",
    "preview": "How this capability discovers sections and moves tasks between them.",
    "command": "asana guide boards"
  }
]
```

Resolution order, per topic:

1. **Conditional GET** `{DOCS_BASE}{topic}.md` — `If-None-Match` with the cached ETag when one exists; a short timeout (10s), no retries.
2. **200** → print the body; write body + ETag to the cache. **304** → print the cache.
3. **Network failure or 5xx** → print the cache, with a one-line staleness warning on stderr. No cache → `_die(5, …)` naming the URL that failed.

The cache is the **offline floor, never the authority**: `$XDG_STATE_HOME/<name>/guides/<topic>.md` (plus `<topic>.etag`) — user-level regardless of credential scope, because guide content is capability-scoped, project-independent, and non-secret.

## `inventory` — what the capability holds here

`<name> inventory` prints what the capability amounts to in **this** project: a few headline metrics and, where they exist, the things behind them. It is the capability's own answer, so a reader assembling a whole project renders every capability through one template and holds no knowledge of any of them.

```json
{
  "capability": "<name>",
  "project": "/path/to/project",
  "network": false,
  "metrics": [
    { "label": "jobs", "value": 8 },
    { "label": "active here", "value": 6, "note": "environment: development" }
  ],
  "items": [
    { "group": "jobs", "name": "nightly-sync", "state": "enabled",
      "detail": "0 3 * * *",
      "attributes": [{ "label": "environment", "value": "production" }] }
  ],
  "service": { "state": "running", "detail": "pid 4211", "ok": true },
  "deferred": []
}
```

Every field is present on every answer, so a consumer never branches on absence. `capability` and `project` say who answered and about where (`project` is `null` outside a project). `metrics[]` are the headline figures, each `{ "label", "value", "note"? }`, rendered in the order given. `items[]` are the things inside, each `{ "name", "group"?, "state"?, "detail"?, "attributes"? }` — `group` sorts an answer that carries more than one kind of thing (scripts and schedules), `state` is one word a reader shows as a chip, `detail` is one line beside the name, and `attributes[]` are further `{ "label", "value" }` pairs. Ordered arrays rather than objects throughout: a reader that decodes JSON into a map loses the author's order, and the order is what makes the rendering legible. `service` is `null` unless the capability ships one, else `{ "state", "detail"?, "ok"?, "problem"? }` — and a capability that ships one always reports it, read from what the service left on this machine, so `capabilities inventory` can carry every declared service's state. `state` is `running` while a process serves the project, whether its own or the machine service it joined. `ok` is the verdict of the service's local checks — the part of its `service doctor` that reads this machine and nothing further: `true` when they find nothing wrong, `false` with a one-line `problem` naming what they found; a service that gives no verdict leaves `ok` out, and `capabilities inventory` reports it as `null`. A check that needs the network, the store, or a model turn stays `service doctor`'s alone, so a reader can colour every service without paying for any of them. `deferred[]` names what this run did not fetch, each `{ "label", "note"? }`.

**The read is local, fast, secret-free, and leaves nothing changed.** No network, no login, no writes — not to the project, not to state, not to a cache. A secret never appears, masked or otherwise; where credentials resolve from is `connections`' question and whether they work is `doctor`'s.

**Network-backed detail is declared and opt-in.** A capability whose figures live in the remote system declares it once, in the `INVENTORY` constant, and the manifest carries the declaration so a caller knows before it asks:

```python
INVENTORY = {"network": "optional"}   # "none" | "optional" | "required"
```

`none` — the whole report is local. `optional` — the local report stands on its own and `--network` adds to it. `required` — without `--network` there is nothing local to say. Bare, the verb never reaches the network whatever the mode; what it left behind it names in `deferred`. `<name> inventory --network` is the caller's explicit consent, and only then may the capability make the read remote. The answer always states which it was in its own `network` field.

A capability with nothing of its own to report leaves `INVENTORY = None` and answers the same envelope, empty. That absence is the contract, not a gap.

The envelope, the flag parsing and the empty answer are the core tier's; a capability that declares `INVENTORY` supplies `_inventory(network: bool) -> dict` beside its `_cmd_connections`, returning any of `metrics`, `items`, `service`, and `deferred`.

`inventory` is an **operational** verb: it reads project content rather than the capability's own declaration, so the policy gate applies to it exactly as it applies to `refs` and `ids` ([the capability policy gate](#the-capability-policy-gate)). The safe-discovery set stays `help`, `stub`, `manifest`, `connections`.

## Project records and the envelope

Every project has a visible `capabilities/` envelope. A project is bound by its durable id, `CAPABILITIES_PROJECT_ID`, in the environment or the project root's `.env.local` (resolved as described below), and no file of the envelope is needed to name it. Its slug - its short technical name, which names its state directories, its service log lines and its `--project` shorthand - is its agent's row in the store, `capabilities_agents.slug`: unique within the store, lowercase letters, digits and hyphens, written by the manager alone (`capabilities relabel`, and `init` and `migrate` where the row has none: the slug an older `project.json` names, else the agent's name transliterated, else the folder's name). Where the row holds none, the slug an older `project.json` still names stands in, else the project id; a capability reads it through `_project_slug()`, which refuses rather than answer the id where the store is configured but does not answer and no older `project.json` names one. Live configuration is kept in files, apart from the connections, grants, identifiers and policy a project in store mode keeps in its store ([connections in the store](#connections-in-the-store)), the references a project imported into its store (below) and the definitions a service runs from - its workers, its scheduled jobs - which the store keeps as versions (DOCTRINE rule 22), and the policy gate comes through the records adapter with every other live record.

All runtime readers open the same records adapter, which maps logical collections onto the established envelope and `$XDG_CONFIG_HOME` layout. Project records resolve before global records; merge collections compose by key, and a connection identity is always taken whole from one scope rather than assembled field by field. A call site asks for a collection, never for a path.

Project material has this layout:

```
capabilities/
  project.json        # an older project's slug and records mode, until it moves; never its id, and init writes none
  settings.json       # policy records (files mode)
  <name>/
    connections.json  # connection, grant, and default-setting records (files mode)
    identifiers.json  # identifier records, managed by the ids verbs (files mode)
    reference/        # one front-matter .md per reference
      *.md
    service/          # project-local service settings/context
    state/            # capability-written; never committed
```

Configuration - settings, references and state - stays in these files, apart from the references a project imported into its store, and so do the gate, identifiers, connections and grants in files mode; a service's definitions - its workers, its scheduled jobs - are versions in the store. The database holds only the tables the package designs and migrates for itself: each capability's runtime state and the definitions its service runs from ([runtime state in the store](#runtime-state-in-the-store)) and, in store mode, the contract's record tables ([connections in the store](#connections-in-the-store)), and the contract's `capabilities_references`, in any mode.

**A project resolves its database through one cascade** - the project's env files, then the process environment, then the machine's store setting file - and its levels, keys and file are [the store setting](#the-store-setting).

`capabilities/` is canonical and intentionally visible so the project's context
owner and humans share one project body. The runtime also reads a legacy
`.capabilities/` tree until `capabilities init` migrates it. Migration is
manager-owned and preflighted: disjoint gate entries and gitignore lines merge;
any other content collision stops before either tree changes. Capability scripts
never migrate.

**Where the envelope sits is resolved, not assumed.** A project bound by
`.contextkit/config.toml` has a context owner that owns its visible body, so the
runtime asks it — `contextkit path capabilities`, run in the project root,
printing the absolute envelope path — and everything derived from the envelope
follows that answer: the project identity, the manager-owned `.gitignore`, per-capability
identifiers, references, connections, and state. `$CAPABILITIES_PROJECT_ENVELOPE`
supplies the same answer in advance, so a context owner composing through
`capabilities context --fragment` is never called back mid-compose. Resolution is
filesystem-cheap and leaves the project untouched: at most one lookup per
invocation, cached per project root, and never during project-root discovery.

A project without `.contextkit/config.toml` that is bound to its id keeps the envelope at `<root>/<body_root>/capabilities`: the body root is the folder its agent's row in the store names (`capabilities_agents.body_root`), and the project root itself where the row names none or no store is configured for the project. The manager reads the row when its record (below) holds no standing answer for the root; while the store does not answer, the last answer recorded for that root and id stands whatever its age; with none recorded, an existing `<root>/capabilities` folder is the envelope; and only where that folder does not exist either does the manager refuse (`store_unreachable`, exit 5) rather than guess. Capabilities ask the manager and never read the row. Every other case — no `.contextkit/config.toml` and no id, no `contextkit` on `PATH`, a failing command, or output that is not an absolute path inside the project — keeps the envelope at `<root>/capabilities`, so a project without a context owner depends on nothing but itself.

**A resolved answer is recorded, and stands only while its inputs do.** The
manager writes one entry per absolute project root at
`$XDG_CACHE_HOME/capabilities/envelope/`, holding the resolved envelope, the size
and modification time of every input that answer consulted — the project's
`.contextkit/config.toml` (where it has none, also its `.env.local` and `.env`,
beside the id and the body root it was answered for) and the context owner's own
program — and a coarse expiry bounding what a stat cannot see. A capability CLI
and the manager both read it, re-observing each named input and comparing it whole, so an ordinary
call resolves the envelope in one small read rather than two interpreter starts.
Anything in doubt — an unreadable file, an unexpected shape, a moved input, a
passed expiry — resolves again from the context owner, so the failure mode is
slow and never wrong. `$CAPABILITIES_PROJECT_ENVELOPE` outranks the record and is
never written into it. The manager is the record's only writer and replaces
entries atomically, because capability CLIs run concurrently; the record is
reconstructible from nothing, so deleting it costs one resolution.

**The project root is found by its binding.** Walking up from `$CLAUDE_PROJECT_DIR`, else the working directory, the root is the nearest folder that is bound: the folder `$CAPABILITIES_PROJECT_ID_ROOT` names where the environment sets `$CAPABILITIES_PROJECT_ID` beside it, a folder whose `.env.local`, else `.env`, sets `CAPABILITIES_PROJECT_ID`, or a folder still carrying a binding of an older kind until it moves - an envelope's `project.json` or `settings.json`, a legacy `.capabilities/` envelope, a `.contextkit/config.toml`. Where no folder on the walk is bound, the folder the environment binds is the root, so a deployed agent is found from wherever its processes stand; where the environment binds none either, the nearest folder holding `.env`, `.env.local` or `.git` is the root of a project bound to no id. The nearest binding wins, so a hand-down from a process standing in one project never binds a descendant standing in a project of its own. A home directory is never a root, and the manager and every capability walk the same way.

**The project id is bound by the environment.** `CAPABILITIES_PROJECT_ID` is the id's one binding: the process environment's value wins where `$CAPABILITIES_PROJECT_ID_ROOT` names this root - set so by a deployment for the agent it runs, or handed down beside the envelope - and otherwise the one the project root's `.env.local`, else its `.env`, sets; an id in the environment without a root binds no folder. ContextKit is never asked for it, and a `project.json` an older project still has holds none. The env files are not committed, so each checkout of a repository is bound once, by `capabilities init` (which mints an id for a new project) or `capabilities init --id <id>` (which binds the checkout to an agent its store already holds); a checkout whose `project.json` is v2, or whose envelope holds the `.gitignore` an init wrote, and that nothing binds is refused a new id, since its id is elsewhere. The manager is the id's only writer and never rewrites it. An id kept where it was kept before - an `id` in a v1 `project.json`, or `[identity] id` in `.contextkit/config.toml` - is moved by the manager into `.env.local` the first time it reads the project, and leaves `project.json`, except under the read-only switch, where it answers the id and writes nothing; a capability that finds no id in the environment or the env files while such an older home still holds one asks the manager (`capabilities path --json --identity`) for it. The answer is in one of three states: `handed` (the environment's id for this root), `declared` (the env files') and `undeclared` (no id anywhere). A capability whose manager cannot answer refuses a write that stamps the id, such as a record a capability attributes to the project or a store registration, naming that; a project without an id is left to the write's own refusal, which names `capabilities init`. Reads never refuse over the id. An id a write may stamp is handed down beside the envelope, as `$CAPABILITIES_PROJECT_ID` scoped by `$CAPABILITIES_PROJECT_ID_ROOT` exactly as the envelope handoff is scoped, by every process that resolves the envelope, which resolves the id with it at read level, and by every process the manager launches into a project; one supplied without the scope binds no folder, and one scoped to another root is passed over.

- **Identifiers** — discoverable, non-secret, structural lookup (DOCTRINE rule 4). `identifiers.json` is a thin standard envelope — label → `{ value, note }`. Any reader renders the same menu without understanding the capability, and capability-specific structure lives inside values:

  ```json
  {
    "workspace": { "value": "1199…", "note": "workspace gid" },
    "sections":  { "value": { "Backlog": "1200…", "Doing": "1201…" }, "note": "board sections" }
  }
  ```

  Connections vs identifiers is a **provenance split**: connection entries hold values someone *chose* (wiring, per-connection behavioural keys); identifiers hold values the CLI *discovered*. Different writer, different cadence, different git-diff meaning — never merged.

- **References and service context** — prose by nature (a model, treatment, taxonomy, or prompt), exposed as versioned documents through `<name> context`. They are kept as `.md` under their established envelope paths. A reference may instead be a row of `capabilities_references`, the contract's one table of every capability's references, keyed by project, capability and name and versioned: one active version per reference, every byte of its file as the body, a save adding a version and clearing `active` on the one it replaces, a retired reference keeping its versions with none active. The manager alone imports the folder into it and exports it back byte for byte (`capabilities references`). The records adapter reads a reference's active version before any file of its key, so `refs`, `context show` and the manager's references menu answer it with no `path`, and `context put` saves it as a new version under the same base check; where the store holds none, no store is configured, or none answers, the file is read as before, and a put to a reference while the store does not answer is refused rather than written to a file the store would shadow. `<name> context edit <key>` yields an editable path and `<name> context put <key>` publishes it with an optimistic base-version check. `<name> refs` remains the compact reference menu and `<name> context show <key>` returns a body.

  ```markdown
  ---
  name: vat-regimes
  description: How VAT regimes map to income accounts on sales invoices
  ---
  ```

  `<name> refs` emits the menu — `[{ "name", "description", "path" }]` — reading front matter without loading unrelated bodies. `path` is the document path.

The `ids` verbs manage the identifier collection through the adapter:

| Verb | Does |
|---|---|
| `ids list` | Prints the resolved small, non-secret lookup, each label's `scope` naming where its value came from (`project` or `global`). Empty/absent → `{}`. |
| `ids get <label>` | Prints the value as stored. Unknown label → exit 3. |
| `ids set <label> <value> [--note <text>] [--global]` | Upsert: `<value>` parses as JSON, a non-JSON argument stores as a string; `--note` sets the annotation. |
| `ids rm <label> [--global]` | Removes the label. Unknown label → exit 3. |

Identifiers resolve label by label, the project's value over the global one. `ids set` and `ids rm` act on the project scope, creating `capabilities/<name>/` on demand inside an existing envelope; in a project with no envelope they exit 6, naming `capabilities init` as the remediation. With `--global` they act on the global scope instead: `$XDG_CONFIG_HOME/<name>/identifiers.json` in files mode, and in store mode the global row, which every project reading that store resolves beneath its own.

## The store setting

Which database a process uses is resolved per project, through one cascade shared by every tool of the family that reaches a database - the capabilities, ContextKit and AgentKit - whether or not the others are installed. Three levels are asked in order, and the first that answers is used whole, never completed from a lower level:

1. **Project** - the `AGENTKIT_DB_*` keys in the project's `.env.local` and `.env`, `.env.local` winning key by key.
2. **Process environment** - the same keys in the environment of the process. On a server this is the source.
3. **Machine** - the machine's store setting file, below. On a workstation this is usually the source.

**Keys.** `AGENTKIT_DB_URL`, `AGENTKIT_DB_HOST`, `AGENTKIT_DB_PORT` (default 5432), `AGENTKIT_DB_NAME`, `AGENTKIT_DB_USER`, `AGENTKIT_DB_PASSWORD`, `AGENTKIT_DB_SCHEMA` (default `agentkit`), `AGENTKIT_DB_SSLMODE` (default `require`), `AGENTKIT_DB_SSLROOTCERT`, `AGENTKIT_DB_TRANSACTION_HOST` and `AGENTKIT_DB_TRANSACTION_PORT`, the same in a project's env files and in the process environment. A level answers when it sets any of them. Within one level `AGENTKIT_DB_URL`, a `postgresql://` URL, wins and that level's separate fields are ignored; `AGENTKIT_DB_SCHEMA` and the transaction address still apply beside it.

**Two addresses.** A store has one address unless the level that answers also names a transaction address: `AGENTKIT_DB_TRANSACTION_HOST` and `AGENTKIT_DB_TRANSACTION_PORT`, or `transaction_host` and `transaction_port` in the machine file. It is the same store at another endpoint, typically a transaction-mode pooler, reached with that level's own database, user, password, sslmode and schema; naming only one of the two keeps the other from the address. The address is then the session address. A connection that holds session state - `LISTEN`, a session advisory lock - is a session connection to the session address; every other connection is a transaction connection to the transaction address, or to the one address when none is named, and carries no session state: no prepared statement, the schema bound for each transaction, no `LISTEN`, no session setting and no session lock. A hosted store that offers a session-mode and a transaction-mode pooler on one host takes the session pooler as its address and the transaction pooler's port as its transaction port; the shared database library refuses a known transaction pooler as the address itself.

**One resolver.** The database is PostgreSQL. Each tool of the family resolves the cascade with its own code; inside this package that code is the shared database library alone ([runtime state in the store](#runtime-state-in-the-store)), and no second copy of the order exists. A status or doctor surface reports which level answered and the file or variable each value came from, its secrets redacted. With no level answering, a tool that needs the database refuses with `store_not_configured`.

**The machine file.** `$XDG_CONFIG_HOME/agentkit/store.json` when `XDG_CONFIG_HOME` is set; `~/.config/agentkit/store.json` only when it is unset. A reader reports the absolute path it resolved, since a process started outside a shell, such as an app opened from the Finder or a launchd agent, does not see an `XDG_CONFIG_HOME` a shell profile exports.

**Format, version 1.** One JSON object, at mode 0600, the password inside it:

| Field | Required | Value |
|---|---|---|
| `schema` | yes | `"agentkit.store.v1"`, the format version |
| `host` | yes | host name, address, or the directory of a Unix socket |
| `port` | yes | integer 1-65535 |
| `database` | yes | database name |
| `user` | yes | role name |
| `password` | no | the role's password; absent when the role needs none |
| `sslmode` | yes | `require`, `verify-ca` or `verify-full`; `disable` only for a local host |
| `sslrootcert` | no | a root certificate file path, or `system` |
| `db_schema` | no | the schema inside the database that every tool binds to; `agentkit` when absent |
| `transaction_host` | no | the transaction address's host; the `host` when absent |
| `transaction_port` | no | the transaction address's port, integer 1-65535; the `port` when absent |

`host`, `database`, `user` and `transaction_host` carry no whitespace. A file naming neither `transaction_host` nor `transaction_port` names no transaction address. A version 1 file carries these fields and no others.

**Where each tool's tables live.** Every tool keeps its tables inside the schema the resolved setting names - `db_schema` in the file, `AGENTKIT_DB_SCHEMA` at the levels above it - each under its own prefix, with its own migrations and ledger, and refers to another tool's rows by value only, never by a foreign key.

**TLS.** A database reached across a network is reached over TLS: `sslmode` is `require` or stronger, at every level and in a URL, and for the transaction address as for the address. `disable` is allowed only when the host, and the transaction host, is a local host — `localhost`, a loopback address, or a Unix socket directory — and `allow` and `prefer` never.

**Versions.** A reader refuses a version it does not know with a clear error rather than guessing at it. A writer never replaces or removes a file of a version it does not know, and writes the lowest version that expresses what it holds.

**Writes.** Any tool of the family may write the machine file. A write replaces the whole file atomically — a temporary file in the same directory, renamed over it — at mode 0600. In this package the manager is the writer: `capabilities store set` and `capabilities store unset`, taking the password from stdin, a file or a named environment variable and never from argv; `capabilities store show` reports the setting and the path without its secret, and `capabilities store doctor` proves it. A project's env files and a server's environment are written by whoever configures that project or server, never by a tool.

A capability resolves the setting, and reaches its runtime state, through the shared database library ([runtime state in the store](#runtime-state-in-the-store)).

## The capability policy gate

Operational verbs and `doctor` pass an effective two-scope policy gate before dispatch. Safe discovery verbs — `help`, `stub`, `manifest`, and the local-only `connections` report — remain available so a disabled capability can be understood and configured. `connections show`, which prints a value in the clear, is not one of them: it passes the gate as an operational verb, and a request's verb list passes it only where it names `connections`. The manager owns the policy collection, kept in files mode at:

1. Project: `capabilities/settings.json`.
2. Global machine default: `$XDG_CONFIG_HOME/capabilities/settings.json`.

In store mode it is kept in the store instead ([connections in the store](#connections-in-the-store)): the project's rows and the global rows that every project reading that store shares, a row naming the running node read before the row for every node at each scope.

An explicit project entry wins. An absent project entry inherits the global entry. Absence at both scopes is disabled by default. Policy mutations require explicit `--project` or `--global`; the agent asks the user which scope they mean rather than guessing.

Above both scopes sits the machine ceiling, `$XDG_CONFIG_HOME/capabilities/machine.json`, which only the manager writes and which is never kept in a store, because it answers for this machine alone. Each installed capability is `allowed` or `quarantined` there; a capability without an entry is allowed. Quarantined is effective-disabled in every project and outside any, whatever the project or global entry says: `_gate()` refuses its operational verbs and `doctor` with exit 4 (`quarantined`) before it resolves a project or a policy row, [machine administration](#machine-administration) included, while safe discovery stays available. Allowed leaves the two scopes deciding exactly as below. `capabilities install` writes `quarantined` for a capability new to the machine and `allowed` when given `--allow`; reinstalling or updating keeps the state, and uninstalling drops it. `capabilities allow` and `capabilities quarantine` change it, and lifting a quarantine is the user's decision, never an agent's.

```python
def _project_root() -> Path | None:
    """The nearest bound folder walking up from $CLAUDE_PROJECT_DIR (else cwd):
    the one $CAPABILITIES_PROJECT_ID_ROOT names beside $CAPABILITIES_PROJECT_ID,
    one whose .env.local or .env sets the id, or one with an older binding.
    Else the folder the environment binds, else the nearest folder holding
    .env(.local) or .git. $HOME is never a project root (the machine registry
    lives there)."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    home = Path.home().resolve()
    bound = _env_bound_root()  # the id's root, where the environment sets both
    unbound = None
    for d in (here, *here.parents):
        if d == home:
            break
        if (d == bound or _env_file_project_id(d)
                or (d / "capabilities" / "project.json").is_file()
                or (d / "capabilities" / "settings.json").is_file()
                or (d / ".contextkit" / "config.toml").is_file()
                or (d / ".capabilities").is_dir()):
            return d
        if unbound is None and ((d / ".env").exists() or (d / ".env.local").exists()
                                or (d / ".git").is_dir()):
            unbound = d
    return bound if bound is not None and bound != home else unbound

def _project_capabilities_dir(root: Path) -> Path:
    current = root / "capabilities"
    legacy = root / ".capabilities"
    if ((current / "project.json").is_file()
            or (current / "settings.json").is_file() or not legacy.is_dir()):
        return current
    return legacy

def _gate() -> None:
    _auth_gate()
    if sys.argv[1] in {"help", "stub", "manifest", "connections"}:
        return
    row = _records().resolve("capabilities", "policy").get(NAME)
    effective = row.get("value") if row else None
    if isinstance(effective, dict) and effective.get("enabled") is True:
        return
    _die(4, "not_enabled", f"{NAME} is not enabled by effective policy",
         "ask whether to enable it --project or --global")
```

| Project entry | Global entry | Operational result |
|---|---|---|
| `true` | any | enabled by project |
| `false` | any | disabled by project |
| absent | `true` | enabled by inheritance |
| absent | `false` or absent | disabled |

The gate is a **guardrail, not a security boundary** — project resolution remains cwd-based. A malformed policy is a configuration error and operational absence is closed. The exit-4 envelope routes the scope decision to the human. Bundled service `init`, `start`, `run`, and `join` additionally require explicit project enable: inherited global availability grants CLI use, not ownership of a project daemon or consent to a machine one. A service that declares `service.machine` takes `--machine` on its service verbs, and such a call resolves no project and reads no policy row: the machine service acts for a project only while that project explicitly enables the capability and is on the service's opt-in list, and checks both itself before every action it takes for that project ([machine mode](#machine-mode)). `migrate` of a capability that declares `TABLES` likewise resolves no project and reads no policy row, because it acts on the machine's store rather than for a project and the manager runs it outside any project; it still answers to the machine ceiling, and `migrate`, not `migrate status`, to the read-only switch. The read-only switch refuses its `init`, `start`, `run`, `reload`, and `leave` as it refuses a project's. This rule lives once in the core tier: `_gate()` applies it only when the executable declares a `SERVICE` manifest surface, so each service implements its lifecycle without repeating policy logic and domain commands merely named `service` remain ordinary CLI verbs.

### Machine administration

A call that administers the machine rather than acting for a project reads no policy row, so neither a project's policy nor the global policy decides it; it answers to the machine ceiling, and its writes to the read-only switch. It is the machine-service call and `migrate` above, and three more. Outside any project, a [machine read](#machine-reads) - a verb in `MACHINE_READS` or one a `LINK_METHODS` ceremony runs - and [`setup`](#setup) reporting the machine's prerequisites, because no project's policy could govern them and the machine's connections are lent to them only read-only. Wherever it runs, a write to the machine's own configuration that says so: `setup set|unset --machine` and [`connection add|remove --machine`](#connection-addremove). A write is machine administration only when it carries `--machine`, never by inference from the working directory or `CLAUDE_PROJECT_DIR`; without it `setup set|unset` and `connection add|remove --global` pass the gate as every operational verb does. Inside a project a read is never machine administration. `_gate()` decides it once, in `_machine_administration()`, and only for the verbs the contract itself answers, so a domain verb of the same name stays under the gate.

## Bundled services

A capability that ships a service declares it in the manifest (`SERVICE`) and owns its lifecycle under `<name> service ...`. Which verbs it offers is the capability's business, with one exception fixed by doctrine: a service that takes a declaration ships `reload`.

The daemon holds its declaration in memory and read the file once, so the file and the process part company the moment either moves. Three mechanics keep that honest.

**The running process publishes what it loaded.** Beside its pid file and behind the same lock, the daemon writes a fingerprint of the declaration it adopted and removes it on exit. `doctor` compares that against the declaration on disk and fails while the two differ, so *the process is running* and *the process is current* stay separate answers.

**Reload is signalled, never restarted.** `<name> service reload` loads and validates the declaration first: asking a healthy daemon to adopt a file that does not parse is refused before it is asked, naming the parse error. It then sends `SIGHUP` to the daemon alone and never to its process group, because the group is exactly where in-flight work lives. The daemon takes the request at a point in its loop where nothing is being decided, re-reads, validates again, and swaps only on success; a rejection leaves the previous declaration scheduling and is reported on the service log. The published fingerprint moves last, and only on success, so nothing can report itself current while running something else.

**The caller waits for evidence.** `reload` returns when the published fingerprint reaches the one it computed and times out otherwise, so a success is demonstrated rather than assumed and a silent rejection cannot read as one. A daemon already holding the declaration is not signalled at all.

This is also what lets a service be corrected from inside itself. A job cannot restart the program it runs under - stopping a program stops the process group the job is in, so it dies before the restart lands - but it can reload it, because a reload is the one operation that deliberately leaves the group alone.

What a reload cannot change belongs in the verb's own help rather than in the caller's memory: an identity the process authenticated as, a state root, a records backend. Those need a restart, and saying which is part of shipping the verb.

### Machine mode

A service that declares `service.machine` runs in one of two modes from one implementation. In project mode it serves the project it was started in, as every bundled service does. In machine mode one process on the machine serves every project that opted in to it. Project mode is the case of a set of one: dispatch, its checks and the start of work are the same code in both.

Opting in is explicit and apart from enable. `<name> service join`, run in a project that explicitly enables the capability, adds the project to the service's opt-in list, the file its `service.machine.projects` names; `<name> service leave` removes it. The service alone writes its list, keyed by project id, each entry naming the project's root, slug, and when and by whom it joined. The service notices a project joining, leaving, or whose folder is gone, and reports such a project rather than failing.

Exactly one process serves a project at a time. A lock per project, the same lock in both modes, decides which. A joined project's project-mode `start` and `run` are refused, and a project another process still holds is reported as refused until it is free.

The machine process decides when work starts and nothing about what the work is. It holds what is the machine's - its store connections, held rather than opened per question; an optional machine-wide cap on concurrent work; and the order in which projects are served under that cap - and it loads no project's environment or secrets, so a secret it needs for its own connections resolves from the machine's tiers. The work it starts is the project's: a child process whose working directory is the project, with the project's own environment, connections, grants, and declarations, as project mode starts it. Before every action it takes for a project it checks again that the project is on the list, explicitly enables the capability, and grants the connection the work needs.

The blast radius is one project. Each project's declaration loads and reloads on its own, so a declaration that does not load stops that project only; a project can be paused alone; every log line and every status entry names the project it is about; and the machine status answers for each joined project as `served`, `paused`, `refused`, or `error`, with the reason.

## The credential cascade

Every executable resolves the values named by an explicit connection through the same deterministic cascade, first non-empty wins, so secrets can move from laptop files to deployment environment without changing the declared identity:

1. **Flags** — explicit `--…` overrides, per invocation, for non-secret values only. **A secret never has a flag**: `argv` leaks (process lists, shell history), so a secret resolves from the tiers below, and a one-shot secret override is an env-prefix invocation — `ASANA_TOKEN=… asana …` — which is tier 4 working as designed.
2. **Project env** — `.env.local` then `.env` at the project root (`_project_root()` above). The project you're working in wins.
3. **User config** — `$XDG_CONFIG_HOME/<name>/credentials.env` (default `~/.config/<name>/`). The persistent per-machine default. For a key a [setup field](#what-a-person-is-asked-for) declares `shared`, the machine's one shared file `$XDG_CONFIG_HOME/capabilities/shared.env` is asked first, under the field's shared name, and the capability's own `credentials.env` after it, so one value serves every capability that declares it while a value kept the older way still resolves.
4. **Process env** — exported or host-injected variables. The fallback that lets a deployed box, where no config file is present, resolve correctly.
5. **The agent's vault** — the agent's own secrets in its store, asked last: each a row of `capabilities_vault`, sealed with XChaCha20-Poly1305 under the agent's one encryption key, `AGENT_ENCRYPTION_KEY`, the agent id and the name bound to it. The key lives in the agent's environment - the project's `.env.local` or `.env`, the machine's `$XDG_CONFIG_HOME/capabilities/shared.env`, or the process environment - and never in the store, which keeps only its fingerprint on the agent's row. The vault is read in the same store round trip that reads the agent's connections, only the names asked for are opened, and what is opened is kept in the process's memory and never written to disk; a long-running process rereads it when the vault's channel version moves, so a changed secret reaches it within about a second. A store that cannot be read leaves the tier unanswered and says the vault could not be read; a row sealed under another key is unreadable. The manager's `key` and `secrets` verbs keep the key and the vault, and the shared library (`capabilities_contract.vault`) holds the only copy of the sealing and the reading.

Process env sits **below** the user file deliberately: files are authoritative on a dev machine; injection governs on the box only because no file is there. A one-shot override is a flag (or an env-prefix), not an `export`. *System-injected* and *ambient export* are indistinguishable at runtime — both are just process env — so they share tier 4.

This is the code shape that realizes the cascade, shared verbatim across executables so the resolution is identical everywhere:

```python
def _parse_env_file(path: Path) -> dict:
    """Parse a KEY=VALUE env/dotenv file. Missing/unreadable file -> {}."""
    out: dict = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        out[k] = v.strip().strip('"').strip("'")
    return out

def _project_env() -> dict:
    """Project .env(.local) at the project root. .env.local overrides .env."""
    root = _project_root()
    if root is None:
        return {}
    merged = _parse_env_file(root / ".env")
    merged.update(_parse_env_file(root / ".env.local"))  # .local wins
    return merged
```

The user-config path is rooted at `$XDG_CONFIG_HOME` (default `~/.config/`), never a bare `~/.config`:

```python
_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CREDENTIALS_ENV = _CONFIG_HOME / "<name>" / "credentials.env"
```

Resolution is a single `pick` per value — first non-empty wins, in cascade order:

```python
def pick(flag, env_key):
    # flag > project .env(.local) > user config > process env
    return (getattr(args, flag, None)
            or project.get(env_key)
            or user.get(env_key)
            or os.environ.get(env_key))
```

A missing required value fails through `_die` with the exact remediation for both ends of the cascade — what to set on a laptop, what to inject on a box — never a bare "not found".

**Credential scope** is declared in the manifest (`SCOPE`). `project` — the default — homes the secret in the project's `.env`/`.env.local`, nothing global: the shape for service- and tenant-scoped tools. `user` homes it in `~/.config/<name>/credentials.env`: reserved for personal-account tools where one human has one identity. Scope names where install scaffolds and where `doctor` points its remediation; the cascade itself consults every tier, identically, regardless of scope.

`credentials.env` is **human-written only**: a script never writes back into it of its own accord. What it holds comes from a person, by hand or through the contract's `connection add` and `setup set`, which take a secret on stdin at that person's request ([what a person is asked for](#what-a-person-is-asked-for)). An artifact a login ceremony mints — a session cookie, an exchanged token — is state and lands in the state directory ([below](#state)). One writer per file, everywhere.

A capability whose secret is not a flat token resolves it in its own shape — keyed indirectly through a config file, or persisted after a login exchange — but the **tiers and their order are preserved**: an explicit override beats project config beats user config beats process env, resolved by deterministic code. The shape may vary; the order may not.

## Connections

Every connection-bearing capability resolves its configuration as **explicit named connections**. A registry is required even when there is only one connection; environment variables resolve values for that declared connection but never create an identity implicitly. Core-only capabilities omit the connections tier and report an empty connections map.

Endpoints and identities are declared as **connection records**, written at configuration time by whoever configures and read through the shared adapter. The two scopes are project then global. They compose by connection id: the highest scope declaring an id supplies that entire identity, while other ids inherit from the lower scope. A separate grant record carries `enabled` and `allow_write`, so project permission can change without restating identity. An identity the project declares is the project's to use; one inherited from the global scope is withheld until a grant resolving at project scope says `enabled: true` - grant before use. In files mode `connections.json` stores identities and grants together.

```json
{
  "default": "billing",
  "connections": {
    "billing": { "address": "billing@example.com", "imap_host": "mail.example.com",
                 "secret_env": "MAILBOX_BILLING_APP_PASSWORD" },
    "intake":  { "address": "intake@example.com",  "imap_host": "mail.example.com",
                 "secret_env": "MAILBOX_INTAKE_APP_PASSWORD", "allow_write": false }
  }
}
```

The envelope is the standard's; the entry **interior** is the capability's (hosts and ports for an IMAP tool; url and workspace for a REST tool). Three field names are reserved in every entry:

- **`secret_env`** — a secret never sits in the registry. The entry names the env key holding it, and that key's *value* resolves through cascade tiers 2–5 exactly as any secret does — the registry namespaces the key, the cascade resolves it. A capability with several secrets names each through its own `*_env` field.
- **`allow_write`** — the connection's write gate ([below](#the-write-gate)), represented logically as a grant. Absent falls to the capability's declared `WRITE_DEFAULT`.
- **`identity`** — who the connection is: `{"id": <text>, "display_name": <text or null>}`, the account it is signed in as. The contract's connections tier is its one writer: a capability's login and doctor record it on success through `_record_identity(<id>, <account id>, <display name>)`, which writes the connection's own record at the scope that holds it, only where it changes, and never fails the verb - under the read-only switch or where the records refuse the write it writes nothing - and a person sets it with [`connection add`](#connection-addremove). Absent until either has.

Non-secret values sit literally in the entry: chosen, committed, per-connection project config — both the wiring (endpoints, workspaces) and any behavioural per-connection keys the capability defines.

### Connections in the store

A project keeps its connections, grants, identifiers and policy in one of two homes: the files above, in files mode, or the project's store, in store mode, which serves a project whose agents run on more than one node. One mode owns a project at a time and the two never compose with each other. Either is read through the same records adapter, so `_connections_registry()` below, the gate, the `ids` verbs and every verb above them behave the same in both. A project an older `project.json` still describes is in store mode when it says `"records": "store"`, and in files mode otherwise. A project without one is in files mode where it keeps a `settings.json` gate or no database resolves for it, and otherwise - bound to its id, with a store to keep them in - in store mode: the records of a bound agent are the store's. An agent whose row in `capabilities_agents` has `migrated_at` set is the one exception: wherever a database resolves for it, every record and every project or global settings document is read from and written to the store's rows, its own over the rows for every agent and, at each, the row for its node over the row for every node, whatever its `project.json` says and with its files unread. It is read only on a node registered among its nodes and named by `AGENT_NODE_NAME`, and refused elsewhere naming its master; the master, and every other node whose link lets it write (`may_write`), writes the rows every node shares and the rows naming itself, a row naming another node is the master's alone, and while `migrating_since` is set only the migration on the master writes. A `project.json` saying `"connections": "store"` keeps only its connections and grants in the store, and its identifiers and policy in files. The manager alone writes these keys, through `capabilities connections import`, which writes the rows the project's files resolve to, proves the store resolves the same records, and only then records `"records": "store"`; it never overwrites a row that differs, and run again it changes nothing. The machine ceiling and the harness deny rule stay in files in either mode, and so do the `slug` and `schema` an older `project.json` holds.

Store mode keeps four tables, owned by the contract and migrated under its own ledger in the schema the project's [store setting](#the-store-setting) names:

| Table | Columns |
|---|---|
| `capabilities_connections` | `capability`, `name`, `spec` (jsonb), `owner` (nullable); one row per connection, keyed by `capability` and `name` |
| `capabilities_connection_grants` | `agent_id`, `capability`, `connection`, `node` (nullable), `enabled`, `allow_write`, `is_default` |
| `capabilities_identifiers` | `agent_id` (nullable), `capability`, `label`, `value` (jsonb), `note`, `updated_at`; one row per agent (or none), capability and label |
| `capabilities_policy` | `agent_id` (nullable), `capability`, `node` (nullable), `enabled`, `updated_at`, `service`; one row per agent (or none), capability and node (or none) |
| `capabilities_vault` | `agent_id`, `name`, `ciphertext` (bytea), `key_fingerprint`, `updated_at`, `updated_by`; one row per agent and secret name, the value sealed with the agent's key, which the store never holds; the key's fingerprint is `capabilities_agents.vault_key` |

`spec` is the entry interior, the same object a `connections.json` entry holds, and carries secret references only: each secret stays named by its `*_env` field and its value never enters the connection's row. The value resolves on each node through that node's own cascade tiers 2–5, so one row serves every node while each node keeps its own values, and the agent's vault (`capabilities_vault`, [the credential cascade](#the-credential-cascade)) serves every node that holds the agent's key.

`agent_id` is the project id, the `prj_<12hex>` value the project's identity carries, held as text and never as a foreign key, because the identity's home is outside these tables; `connection` refers to a connection row of the same capability inside them. `node` names the node a grant is for and is null for every node; a grant naming the running node is used before the grant for every node, field by field as grants resolve, and the running node is the id the store tier's `node_id()` returns (`node_name()` returns the same): twelve lowercase hex characters the machine resolves for itself with nothing configured - a Mac derives it from its hardware UUID through a hash, so the UUID never leaves it and nothing is stored, and any other machine reads the one kept in `$XDG_CONFIG_HOME/capabilities/node-id`, minted there once, which a container keeps on its persistent volume across redeploys while its host name, the container id, changes. `AGENT_NODE_ID` overrides it where set (a container handed its id, a test), taking an id or the name a node registered before ids is still keyed by, and any other value is refused (`bad_node_id`); a machine that resolves none is refused (`node_unidentified`), never named by its host name. A node is a row of the manager's node registry, `capabilities_nodes`, keyed by that id and shown by its label, so every row that names a node holds its id and renaming a node changes its label alone; `AGENT_NODE_NAME` is only the label a machine gives itself when it is first registered. `enabled` or `allow_write` left null decides nothing, as a field absent from a file's grant does, so `allow_write` still falls to `WRITE_DEFAULT`. `is_default` is the default pointer, held by at most one grant per agent, capability and node.

Every rule of [connections](#connections) holds unchanged. A connection row is used whole, never assembled from two rows. A row is withheld from a project until that project's grant enables it, as an identity inherited from the global scope is in files mode, and every row has an owner. `owner` null is a connection **shared** by the capability, which any agent may be granted; `owner` naming an agent's id is that **agent's own** connection - its own account, such as its own messaging or chat account - which the store refuses to grant to any other agent or to every agent, and which only that agent changes or removes. `<name> connection add` at project scope in a project in store mode writes a new connection as the project's own and its grant enabling it, in one transaction; a connection that already exists keeps its owner. One written at the machine's scope is shared. A grant is per agent, so the same shared connection is read-write for one agent and read-only for another through each grant's `allow_write`. `capabilities connections list` answers, per capability, every connection with its owner and every grant on it. A store whose rows predate owners gives each one an owner once, when its tables are brought to the version that adds it: a connection whose grants name exactly one agent and none is for every agent becomes that agent's own where it is named for it - the first word of the agent's registered name is a word of the connection's name or of its spec's display `name` - and every other row is shared. `allow_write` resolves through [the write gate](#the-write-gate), the read-only switch closes every gate, and a grant that resolves to `enabled: false` leaves the connection out of the set. `<name> connections` reads the store to report a resolution and reaches nothing a connection names. A store that cannot be reached refuses with `store_unreachable` and exit 5, never as `connection_not_granted`.

An identifier or a policy row whose `agent_id` is null is global: it applies to every project that reads the store, on every machine, and a project's own row with the same label or capability is read before it. Identifiers carry no versions: `ids set` overwrites a row and `ids rm` deletes it. A policy row's `node` names the node it is for and is null for every node, the running node being `node_name()` as for grants, so the gate resolves from the first of the project's row for this node, its row for every node, the global row for this node and the global row for every node, and is denied where none exists; the scope that answered is the scope [the gate](#the-capability-policy-gate) acts on, so service activation still takes a project row. A policy record says which node the row that answered names (`node`, null for every node) and whether that row places the capability's bundled service there (`service`). The two answers of a row are apart: `enabled` decides whether the capability is usable on that node, and `service`, which only the project's row naming a node can carry, whether its service is placed there, so a service moves between nodes without changing where the capability is usable. The manager writes policy rows for every node through `enable`, `disable` and `inherit`, and for one node with `--node <node>`, which files mode refuses as `node_requires_store` because files carry no node; `enable --node` and `disable --node` set both answers alike, and `place` and `unplace` set `service` alone, an exclusive service (`service.placement`) taken off every other node as it is placed; a write that stamps the project id takes it only once it is verified. With the gate in the store every gated call reads the store, and a store that cannot be reached refuses it with `store_unreachable` and exit 5.

Once the store holds the rows every agent inherits - `capabilities_global.global_migrated_at` is set, which `capabilities migrate --global` does - those rows are the global scope for every process that reads that store, outside any project and in a project in files mode alike: every shared connection row (a row an agent owns is that agent's own and is not part of it), the grant for every agent and every node (`agent_id` and `node` null) with the default pointer it carries, and the global policy rows. A config-home file is then read only for a connection id, or a capability's policy, the store holds no row of, and a file's default pointer only for a capability the store holds no connection of. No connection, grant or default pointer is written to a file at either scope: a global write (`--global`, `--machine`, from an agent or from a capability page) is a row of the store, a connection written there is shared, and removing one while an agent's grant enables it is refused with `connection_granted`; a project in files mode is refused a write of its own connection, grant or default pointer with `connections_in_store` until it moves into the store; and a configured store that does not answer refuses a write with exit 5 rather than leaving it in a file, while a read falls back to the files. `capabilities enable`, `disable` and `inherit` with `--global` outside any project write the global policy row the same way. A secret is still written to a file - the project's `.env.local`, or the capability's credentials file - and the row names its env key. Where no database resolves, or the global rows are not there yet, the config home's files are the global scope as before. `capabilities migrate --retire-connections` moves the config home's connections files to a dated backup once every connection they declare is a row of the store with the same fields.

### Selection

One flag selects, accepted by every domain verb: `--connection <id>`. A capability may accept a native alias beside it (`--mailbox <id|address>`); the standard flag always works. Resolution is deterministic, refusing ambiguity rather than guessing:

```python
def _connections_registry() -> dict:
    """The effective identities and grants, independent of their backend."""
    registry = _records().connections(NAME, write_default=WRITE_DEFAULT)
    if not registry.get("connections"):
        _die(6, "connections_required", f"{NAME} requires an explicit connection")
    return registry

def _select_connection(reg: dict | None, wanted: str | None) -> tuple[str, dict | None]:
    """flag → default pointer → sole entry → die 6."""
    if reg is None:
        _die(6, "connections_required", f"{NAME} requires an explicit registry")
    conns = reg["connections"]
    if wanted:
        if wanted in conns:
            return wanted, conns[wanted]
        _die(6, "unknown_connection", f"no connection matches {wanted!r}",
             f"known: {', '.join(conns)}")
    default = reg.get("default")
    if default:
        if default not in conns:
            _die(6, "bad_default", f"default points to unknown connection {default!r}",
                 f"known: {', '.join(conns)}")
        return default, conns[default]
    if len(conns) == 1:
        cid = next(iter(conns))
        return cid, conns[cid]
    _die(6, "ambiguous_connection",
         f"registry defines {len(conns)} connections and no default; pass --connection <id>",
         f"known: {', '.join(conns)}")
```

`default` is a **pointer, not an entry**: connection ids are stable identities — state keys, log lines, `--connection` arguments — and which one is default is policy that moves without renaming anything. The pointer makes two defaults structurally impossible.

### The write gate

A connection without `"allow_write": true` resolves its writability from two declarations: the entry's own `allow_write`, else the capability's **`WRITE_DEFAULT`**. A connection that resolves to `false` is a **source**: read verbs run, write verbs exit 4. The script declares which of its domain verbs mutate the remote system in one constant — `WRITE_VERBS` — and refuses at dispatch, after selection, before any credential resolves:

```python
def _write_gate(conn_id: str, conn: dict | None, verb: str) -> None:
    """Policy from the committed registry; nothing in the cascade lifts it."""
    if verb in WRITE_VERBS and not (conn or {}).get("allow_write", WRITE_DEFAULT):
        _die(4, "read_only",
             f"connection {conn_id!r} does not allow writes",
             "Do not lift the gate yourself — ask the user; granting is "
             "`allow_write: true` on this connection in connections.json.")
```

`WRITE_DEFAULT` is the capability's word on what silence means, declared once in the script: `True` for tools whose writes stay inside a system the consumer owns; **`False` when a write leaves the system**. Under `WRITE_DEFAULT = False`, writing is granted per connection, deliberately, in a registry entry. The refused write is the ceremony: exit 4 at the moment of intent routes the grant decision to the human with the exact remediation in hand.

The principle: **the cascade resolves values; a gate is not a value.** No flag, no env var, no tier overrides the gate — it reads from the two declarations alone, and the exit-4 envelope routes the decision to the human, exactly as the project gate does. Exit 4 is the policy-refusal code in both.

**The read-only switch closes every gate at once and opens none.** `CAPABILITIES_READ_ONLY` set to `1` or `true` (any case) in a capability's environment puts it, and every process it starts, under the switch; unset, `0` or `false` leave everything as declared. Under it every connection resolves read-only whatever its grant or `WRITE_DEFAULT`, and every write verb exits 4 with the code `read_only_switch`, distinct from a connection's own `read_only`. The same refusal covers a write to a project record — identifiers, references, connections, grants, settings, policy — whether a capability or the manager would make it, and a bundled service's `init`, `start`, `run` and `reload`. A capability's operational state and caches keep writing, so a read that refreshes a local store still succeeds. The contract decides the switch once, in the store tier (`read_only_switch()`), because that tier is the one every capability carries and the manager and bundled services import; every writer consults it and none reimplements it. `<name> connections` and `capabilities doctor` report it while it is on.

### Machine reads

Outside any project there is no scope a grant could be written in, so a capability may declare, beside `WRITE_DEFAULT`, the read verbs that there use the machine's own connections: `MACHINE_READS = ("connections", "doctor")`, a tuple of verbs the capability exposes, none of them a write verb, which the manager validates wherever it validates a capability. Absent or empty, the capability has no machine reads. Outside any project a declared verb sees every connection declared globally whose grant does not resolve to `enabled: false`, each one a source whatever its grant or `WRITE_DEFAULT` says, so a write through it exits 4 `read_only`, and `connections` names each one as the machine's under `machine_reads`. A verb a [`LINK_METHODS`](#linking-a-connection) ceremony runs uses them there the same way, so a connection declared globally is linked on the machine before any project is granted it; each ceremony's `argv` names the capability and then that verb, one the capability exposes and not a write verb, which the manager validates wherever it validates `MACHINE_READS`. Every other verb outside a project is refused the machine's connections, and inside a project grants alone decide what every verb may use. Outside any project a machine read is [machine administration](#machine-administration): it reads no policy row and answers to the machine ceiling first, and the read-only switch still closes every gate.

### `connections` — the resolution report

`<name> connections` prints where every value of every declared connection resolves from — the programmatic answer to *"which credentials is this using, and from where?"*. It is **purely local**: resolution only, no authentication attempt and no call to any system a connection names (readiness stays `doctor`'s question); in store mode reading the store is the resolution. The report always carries the same shape, so a consumer never branches on cardinality:

```json
{
  "default": "billing",
  "connections": {
    "billing": {
      "allow_write": true,
      "keys": [
        { "key": "address", "secret": false, "required": true, "set": true,
          "tier": "connection", "source": "/path/capabilities/mailbox/connections.json",
          "value": "billing@example.com" },
        { "key": "MAILBOX_BILLING_APP_PASSWORD", "secret": true, "required": true, "set": true,
          "tier": "project", "source": "/path/.env.local", "value": "…k9f3" }
      ]
    }
  }
}
```

Per key: `set` — a non-empty value resolved; `tier` — `connection` (literal in the registry entry), `project` (`.env`/`.env.local`), `user` (`credentials.env`), or `env` (process env); `source` — the winning file's absolute path, `null` for process env. A flag override is per-invocation and never part of the report. An unset required key reports `"set": false` with the same remediation `doctor` would name. `allow_write` is the **effective** value — the entry's, else `WRITE_DEFAULT` applied.

Each connection in the report carries `identity`, who it is as its record says, with no round-trip: the [`identity`](#connections) recorded on it, else - where the capability marks a connection field as its main identifier - `{"id": <that field's value>, "display_name": null}`, else `null`.

A non-secret value prints in full. A secret prints **masked** — `…` plus the last 4 characters, fully masked (`"****"`) when shorter than 8 — one fixed rule, never a per-capability choice:

```python
def _mask(value: str) -> str:
    return ("…" + value[-4:]) if len(value) >= 8 else "****"
```

Beside the connections the report carries `policy`, the capability's effective policy state in the words `capabilities list` uses: `effective` (`enabled` or `disabled`), the `source` that decided it (`machine` for the ceiling, `project`, `global`, or `default` when neither scope says anything) and the `machine` ceiling itself. A policy record that cannot be read is reported there as the refusal the gate would give, and the report still answers.

#### Revealing one value

`<name> connections show <id> --reveal <KEY>` prints one value in the clear, so a person's own control panel can show or copy a secret it otherwise displays masked: the value the declared [connection field](#what-a-person-is-asked-for) `KEY` of connection `<id>` resolves to - for a secret field, the value of the env key the entry names, resolved through the cascade - raw on stdout with a trailing newline and nothing else. The connection is selected as every verb selects one, so an unknown connection exits 6 `unknown_connection` and one the project may not use exits 4 `connection_not_granted`. A key the capability does not declare exits 6 `unknown_field` and one that holds no value exits 3 `not_set`, each with nothing on stdout. It is not safe discovery: it passes [the capability policy gate](#the-capability-policy-gate) as an operational verb, and outside any project it is a machine read where the capability declares `connections` in `MACHINE_READS`. Every other output keeps the masking rule above.

A **core-only** capability — one binding the core tier and not the connections tier, because it has nothing to resolve (it drives a local tool or the host, with no credentials or endpoint) — still answers `connections`, reporting an empty map: `{ "connections": {}, "default": null }`. That absence is the contract, not a gap: `audit` accepts the empty report in place of the explicit-registry checks, and never writes a registry against such a capability.

## What a person is asked for

What a capability needs from a person is declared once, at the top of the script, so any reader - `manifest`, an agent, a control panel - renders it and drives it without knowing the capability. Five declarations, each optional:

- `CONN_FIELDS` - the fields of one connection entry, the interior [connections](#connections) leaves to the capability.
- `SETUP_FIELDS` - what the machine is given once, before any connection: an app's own credentials, a key a side feature uses. Each is an env key, kept in the capability's `credentials.env` and resolved through [the credential cascade](#the-credential-cascade); a field that declares `shared` is kept once for the machine, in `shared.env`, for every capability that declares it.
- `SETUP` - that setup as a whole, each key optional: `title`, what the setup is called; `guide`, `{ "url", "steps" }`, the page where a person obtains the fields and the steps they follow there, each one sentence; and `verify`, the name of a verb of the capability that, run with no arguments, asks the remote system whether it accepts the machine's setup and exits 0 only when it does. A field's `label` names it as that page does, so a person finds it there.
- `LINK_METHODS` - the interactive ceremonies that link a connection to an account.
- `SETTINGS` - where the capability's settings live and the keys it declares ([settings](#settings)).

A field, in `CONN_FIELDS`, `SETUP_FIELDS` and `SETTINGS.keys` alike:

| Key | Meaning |
|---|---|
| `key` | The entry field, env key or dotted settings key it is written to. |
| `label`, `help` | What a person is shown: a few words, and one sentence. |
| `type` | `string`, `integer`, `number`, `boolean`, `url`, `email`, `phone`, `path`, `enum` (with `choices`), `list` or `object`. |
| `required` | Whether a connection is incomplete without it. |
| `secret` | The value is a secret: it is taken only on stdin and never from argv, and never sits in a record. A secret connection field's `key` is the entry field that names its env key (`secret_env`, `db_password_env`), and `env` is the template of that key, `{ID}` standing for the connection id upper-cased with every other character an underscore. |
| `default`, `minimum`, `maximum`, `choices` | Optional: the value used when none is given, and the bounds a value is checked against. |
| `shared` | `SETUP_FIELDS` only, optional: the name, an env key in upper case (`<SERVICE>_API_KEY`), under which the value is kept once for the whole machine in `$XDG_CONFIG_HOME/capabilities/shared.env` rather than in the capability's own `credentials.env`. Every capability whose field names the same shared name reads the one value. It is the key a machine-wide store of secrets fills when one takes that file's place. |
| `identity` | `CONN_FIELDS` only, optional: `true` marks the field as the connection's main identifier - the account id for a messenger, the address for a mailbox - on at most one field and never on a secret. |

The manager validates the declarations wherever it validates a manifest.

### `connection add|remove`

`<name> connection add <id> [--project|--global|--machine] [--default] [--replace] [--field KEY=VALUE ...] [--env KEY=ENV_KEY ...] [--stdin] [--identity ID [--display-name TEXT]]` writes one connection from the declared fields, and is answered the same way by every capability that declares `CONN_FIELDS`. A non-secret field may come on argv; every field may come in one JSON object on stdin, and a secret field only there. A secret is written to the credentials file of the scope the record lands in - the project's `.env.local` for `--project`, the default, or the capability's `credentials.env` for `--global` and `--machine` - under its own env key, single-quoted, the file replaced atomically at mode 0600 and read back through the cascade's parser, and the entry names the key. `--env KEY=ENV_KEY` instead names, for a secret field, a key the cascade already resolves, so the entry names that key and no value is written for it; the name must resolve when the connection is added, or the call refuses with exit 6 `env_key_unset` without echoing it, and `connection remove` keeps a key it did not write. The entry is written through the records adapter, so a project in store mode keeps it in its store as any connection. `--machine` writes at the machine's scope, as `--global` does, as [machine administration](#machine-administration), and refuses beside `--project`. A connection that already exists at the scope is refused with exit 6 `connection_exists` unless `--replace` is given; a replaced connection keeps every field not given, and `null` in the JSON object clears one. Required fields left unset refuse with exit 6 `fields_required`, a secret on argv with exit 6 `secret_on_argv`. `--default` points the scope's default at it. `--identity ID` records the connection's [`identity`](#connections) by hand, with `--display-name TEXT` beside it or none; `--display-name` alone renames the identity recorded and refuses with exit 6 `input` where none is, and `--identity ''` clears it. The answer names the record, the credentials file, every field as it now resolves, secrets masked, and the connection's `identity` as `connections` reports it; inside a project it says whether the project may use the connection (`granted`) and, for a global connection or one the project may not yet use - a project in store mode uses even its own connection only through a grant - the grant it still needs.

`<name> connection remove <id> [--project|--global|--machine]` removes the entry, the default pointer when it names it, and every secret line `connection add` wrote for it; a key another connection at that scope names is kept. It refuses with exit 6 `connection_granted`, naming each project, while a project is granted the connection and would lose it: for a connection kept in the store, every project the store holds a grant enabling it for, on any node (`Records.grantees`); for a connection of the machine, the project the call runs in when that project grants it and declares no connection of its own under that id. A project's own connection in its files is granted to no other project. A project switches its grant off before the connection is removed.

### `setup`

`<name> setup` reports each declared prerequisite: whether it resolves, from which tier and file, secrets masked, and `ready` when every required one does. A field that declares `shared` is reported under `shared`, apart from `fields`, beside `shared_credentials`, the shared file's absolute path, both present only where a field declares `shared`, so a control panel leaves shared fields out of each capability's form and asks for each shared name once. `setup set [--machine] [--field KEY=VALUE ...] [--stdin]` writes them to the capability's `credentials.env`, and a shared field to `shared.env` under its shared name, a secret only from the JSON object on stdin, and `setup unset [--machine] <KEY> ...` removes them, a shared field from `shared.env` and from the capability's own `credentials.env` alike. `setup show --reveal <KEY>` prints the one value that report masks for the declared setup field `KEY`, as the cascade resolves it, raw on stdout with a trailing newline and nothing else; a key the capability does not declare exits 6 `unknown_field` and one that resolves nowhere exits 3 `not_set`, each with nothing on stdout. Each answer of `setup`, `setup set` and `setup unset` carries the `title`, `guide` and `verify` the capability declares in `SETUP`, each only where declared. Outside any project `setup` reporting is [machine administration](#machine-administration), and so is a write that carries `--machine`. Every connection of the capability resolves them through the cascade, so a control panel asks for them once, above the connections.

### Linking a connection

A verb that links a connection to an account interactively - a QR to scan, a code to enter, a two-step password - takes `--events`, and is then drivable with no terminal. It speaks JSON lines on stdout, one object per line naming its `event`, and reads the answer to each event that asks for one as one line on stdin:

| Event | Fields | Meaning |
|---|---|---|
| `qr` | `payload`, `expires`?, `refreshed` | A QR to render and scan; a new one follows each that expires. |
| `pairing_code` | `code`, `phone` | A code the person enters on the phone. |
| `phone_needed` | | Answer with the phone number. |
| `code_needed` | `phone`, `retry` | Answer with the code the service sent. |
| `password_needed` | `hint`, `retry` | Answer with the account's two-step password. |
| `linked`, `progress` | the capability's | The link completed; the ceremony is still at work. |
| `done` | `result` | The result the verb prints without `--events`, as the last line. |
| `error` | `exit`, `error` | The refusal, as the last line; the same envelope also goes to stderr. |

Standard input closing while an answer is awaited exits 6 `input_closed`. The exit code is the one the verb has without `--events`, and without `--events` the verb is unchanged. `LINK_METHODS` names each ceremony - `id`, `label`, `help` and its `argv`, which carries `--events` - to be run with `--connection <id>` added. The contract carries the stream (`_events_begin`, `_event`, `_event_answer`): once a verb begins it, `_emit` writes the `done` event and `_die` the `error` event.

### Settings

A capability that keeps settings declares where: `SETTINGS["homes"]`, one home per scope, each `{ "scope", "path", "format", "inherits"?, "seed"? }`. `scope` is `project` (the path is under the capability's folder in the project envelope), `global` (under its folder in the user's config home, the defaults every project reads beneath its own) or `machine` (under the same folder, the settings of the capability's machine service); `format` is `json` or `toml`; `inherits` names the scope a top-level key absent here is read from, as the records adapter merges a project's settings over the global ones; `seed` is the document a file not yet written starts as. `SETTINGS["keys"]` declares the keys a control panel shows as controls, each a field as above with `scope`, and `apply`: `next_call` (read afresh by the next call), `reload` (a running service takes it on `service reload`) or `restart` (only a stopped and started one does). Keys not declared are still read and written; they are what a panel shows as JSON.

`<name> settings [--scope S]` reads every home whole, with each declared key's value and the scope it came from. `settings get <key>`, `settings set <key> <value>`, `settings unset <key>` (a dotted key reaches a nested one, and a JSON array of keys one whose keys contain dots; `<value>` is read as JSON, else as text), `settings put < document.json`, which replaces one scope's document, `settings export`, which prints it, and `settings edit [--yes]`, which opens it in `$VISUAL` or `$EDITOR`, shows what changes and writes it once confirmed, write through one surface whatever the home's format; `--scope` defaults to the key's declared scope, else the only home. A declared key's value is checked against its declaration, then the whole document as the capability itself reads it - `_settings_check(scope, document)`, in the capability's own code, returns the problems its own reader finds in the document about to replace the home - and a refusal is exit 6 `settings_invalid` with nothing written. For an agent whose records moved into the store, a project or global home is rows: a write locks the document, applies the change, checks the whole result, stores the paths that changed with a trail row each in one transaction, and answers the document's new `generation`; `settings` names the generations it read under `generations`, and a removed last child leaves `{}` at its parent. The file is replaced atomically; a TOML home is rewritten from its content, so its comments are not kept. The answer names `apply`, so a caller knows whether to `service reload`. Under the read-only switch every write is refused.

## Identity-free

A shared tool bakes in no consumer's identity — no person, company, tenant, account, or host-with-tenant **value** (DOCTRINE rule 10). The consumer supplies those through the cascade; the tool refers to them by role. Config-key *names* (`<NAME>_API_KEY`, `<NAME>_WORKSPACE`) are structural and belong in the script; the *values* never do. This holds in the help text too: examples use placeholders (`user@example.com`, `<PERSON>`), never a real name or address. A default that would otherwise hardcode a consumer (an author or actor name, a default identity) is sourced from env/config with no baked-in fallback — absent the value, the tool asks for it rather than assuming one.

## State

State follows the **scope of the credentials that minted it**, resolved by one shared shape:

```python
_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))

def _state_dir() -> Path:
    if SCOPE == "project":
        root = _project_root()
        if root is not None:
            envelope = _project_capabilities_dir(root)
            if ((envelope / "project.json").is_file()
                    or (envelope / "settings.json").is_file()
                    or (envelope.is_dir() and _bound_project_id(root))):
                return envelope / NAME / "state"
    return _STATE_HOME / NAME
```

- **Project scope** → `<root>/capabilities/<name>/state/` — session cookies, scrape caches, pin-pending markers, each project isolated to its own account session. The manager-owned `capabilities/.gitignore` guarantees `*/state/` never commits. Outside an initialized envelope state falls back to the user state home; operational use there still requires global policy enable and an explicit global connections registry where the capability bears connections.
- **User scope** → `$XDG_STATE_HOME/<name>/` (default `~/.local/state/<name>/`).

A stateful capability keys its state **per connection** — `<state-dir>/<connection-id>/…` — so two connections of one capability never share a session. The guides cache is the exception: guide content is capability-scoped and connection-independent, so it stays unkeyed at the user state home.

**Bulk data stores are relocatable at the root, fixed inside.** A capability that syncs bulk data (message archives, exports) defaults the store to `<state-dir>/<connection-id>/…` like any state — and MAY expose the **root** as a per-connection key on the connection entry (e.g. `messages_dir`: absolute, or relative to the project root) for a consumer who wants the data elsewhere. Only the root moves: the structure beneath it is the CLI's contract, documented in its help, identical wherever the root points. The `*/state/` gitignore guard covers only the default location, so the guard travels as a responsibility: `doctor` verifies the active root is git-ignored and warns when it is not (DOCTRINE rule 16 — synced data is minted by credentials and never commits).

Known limitation, recorded for fast diagnosis: two projects driving the *same* account of a single-session service thrash each other's cookies (login here invalidates the cookie there). Per-project state trades that for account isolation — the right trade where re-logins are cheap.

## Runtime state in the store

Runtime state is what a capability's own processes coordinate through — a service's run ledger, its job tables, the session a capability resumes from one call to the next — and it lives in the PostgreSQL its project's database setting resolves to ([the store setting](#the-store-setting); DOCTRINE rule 22).

A capability reaches it through the shared database library, `capabilities-contract` on PyPI, imported as `capabilities_contract.db` and pinned to an exact version in the script's PEP-723 dependencies. The library resolves the database setting for a project root through the cascade and writes nothing, connects bound to the schema that setting names, and applies each owner's migration steps once under that owner's ledger; the library's own documentation carries its call surface and error codes. The capability is the owner of its tables: the owner is its name, every object a migration step creates is named `<name>_…` inside the setting's schema, and its steps are recorded in the ledger under that name, so tools at different releases share one database and each changes only its own tables.

**Migrations run when asked.** A capability that keeps tables declares `TABLES = True` beside `STATE`, and a `_tables()` builder returning its steps and its schema version `(steps, major, minor)`; the core tier then answers `<name> migrate`, which applies the pending steps under the library's lock, and `<name> migrate status`, which reports where the tables stand without a lock and changes nothing. Three callers ask for it: the manager runs `<name> migrate` when it installs or updates the payload, so the tables reach the code where the code arrives, and reports a refusal without undoing the payload; a service runs it once when it starts; and an ordinary call that finds its tables missing or older than its code migrates once on the library's locked fallback path. Every other call takes no lock and applies nothing. The library bounds each wait - a connect, a lock - and reports one that runs out as `store_unreachable` or `store_busy`, so `doctor` answers within a watchdog's timeout rather than hanging, and both ends of a connection probe an idle peer so the store drops a client that died; the bounds are the library's to state.

**One working connection per process.** A process that holds `LISTEN` or a session lock keeps one session connection per store for it, at the session address, and may put its own questions on it. Every other statement runs on the process's working connection, a transaction connection the shared database library opens on first use, lends to each operation - a command's call, a read or write of records, a lease renewal, a pass of a service - and keeps until the process exits, so a command connects once and a service keeps one working connection beside its listener. A supervisor whose service is not running here closes it after each read of the gate, and holds no connection between them. The library tries a failed connect again and replaces a working connection the store or the network lost; a service whose store goes away keeps running and asks for it again on a doubling interval, and a supervisor does not restart a service whose doctor fails because the store does not answer.

A service that keeps runtime state resolves the store before it takes any work. With no store configured it refuses to run with the error code `store_not_configured` and a hint naming the `AGENTKIT_DB_*` keys and `capabilities store set`, and its `service doctor` reports the same. The library finds a store only through the cascade of [the store setting](#the-store-setting), and every level names a PostgreSQL.

## The I/O contract

Success is JSON on **stdout**; failure is a structured envelope on **stderr**; the exit code carries the category. Two helpers are the whole contract:

```python
def _emit(value) -> None:
    if isinstance(value, str):
        sys.stdout.write(value if value.endswith("\n") else value + "\n")
    else:
        sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

def _die(exit_code: int, code: str, message: str,
         hint: str | None = None, status: int | None = None) -> NoReturn:
    err: dict = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    if status is not None:
        err["status"] = status
    sys.stderr.write(json.dumps({"error": err}, ensure_ascii=False) + "\n")
    sys.exit(exit_code)
```

`_emit` prints structured results as indented JSON and scalar results (a hash, a job id) as a bare string — a caller can consume either without a schema dance. `_die` prints exactly one shape — `{"error": {"code", "message", "hint"?, "status"?}}` — where `code` is a stable machine token, `message` is human-readable, `hint` is the remediation when one exists, and `status` is the upstream HTTP status when the failure came from a request. stdout stays clean for data; diagnostics never pollute it. The gate's exit-4 refusal rides this same envelope — one error shape, everywhere.

## Exit-code taxonomy

The code tells the caller *which kind* of failure without parsing the message:

| Code | Meaning |
|---|---|
| `0` | success |
| `2` | auth — missing/rejected credentials, 401/403 |
| `3` | not found — the addressed resource does not exist, 404 |
| `4` | policy refusal — effective project/global capability policy, explicit project requirement for service activation, or a write verb on a read-only connection |
| `5` | server / network — timeout, connection error, 429 after retries, 5xx |
| `6` | input / validation — a malformed argument, a bad date, a conflicting flag |

Code `1` is left to the runtime for an uncaught exception — a bug, not a handled outcome; handled failures always carry one of the categories above. A capability may add codes **from 7 up** for a domain-specific outcome it needs a caller to branch on (a task blocked by open dependencies, say); it states each addition in its help's exit-code table. The numbers are stable — a caller scripts against them.

## Resilient HTTP

An HTTP CLI wraps every request in one retry loop with bounded, backed-off retries, so a transient rate-limit or 5xx is absorbed rather than surfaced:

```python
TIMEOUT = httpx.Timeout(60.0)
MAX_RETRIES = 3

for attempt in range(1, MAX_RETRIES + 1):
    try:
        resp = client.request(method, path, params=params, json=json_body)
    except httpx.TimeoutException:
        if attempt < MAX_RETRIES:
            time.sleep(attempt * 1.5); continue
        _die(5, "timeout", f"request timed out: {method} {path}", "<service> was unresponsive")
    except httpx.RequestError as e:
        _die(5, "network_error", f"network error: {e}", "check connectivity")

    if resp.status_code == 429:
        retry_after = float(resp.headers.get("Retry-After", str(attempt * 1.5)))
        if attempt < MAX_RETRIES:
            time.sleep(retry_after); continue
        _die(5, "rate_limited", "rate-limited", status=429)
    if resp.status_code >= 500:
        if attempt < MAX_RETRIES:
            time.sleep(attempt * 1.5); continue
        _die(5, "server_error", f"<service> returned {resp.status_code}", status=resp.status_code)
    # 4xx → map to 2/3/6 by status; 2xx → return parsed body
```

A 429 honours the server's `Retry-After` header (falling back to the linear schedule); a timeout or 5xx backs off `attempt * 1.5` seconds; after `MAX_RETRIES` the loop converts the failure into a `_die(5, …)` carrying the upstream `status`. A 4xx is not retried — it maps straight to `2` (auth), `3` (not found), or `6` (validation). A CLI over a non-HTTP protocol realizes the same intent in its protocol's terms — fail fast and clearly on a connection error, retry only what is idempotent and transient.

## `doctor`

Every CLI has a `doctor` subcommand: the cheapest authenticated round-trip that proves the whole chain — credentials resolved, endpoint reachable, identity confirmed. It examines **every connection**: bare, it round-trips each configured connection and reports per id — `{"ok": <all healthy>, "connections": {"<id>": {…}}}` — exiting `0` only when every connection is healthy, else with the first failure's category; `doctor --connection <id>` checks one. Per connection it carries a few cheap facts (the service version, the authenticated identity, a reachability flag) and rides the same exit-code taxonomy — `0` healthy, `2` if credentials are missing or rejected, `5` if the service is unreachable. It is the first thing an agent runs against a tool, and for a stateful CLI it is also the recovery point: `doctor` detects an expired session and attempts re-authentication before reporting, so a healthy exit means *ready to work*, not merely *configured*.

Credential resolution is the **first gate, and it calls no system a connection names**: before any round-trip, `doctor` resolves the connection(s) under test through the cascade — the *same* resolution [`connections`](#connections--the-resolution-report) reports — and refuses with exit `2` when any **required** key is unset, naming the remediation from the credential scope and `CRED_KEYS`. The verdict is the cascade's, read across its resolution tiers (`connection` literal, `project`, `user`, `env`) from the connection's own report rows — **never the presence of any single file**. One resolver, two consumers: `doctor`'s readiness gate and `connections`' report answer *"are the credentials here?"* through the same per-key resolution, so they cannot diverge — the failure mode where a `doctor` preflight checks one tier while the real commands resolve through all of them is structurally precluded.

```python
def _missing_required(keys: list[dict]) -> list[str]:
    """Required report rows (from `_key_report`) that did not resolve through the
    cascade — empty ⇒ credentials present. `doctor`'s first gate, which calls no
    system a connection names, derived from the same per-key resolution
    `connections` prints, so the readiness gate and the resolution report can
    never disagree."""
    return [k["key"] for k in keys if k["required"] and not k["set"]]
```

Only a connection that clears this gate proceeds to the live round-trip (the per-capability authenticated probe, conventionally `_check_connection`). A capability with nothing to resolve — core-only, empty `CRED_KEYS`, binding no connections tier — has no gate to clear: `doctor` proves whatever local readiness it can (a host grant, a tool on `PATH`) and stops there.

`doctor` is the **readiness oracle**. The stub only announces that a tool exists, so "is it wired up *here*?" is `doctor`'s question to answer at use-time, never inferred from stub presence. A failing `doctor` names the exact remediation. A connection-bearing capability is ready only after effective policy enables it and an explicit project or global registry declares its identity; the registry's named values then resolve through the credential cascade. A core-only capability still has no registry requirement.

**`doctor` raises one question, and only where the system cannot know the answer.** As the process exits, after whatever the capability's own `doctor` answered, the contract library writes at most one line on stderr, `{"supervisor": {"question", "conflicts", "update_failed"}}`: `conflicts[]` names each key `doctor` resolved from a file (the project's `.env.local` or `.env`, the machine's `shared.env`, the capability's `credentials.env`) that the agent's vault holds with another value, `{key, files}`, never a value; `update_failed` is the manager's record of an update of the capability that stopped, `{name, at, error}`, else `null`; `question` says both in one sentence. Equal values, a key held in one place only, a vault that cannot be read and a healthy state raise nothing, and nothing else about the vault or the files is raised. The line changes neither `doctor`'s answer on stdout nor its exit code. A capability raises it by passing `doctor` through `_contract`, as every capability does, and writes no code of its own for it.

## Conformance and deviation

These patterns are a strong default, not a cage — the same standing allowance the doctrine grants ([DOCTRINE.md](DOCTRINE.md#deviations-are-allowed--and-recorded)). A capability whose protocol or interaction model genuinely differs realizes the *intent* of a pattern in its own form. Such a deviation is **recorded in the capability's own dedicated deviation file** — a file whose sole purpose is to describe it, kept apart so it is never commingled with other content or accidentally dropped — never here: this standard states the rule, a capability states its own exception, in its own folder. `capabilities audit` reads that file first and treats the deviation as a deliberate choice, not drift to fix. The bar is realizing the intent: a deterministic cascade, deterministic connection selection with a hard write gate, a clean stdout/stderr split, a stable exit-code contract, the contract verbs, the gate, a `doctor`, an agent-first help, and no consumer identity baked in.
