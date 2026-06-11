# The shebang CLI

What a capability's **executable** is comprised of — the code shape every `bin/<name>` fills. The script is the capability's **sole distributed artifact**: one self-contained file carrying its domain verbs and the **contract verbs** that make it self-describing and self-gating. This file is the contract's specification — the verbs and their output shapes, the shared code patterns (the project walk, the gate, the credential cascade, state resolution), the I/O envelope, and the exit codes. The behavioural invariants — the credential cascade, identity-freedom, discoverable knowledge, host-neutrality — and how to validate them live in the doctrine ([DOCTRINE.md](DOCTRINE.md)); this file holds the **code patterns that realize them**, distilled from the CLIs that already embody them so a new executable slots in without re-deriving the house style.

A capability's CLI is the **only** thing a consuming project runs — symlinked onto `PATH` by the manager, knowing nothing of hosts or sibling capabilities — so its surface, its contract, and its failure behaviour are the capability's public API. The patterns below are what make that API uniform across every tool an agent picks up.

## One file, `uv run`, PEP-723

The executable is a single script with its dependencies declared inline, run by `uv` with no venv to provision and no install step. Every CLI opens identically:

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

Every script implements the same contract verbs alongside its domain verbs. The contract is **protocol 1** — this document is its specification — and it is a **spec, never a shared runtime library**: each script realizes it by copying the patterns in this file, and `capabilities audit` verifies conformance by calling the verbs and validating output shapes. No script imports from the manager, so a manager update can never break a deployed script.

| Verb | Returns |
|---|---|
| `help` | The full usage contract (the module docstring), verbatim. |
| `doctor` | The cheapest authenticated round-trip; proves credentials, reachability, identity. |
| `stub` | The one-line awareness text. |
| `manifest --json` | The machine-readable declaration (schema below). |
| `guide` / `guide <topic>` | The menu of upstream guides / one guide's body, fetched live from the docs base. |
| `ids list\|get\|set\|rm` | The project identifiers envelope, managed. |
| `refs` | The menu of the project's reference files, from front-matter. |

The declaration facts feed the contract from **constants at the top of the script** — one home each. Every contract verb renders from these, so the verbs cannot disagree with one another:

```python
NAME = "asana"
PROTOCOL = 1        # contract version this script implements
SUMMARY = "Asana CLI over the REST API — list/read/create tasks, comment, complete."
SCOPE = "project"   # credential scope: "project" | "user"
CRED_KEYS = [
    {"key": "ASANA_TOKEN", "secret": True, "required": True, "note": "personal access token"},
]
DOCS_BASE = "https://raw.githubusercontent.com/<org>/capabilities/main/capabilities/asana/guides/"
TOPICS = ["authoring", "boards"]    # [] when no guides ship; DOCS_BASE "" likewise
STATE = False       # True when the capability writes session/cache state
POST_INSTALL = []   # [{"cmd": …, "note": …}] steps the manager offers at install
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
  "protocol": 1,
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
  "post_install": []
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
  settings.json       # capability-owned, free shape: non-secret project config
  identifiers.json    # the identifiers envelope, managed by the ids verbs
  state/              # capability-written; never committed
  *.md                # references: front-matter envelope + free prose
```

(`.capabilities/settings.json` — one level up — is the manager-owned gate; the script only ever reads it.)

- **`settings.json`** — the non-secret values that drive this capability in this project: base URLs, tenants, workspace/folder, profiles, behavioral defaults, `secret_env` indirection where the secret's *key name* is configurable. The standard names the location and the file name; the capability owns the schema.

- **`identifiers.json`** — discoverable, non-secret, structural lookup (DOCTRINE rule 4). A thin standard envelope — label → `{ value, note }` — so any reader can render the menu without understanding the capability; capability-specific structure lives inside values:

  ```json
  {
    "workspace": { "value": "1199…", "note": "workspace gid" },
    "sections":  { "value": { "Backlog": "1200…", "Doing": "1201…" }, "note": "board sections" }
  }
  ```

  Settings vs identifiers is a **provenance split**: settings hold values someone *chose*; identifiers hold values the CLI *discovered*. Different writer, different cadence, different git-diff meaning — never merged.

