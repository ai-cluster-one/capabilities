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

Every script implements the same contract verbs alongside its domain verbs. The contract is **protocol 2** — this document is its specification — and it is a **spec, never a shared runtime library**: each script realizes it by copying the patterns in this file, and `capabilities audit` verifies conformance by calling the verbs and validating output shapes. No script imports from the manager, so a manager update can never break a deployed script.

| Verb | Returns |
|---|---|
| `help` | The full usage contract (the module docstring), verbatim. |
| `doctor` | The cheapest authenticated round-trip, per connection; proves credentials, reachability, identity. |
| `connections` | The resolution report: every connection, every value, its winning tier and source, secrets masked. Local only — no network. |
| `stub` | The one-line awareness text. |
| `manifest --json` | The machine-readable declaration (schema below). |
| `guide` / `guide <topic>` | The menu of upstream guides / one guide's body, fetched live from the docs base. |
| `ids list\|get\|set\|rm` | The project identifiers envelope, managed. |
| `refs` | The menu of the project's reference files, from front-matter. |

The declaration facts feed the contract from **constants at the top of the script** — one home each. Every contract verb renders from these, so the verbs cannot disagree with one another:

```python
NAME = "asana"
PROTOCOL = 2        # contract version this script implements
SUMMARY = "Asana CLI over the REST API — list/read/create tasks, comment, complete."
SCOPE = "project"   # credential scope: "project" | "user"
CRED_KEYS = [
    {"key": "ASANA_TOKEN", "secret": True, "required": True, "note": "personal access token"},
]
WRITE_VERBS = {"create", "comment", "complete"}   # domain verbs that mutate the remote system
WRITE_DEFAULT = True    # a connection's allow_write when its entry is silent; False when writes leave the system
DOCS_BASE = "https://raw.githubusercontent.com/<org>/capabilities/main/capabilities/asana/guides/"
TOPICS = ["authoring", "boards"]    # [] when no guides ship; DOCS_BASE "" likewise
STATE = False       # True when the capability writes session/cache state
POST_INSTALL = []   # [{"cmd": …, "note": …}] steps the manager offers at install
SERVICE = None      # or {"name", "summary", "verbs", ...} when a bundled service ships
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
  "protocol": 2,
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
  "post_install": [],
  "service": {
    "name": "assistant",
    "summary": "Project-local assistant daemon using the bundled service engine.",
    "verbs": ["init", "doctor", "run", "start", "stop", "status", "logs"]
  }
}
```

