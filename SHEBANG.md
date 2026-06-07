# The shebang CLI

What a capability's **executable** is comprised of — the code shape every `bin/<name>` fills. This is the doctrine ([DOCTRINE.md](DOCTRINE.md)) applied to the one slot that is a program rather than prose: [TEMPLATE.md](TEMPLATE.md) gives the doc slots their form; this file gives the executable its form. The behavioural invariants — the credential cascade, identity-freedom, discoverable knowledge, host-neutrality — and how to validate them live in the doctrine; this file holds the **code patterns that realize them**, distilled from the CLIs that already embody them so a new executable slots in without re-deriving the house style.

A capability's CLI is one self-contained file. It is the **only** thing a consuming project runs (DOCTRINE rule 13: the CLI reaches every host the same way — symlinked onto `PATH`), so its surface, its contract, and its failure behaviour are the capability's public API. The patterns below are what make that API uniform across every tool an agent picks up.

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

## Agent-first help is the surface

`<name> help` prints a hand-written usage contract that is the **single source of truth** for the surface (DOCTRINE rule 3 — discoverable knowledge lives in the tool, not in docs that go stale). It is the module docstring, emitted verbatim:

```python
if args.cmd == "help":
    sys.stdout.write((__doc__ or "").lstrip("\n")); return
```

The docstring carries everything an agent needs to drive the tool without reading the source: a one-line statement of what the CLI is and that it is agent-invoked, the credential keys it resolves, every subcommand with its arguments and flags, the I/O contract, and the exit-code table. It is written *for an agent loading it on demand* — prescriptive, exhaustive, structured with visual headers — not as terse `--help` output. A stateful CLI (one with a login ceremony) opens its help with the startup protocol the agent must follow (`<name> doctor` first, what each exit code means, how to recover).

When the help text outgrows the docstring, it may live in a `HELP` constant printed the same way; the contract is identical — `<name> help` is the canonical surface, and nothing about a command is documented anywhere a `<name> help` could have answered.

## The credential cascade

Every executable resolves its credentials the same way — a deterministic **four-tier cascade**, first non-empty wins — so config follows the developer from a laptop (project and user config present) to a deployed box (global env only) with no change in code and no model or agent lookup:

1. **Flags** — explicit `--…` overrides, per invocation, where a value override on the command line makes sense (a secret that must never touch `argv` simply omits its flag and resolves from the tiers below).
2. **Project env** — `.env.local` then `.env`, discovered by walking up from `$CLAUDE_PROJECT_DIR` (else cwd) to the project root — the first directory holding either, or a `.git` root. The project you're working in wins.
3. **User config** — `$XDG_CONFIG_HOME/<name>/credentials.env` (default `~/.config/<name>/`). The persistent per-machine default.
4. **Process env** — exported or host-injected variables. The fallback that lets a deployed box, where no config file is present, resolve correctly.

Process env sits **below** the user file deliberately: files are authoritative on a dev machine; injection governs on the box only because no file is there. A one-shot override is a flag, not an `export`. *System-injected* and *ambient export* are indistinguishable at runtime — both are just process env — so they share tier 4.

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
    """Nearest project .env(.local), walking up from $CLAUDE_PROJECT_DIR (else cwd).
    .env.local overrides .env. Stops at the first dir holding either, or a .git root."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / ".env").exists() or (d / ".env.local").exists():
            merged = _parse_env_file(d / ".env")
            merged.update(_parse_env_file(d / ".env.local"))  # .local wins
            return merged
        if (d / ".git").is_dir():
            break  # project root reached, no env file here
    return {}
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

A capability whose secret is not a flat token resolves it in its own shape — keyed indirectly through a config file, or persisted back after a login exchange — but the **tiers and their order are preserved**: an explicit override beats project config beats user config beats process env, resolved by deterministic code. The shape may vary; the order may not.

## Identity-free

A shared tool bakes in no consumer's identity — no person, company, tenant, account, or host-with-tenant **value** (DOCTRINE rule 10). The consumer supplies those through the cascade; the tool refers to them by role. Config-key *names* (`<NAME>_API_KEY`, `<NAME>_WORKSPACE`) are structural and belong in the script; the *values* never do. This holds in the help text too: examples use placeholders (`user@example.com`, `<PERSON>`), never a real name or address. A default that would otherwise hardcode a consumer (an author or actor name, a default identity) is sourced from env/config with no baked-in fallback — absent the value, the tool asks for it rather than assuming one.

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

`_emit` prints structured results as indented JSON and scalar results (a hash, a job id) as a bare string — a caller can consume either without a schema dance. `_die` prints exactly one shape — `{"error": {"code", "message", "hint"?, "status"?}}` — where `code` is a stable machine token, `message` is human-readable, `hint` is the remediation when one exists, and `status` is the upstream HTTP status when the failure came from a request. stdout stays clean for data; diagnostics never pollute it.

## Exit-code taxonomy

The code tells the caller *which kind* of failure without parsing the message:

| Code | Meaning |
|---|---|
| `0` | success |
| `2` | auth — missing/rejected credentials, 401/403 |
| `3` | not found — the addressed resource does not exist, 404 |
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

`doctor` is the **readiness oracle**. The stub only announces that a tool exists, so "is it wired up *here*?" is `doctor`'s question to answer — at use-time, per context, never inferred from the stub's presence. A failing `doctor` does not just report "not configured"; it names the exact remediation, including *where* the missing config goes — the global `~/.config/<name>/`, or a project-side file the capability self-discovers — so an agent learns how to wire the tool up from the tool itself. A capability whose required config is global (or nil) is usable from any project the moment it is installed; one that needs project-side config is globally *aware* but only *ready* where that config exists, and `doctor` makes the difference explicit.

## Conformance and deviation

These patterns are a strong default, not a cage — the same standing allowance the doctrine grants ([DOCTRINE.md](DOCTRINE.md#deviations-are-allowed--and-recorded)). A capability whose protocol or interaction model genuinely differs realizes the *intent* of a pattern in its own form. Such a deviation is **recorded in the capability's own dedicated deviation file** — a file whose sole purpose is to describe it, kept apart so it is never commingled with other content or accidentally dropped — never here: this standard states the rule, a capability states its own exception, in its own folder. A later audit reads that file first and treats the deviation as a deliberate choice, not drift to fix. The bar is realizing the intent: a deterministic cascade, a clean stdout/stderr split, a stable exit-code contract, a `doctor`, an agent-first help, and no consumer identity baked in.