- **References** — prose by nature (a model, a treatment, a taxonomy); the envelope is standardized, the content free:

  ```markdown
  ---
  name: vat-regimes
  description: How VAT regimes map to income accounts on sales invoices
  ---
  ```

  `<name> refs` emits the menu — `[{ "name", "description", "path" }]` — reading **front-matter only**, never the bodies.

The `ids` verbs manage the identifiers envelope:

| Verb | Does |
|---|---|
| `ids list` | Prints the envelope verbatim (it is small, non-secret lookup). Empty/absent → `{}`. |
| `ids get <label>` | Prints the value as stored. Unknown label → exit 3. |
| `ids set <label> <value> [--note <text>]` | Upsert: `<value>` parses as JSON, a non-JSON argument stores as a string; `--note` sets the annotation. |
| `ids rm <label>` | Removes the label. Unknown label → exit 3. |

`ids set` creates `.capabilities/<name>/` on demand **inside an existing `.capabilities/`**; in a project with no `.capabilities/` envelope it exits 6, naming `capabilities init` as the remediation.

## The gate

Every verb — `help` and `doctor` included — passes the project gate before dispatch. The gate reads one file, the manager-owned `.capabilities/settings.json` at the project root, resolved by the same walk everything project-scoped shares:

```python
def _project_root() -> Path | None:
    """Nearest project root, walking up from $CLAUDE_PROJECT_DIR (else cwd):
    the first directory holding .capabilities/, .env(.local), or .git."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    for d in (here, *here.parents):
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
| `4` | disabled by project policy — the gate, on every verb |
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

Every CLI has a `doctor` subcommand: the cheapest authenticated round-trip that proves the whole chain — credentials resolved, endpoint reachable, identity confirmed. It returns `{"ok": true, …}` on stdout with a few cheap facts (the service version, the authenticated identity, a reachability flag) and rides the same exit-code taxonomy — `0` healthy, `2` if credentials are missing or rejected, `5` if the service is unreachable. It is the first thing an agent runs against a tool, and for a stateful CLI it is also the recovery point: `doctor` detects an expired session and attempts re-authentication before reporting, so a healthy exit means *ready to work*, not merely *configured*.

`doctor` is the **readiness oracle**. The stub only announces that a tool exists, so "is it wired up *here*?" is `doctor`'s question to answer — at use-time, per context, never inferred from the stub's presence. A failing `doctor` does not just report "not configured"; it names the exact remediation, including *where* the missing config goes — derived from the declared credential scope and `CRED_KEYS`: the project `.env`, or `~/.config/<name>/credentials.env` — so an agent learns how to wire the tool up from the tool itself. A capability whose required config is global (or nil) is usable from any project the moment it is installed; one that needs project-side config is globally *aware* but only *ready* where that config exists, and `doctor` makes the difference explicit.

## Conformance and deviation

These patterns are a strong default, not a cage — the same standing allowance the doctrine grants ([DOCTRINE.md](DOCTRINE.md#deviations-are-allowed--and-recorded)). A capability whose protocol or interaction model genuinely differs realizes the *intent* of a pattern in its own form. Such a deviation is **recorded in the capability's own dedicated deviation file** — a file whose sole purpose is to describe it, kept apart so it is never commingled with other content or accidentally dropped — never here: this standard states the rule, a capability states its own exception, in its own folder. `capabilities audit` reads that file first and treats the deviation as a deliberate choice, not drift to fix. The bar is realizing the intent: a deterministic cascade, a clean stdout/stderr split, a stable exit-code contract, the contract verbs, the gate, a `doctor`, an agent-first help, and no consumer identity baked in.