| Field | Meaning |
|---|---|
| `name`, `protocol`, `summary` | The constants, verbatim. |
| `credentials.scope` | `project` or `user` — where the secret lives (see [the credential cascade](#the-credential-cascade)). |
| `credentials.keys[]` | Every key the cascade resolves: `key`, `secret`, `required`, `note`. Install scaffolding and `doctor`'s remediation both derive from this list. |
| `docs.base` | The upstream guides base URL; `""` when no guides ship. Overridable through the cascade as `<NAME>_DOCS_BASE`, so storage can move without touching the contract. |
| `docs.topics` | The shipped topic list; `[]` when no guides ship. |
| `state` | `true` declares the capability writes session/cache state (see [state](#state)). |
| `post_install[]` | `{ "cmd", "note" }` steps the manager **offers** at install — idempotent, never auto-run. |
| `service` *(optional)* | Conservative metadata for a bundled service: at minimum `name`, `summary`, and `verbs[]`. The CLI owns the detailed lifecycle contract, usually under `<name> service ...`. |

## Guides

`<name> guide` prints the topic menu (from `TOPICS`); `<name> guide <topic>` prints one guide's body. The script is the **door** to the docs, never the storage: the topic list is computed from what the capability ships so it cannot drift, and a body resolves **live** against the declared base so an upstream edit reaches every consumer at once.

Resolution order, per topic:

1. **Conditional GET** `{DOCS_BASE}{topic}.md` — `If-None-Match` with the cached ETag when one exists; a short timeout (10s), no retries.
2. **200** → print the body; write body + ETag to the cache. **304** → print the cache.
3. **Network failure or 5xx** → print the cache, with a one-line staleness warning on stderr. No cache → `_die(5, …)` naming the URL that failed.

The cache is the **offline floor, never the authority**: `$XDG_STATE_HOME/<name>/guides/<topic>.md` (plus `<topic>.etag`) — user-level regardless of credential scope, because guide content is capability-scoped, project-independent, and non-secret.

## The project envelope

Everything the capability reads and writes in a consuming project lives under `.capabilities/<name>/`:

```
.capabilities/<name>/
  connections.json    # the connections registry — standard envelope (see Connections)
  identifiers.json    # the identifiers envelope, managed by the ids verbs
  reference/          # the single home for references — one front-matter .md per topic
    *.md              # front-matter envelope + free prose, surfaced by `<name> refs`
  service/            # project-local config/policy for a bundled service, if any
  state/              # capability-written; never committed
```

(`.capabilities/settings.json` — one level up — is the manager-owned gate; the script only ever reads it.)

- **`identifiers.json`** — discoverable, non-secret, structural lookup (DOCTRINE rule 4). A thin standard envelope — label → `{ value, note }` — so any reader can render the menu without understanding the capability; capability-specific structure lives inside values:

  ```json
  {
    "workspace": { "value": "1199…", "note": "workspace gid" },
    "sections":  { "value": { "Backlog": "1200…", "Doing": "1201…" }, "note": "board sections" }
  }
  ```

  Connections vs identifiers is a **provenance split**: connection entries hold values someone *chose* (wiring, per-connection behavioural keys); identifiers hold values the CLI *discovered*. Different writer, different cadence, different git-diff meaning — never merged.

- **References** — prose by nature (a model, a treatment, a taxonomy); the envelope is standardized, the content free. Each is its own `.md` under `.capabilities/<name>/reference/` — the single home for references, kept apart from the JSON config files. Drop a file in with the front-matter below and the context build picks it up; no index to maintain:

  ```markdown
  ---
  name: vat-regimes
  description: How VAT regimes map to income accounts on sales invoices
  ---
  ```

  `<name> refs` emits the menu — `[{ "name", "description", "path" }]` — reading **front-matter only** from `.capabilities/<name>/reference/*.md`, never the bodies. References elsewhere in the envelope (loose `.md` beside the JSON files) are not read.

The `ids` verbs manage the identifiers envelope:

| Verb | Does |
|---|---|
| `ids list` | Prints the envelope verbatim (it is small, non-secret lookup). Empty/absent → `{}`. |
| `ids get <label>` | Prints the value as stored. Unknown label → exit 3. |
| `ids set <label> <value> [--note <text>]` | Upsert: `<value>` parses as JSON, a non-JSON argument stores as a string; `--note` sets the annotation. |
| `ids rm <label>` | Removes the label. Unknown label → exit 3. |

`ids set` creates `.capabilities/<name>/` on demand **inside an existing `.capabilities/`**; in a project with no `.capabilities/` envelope it exits 6, naming `capabilities init` as the remediation.

## The project gate

Every verb — `help` and `doctor` included — passes the project gate before dispatch. The gate reads one file, the manager-owned `.capabilities/settings.json` at the project root, resolved by the same walk everything project-scoped shares:

```python
def _project_root() -> Path | None:
    """Nearest project root, walking up from $CLAUDE_PROJECT_DIR (else cwd):
    the first directory holding .capabilities/, .env(.local), or .git.
    $HOME is never a project root (the machine registry lives there)."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    home = Path.home().resolve()
    for d in (here, *here.parents):
        if d == home:
            return None
        if ((d / ".capabilities").is_dir() or (d / ".env").exists()
                or (d / ".env.local").exists() or (d / ".git").is_dir()):
            return d
    return None

def _gate() -> None:
    """absent → run; enabled → run; disabled → exit 4. Unreadable never blocks."""
    root = _project_root()
    if root is None:
        return
    gate_file = root / ".capabilities" / "settings.json"
    try:
        entry = json.loads(gate_file.read_text()).get("capabilities", {}).get(NAME)
    except (OSError, ValueError):
        return
    if isinstance(entry, dict) and entry.get("enabled") is False:
        _die(4, "disabled",
             f"{NAME} is disabled in this project ({gate_file})",
             "Do not enable it yourself — ask the user whether to enable it, then stop.")
```

| State | Execution |
|---|---|
| absent — no project, no gate file, or no entry for this capability | runs; the gate has no opinion |
| `"enabled": true` | runs |
| `"enabled": false` | every verb exits 4 |

The gate is a **guardrail, not a security boundary** — it is cwd-based and fails open (an unreadable gate file never blocks). Its job is to stop a stray reference from becoming use: the exit-4 envelope is written for the model and routes the decision to the human. Absence keeps ad-hoc use working — a home directory, a server, cron — so disable is a deliberate user statement, recorded in the file.

## The credential cascade

Every executable resolves its credentials the same way — a deterministic **four-tier cascade**, first non-empty wins — so config follows the developer from a laptop (project and user config present) to a deployed box (global env only) with no change in code and no model or agent lookup:

1. **Flags** — explicit `--…` overrides, per invocation, for non-secret values only. **A secret never has a flag**: `argv` leaks (process lists, shell history), so a secret resolves from the tiers below, and a one-shot secret override is an env-prefix invocation — `ASANA_TOKEN=… asana …` — which is tier 4 working as designed.
2. **Project env** — `.env.local` then `.env` at the project root (`_project_root()` above). The project you're working in wins.
3. **User config** — `$XDG_CONFIG_HOME/<name>/credentials.env` (default `~/.config/<name>/`). The persistent per-machine default.
4. **Process env** — exported or host-injected variables. The fallback that lets a deployed box, where no config file is present, resolve correctly.

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

**Credential scope** is declared in the manifest (`SCOPE`). `project` — the default — homes the secret in the project's `.env`/`.env.local`, nothing global: the shape for service- and tenant-scoped tools. `user` homes it in `~/.config/<name>/credentials.env`: reserved for personal-account tools where one human has one identity. Scope names where install scaffolds and where `doctor` points its remediation; the cascade itself consults all four tiers, identically, regardless of scope.

`credentials.env` is **human-written only**: a script never writes back into it. An artifact a login ceremony mints — a session cookie, an exchanged token — is state and lands in the state directory ([below](#state)). One writer per file, everywhere.

A capability whose secret is not a flat token resolves it in its own shape — keyed indirectly through a config file, or persisted after a login exchange — but the **tiers and their order are preserved**: an explicit override beats project config beats user config beats process env, resolved by deterministic code. The shape may vary; the order may not.

## Connections

Every capability resolves its configuration as **connections** — named, complete resolutions of its credential keys. The model is universal; cardinality is a runtime fact of the consuming project, never a declared trait. A project with no registry has exactly **one implicit connection, `default`**, resolved by the bare cascade above — which is what keeps a deployed box, where only injected env exists, resolving with zero files.

More than one endpoint or identity is declared in the **connections registry** — a standard envelope, written at configuration time by whoever configures, read by the script. The registry resolves through two homes, **first found wins, never merged**:

1. **Project** — `.capabilities/<name>/connections.json`: the project's own endpoints and identities.
2. **User** — `$XDG_CONFIG_HOME/<name>/connections.json` (default `~/.config/<name>/`): machine-level identities, the shape for personal-account tools whose connections are a fact of the machine, not of any one project.

Whichever registry is found is **authoritative**: it fully defines the connection set, the implicit default does not exist beside it, and the other home is not consulted.

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

The envelope is the standard's; the entry **interior** is the capability's (hosts and ports for an IMAP tool; url and workspace for a REST tool). Two field names are reserved in every entry:

- **`secret_env`** — a secret never sits in the registry. The entry names the env key holding it, and that key's *value* resolves through cascade tiers 2–4 exactly as any secret does — the registry namespaces the key, the cascade resolves it. A capability with several secrets names each through its own `*_env` field.
- **`allow_write`** — the connection's write gate ([below](#the-write-gate)). Absent falls to the capability's declared `WRITE_DEFAULT`.

Non-secret values sit literally in the entry: chosen, committed, per-connection project config — both the wiring (endpoints, workspaces) and any behavioural per-connection keys the capability defines.

### Selection

One flag selects, accepted by every domain verb: `--connection <id>`. A capability may accept a native alias beside it (`--mailbox <id|address>`); the standard flag always works. Resolution is deterministic, refusing ambiguity rather than guessing:

```python
def _connections_registry() -> dict | None:
    """The connections envelope: project envelope first, else the user config
    home. First found wins; None when neither declares one."""
    envdir = _env_dir()
    candidates = ([envdir / "connections.json"] if envdir else []) + \
                 [_CONFIG_HOME / NAME / "connections.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            reg = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            _die(6, "bad_config", f"cannot read {path}: {e}")
        if not isinstance(reg.get("connections"), dict) or not reg["connections"]:
            _die(6, "bad_config", f"{path} is not a connections envelope",
                 'expected {"default": "<id>", "connections": { ... }}')
        return reg
    return None

def _select_connection(reg: dict | None, wanted: str | None) -> tuple[str, dict | None]:
    """flag → default pointer → sole entry → die 6. No registry → the implicit default."""
    if reg is None:
        if wanted and wanted != "default":
            _die(6, "unknown_connection",
                 f"no connections registry; the sole connection is 'default'",
                 "the implicit default resolves from the credential cascade")
        return "default", None
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

`WRITE_DEFAULT` is the capability's word on what silence means, declared once in the script: `True` for tools whose writes stay inside a system the consumer owns (a task tracker, an automation instance); **`False` when a write leaves the system** — messages a human, sends mail, moves money. Under `WRITE_DEFAULT = False`, writing is granted per connection, deliberately, in a committed registry entry — the implicit default connection cannot write at all, so a deployed box that must send carries a registry naming its writing identity. The refused write is the ceremony: exit 4 at the moment of intent routes the grant decision to the human with the exact remediation in hand.

The principle: **the cascade resolves values; a gate is not a value.** No flag, no env var, no tier overrides the gate — it reads from the two declarations alone, and the exit-4 envelope routes the decision to the human, exactly as the project gate does. Exit 4 is the policy-refusal code in both.

### `connections` — the resolution report

`<name> connections` prints where every value of every connection resolves from — the programmatic answer to *"which credentials is this using, and from where?"*. It is **purely local**: resolution only, no network, no authentication attempt (readiness stays `doctor`'s question). The report always carries the same shape, the implicit default included, so a consumer never branches on cardinality:

```json
{
  "default": "billing",
  "connections": {
    "billing": {
      "allow_write": true,
      "keys": [
        { "key": "address", "secret": false, "required": true, "set": true,
          "tier": "connection", "source": "/path/.capabilities/mailbox/connections.json",
          "value": "billing@example.com" },
        { "key": "MAILBOX_BILLING_APP_PASSWORD", "secret": true, "required": true, "set": true,
          "tier": "project", "source": "/path/.env.local", "value": "…k9f3" }
      ]
    }
  }
}
```

Per key: `set` — a non-empty value resolved; `tier` — `connection` (literal in the registry entry), `project` (`.env`/`.env.local`), `user` (`credentials.env`), or `env` (process env); `source` — the winning file's absolute path, `null` for process env. A flag override is per-invocation and never part of the report. An unset required key reports `"set": false` with the same remediation `doctor` would name. `allow_write` is the **effective** value — the entry's, else `WRITE_DEFAULT` applied.

A non-secret value prints in full. A secret prints **masked** — `…` plus the last 4 characters, fully masked (`"****"`) when shorter than 8 — one fixed rule, never a per-capability choice:

```python
def _mask(value: str) -> str:
    return ("…" + value[-4:]) if len(value) >= 8 else "****"
```

A **core-only** capability — one carrying the `capability core` fence and no `connections` fence, because it has nothing to resolve (it drives a local tool or the host, with no credentials or endpoint) — still answers `connections`, reporting an empty map: `{ "connections": {}, "default": null }`. That absence is the contract, not a gap: `audit` accepts the empty report in place of the implicit-default-plus-registry checks, and never writes a registry against such a capability.

## Identity-free

A shared tool bakes in no consumer's identity — no person, company, tenant, account, or host-with-tenant **value** (DOCTRINE rule 10). The consumer supplies those through the cascade; the tool refers to them by role. Config-key *names* (`<NAME>_API_KEY`, `<NAME>_WORKSPACE`) are structural and belong in the script; the *values* never do. This holds in the help text too: examples use placeholders (`user@example.com`, `<PERSON>`), never a real name or address. A default that would otherwise hardcode a consumer (an author or actor name, a default identity) is sourced from env/config with no baked-in fallback — absent the value, the tool asks for it rather than assuming one.

## State

State follows the **scope of the credentials that minted it**, resolved by one shared shape:

```python
_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))

def _state_dir() -> Path:
    if SCOPE == "project":
        root = _project_root()
        if root is not None and (root / ".capabilities").is_dir():
            return root / ".capabilities" / NAME / "state"
    return _STATE_HOME / NAME
```

- **Project scope** → `<root>/.capabilities/<name>/state/` — session cookies, scrape caches, pin-pending markers, session maps, each project isolated to its own account session. The manager-owned `.capabilities/.gitignore` is what guarantees `*/state/` never commits — a session cookie is a secret, so the guard is a precondition: project state lands inside the envelope only where `.capabilities/` already exists. Anywhere else — a home directory, a server, cron, an unwired repo — state falls back to the user state home and ad-hoc use keeps working.
- **User scope** → `$XDG_STATE_HOME/<name>/` (default `~/.local/state/<name>/`).

A stateful capability keys its state **per connection** — `<state-dir>/<connection-id>/…` — so two connections of one capability never share a session. The guides cache is the exception: guide content is capability-scoped and connection-independent, so it stays unkeyed at the user state home.

**Bulk data stores are relocatable at the root, fixed inside.** A capability that syncs bulk data (message archives, exports) defaults the store to `<state-dir>/<connection-id>/…` like any state — and MAY expose the **root** as a per-connection key on the connection entry (e.g. `messages_dir`: absolute, or relative to the project root) for a consumer who wants the data elsewhere. Only the root moves: the structure beneath it is the CLI's contract, documented in its help, identical wherever the root points. The `*/state/` gitignore guard covers only the default location, so the guard travels as a responsibility: `doctor` verifies the active root is git-ignored and warns when it is not (DOCTRINE rule 16 — synced data is minted by credentials and never commits).

Known limitation, recorded for fast diagnosis: two projects driving the *same* account of a single-session service thrash each other's cookies (login here invalidates the cookie there). Per-project state trades that for account isolation — the right trade where re-logins are cheap.

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
| `4` | policy refusal — the project gate (every verb), or a write verb on a read-only connection |
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

Credential resolution is the **first gate, and it is network-free**: before any round-trip, `doctor` resolves the connection(s) under test through the cascade — the *same* resolution [`connections`](#connections--the-resolution-report) reports — and refuses with exit `2` when any **required** key is unset, naming the remediation from the credential scope and `CRED_KEYS`. The verdict is the cascade's, read across its resolution tiers (`connection` literal, `project`, `user`, `env`) from the connection's own report rows — **never the presence of any single file**. One resolver, two consumers: `doctor`'s readiness gate and `connections`' report answer *"are the credentials here?"* through the same per-key resolution, so they cannot diverge — the failure mode where a `doctor` preflight checks one tier while the real commands resolve through all of them is structurally precluded.

```python
def _missing_required(keys: list[dict]) -> list[str]:
    """Required report rows (from `_key_report`) that did not resolve through the
    cascade — empty ⇒ credentials present. `doctor`'s network-free gate, derived
    from the same per-key resolution `connections` prints, so the readiness gate
    and the resolution report can never disagree."""
    return [k["key"] for k in keys if k["required"] and not k["set"]]
```

Only a connection that clears this gate proceeds to the live round-trip (the per-capability authenticated probe, conventionally `_check_connection`). A capability with nothing to resolve — core-only, empty `CRED_KEYS`, carrying no `connections` fence — has no gate to clear: `doctor` proves whatever local readiness it can (a host grant, a tool on `PATH`) and stops there.

`doctor` is the **readiness oracle**. The stub only announces that a tool exists, so "is it wired up *here*?" is `doctor`'s question to answer — at use-time, per context, never inferred from the stub's presence. A failing `doctor` does not just report "not configured"; it names the exact remediation, including *where* the missing config goes — derived from the declared credential scope and `CRED_KEYS`: the project `.env`, or `~/.config/<name>/credentials.env` — so an agent learns how to wire the tool up from the tool itself. A capability whose required config is global (or nil) is usable from any project the moment it is installed; one that needs project-side config is globally *aware* but only *ready* where that config exists, and `doctor` makes the difference explicit.

## Conformance and deviation

These patterns are a strong default, not a cage — the same standing allowance the doctrine grants ([DOCTRINE.md](DOCTRINE.md#deviations-are-allowed--and-recorded)). A capability whose protocol or interaction model genuinely differs realizes the *intent* of a pattern in its own form. Such a deviation is **recorded in the capability's own dedicated deviation file** — a file whose sole purpose is to describe it, kept apart so it is never commingled with other content or accidentally dropped — never here: this standard states the rule, a capability states its own exception, in its own folder. `capabilities audit` reads that file first and treats the deviation as a deliberate choice, not drift to fix. The bar is realizing the intent: a deterministic cascade, deterministic connection selection with a hard write gate, a clean stdout/stderr split, a stable exit-code contract, the contract verbs, the gate, a `doctor`, an agent-first help, and no consumer identity baked in.
