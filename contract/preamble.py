"""Canonical contract preamble — the one true copy of the shared plumbing.

Every capability CLI (`capabilities/<name>/bin/<name>`) carries an identical copy
of the helpers between the fence markers below. They are COPIED, never imported:
each `bin/<name>` stays a single self-contained file runnable by `uv run` with no
sibling imports, so a manager update can never break a deployed capability
(SHEBANG.md "spec, never a shared runtime library"; DOCTRINE rule 15).

`capabilities sync-contract` stamps the fenced interior of this file into every
capability; `capabilities audit` byte-compares each script's fenced block to this
one and fails on drift. There is no per-function override mechanism — after the
deviation pre-clean the common set is uniform, so each fence is all-or-nothing and
the drift-check is strict.

TWO TIERS
=========
The preamble has two independent fenced regions:

  - **capability core** — the capability declaration surface (summary/manifest,
    references, guide, ids) plus the file/project/IO plumbing. EVERY capability
    carries it, connection-bearing or not. A capability with no connections takes
    the core fence and stops; that absence is not a deviation, just an absence.

  - **connections** — the credential cascade and connection resolver. Only a
    capability that implements connections carries this fence. Omitting it is the
    normal state for a connection-less capability.

A capability's archetype (API / CLI-wrapper / web-session) lives in the connections
tier; a core-only capability has no archetype because it has no connection.

WHAT EACH CAPABILITY MUST DEFINE *ABOVE* THE FENCES (the vendored blocks read
these module-level names; they are the only coupling):
    core:        NAME, SUMMARY, SCOPE, DOCS_BASE, TOPICS, STATE,
                 POST_INSTALL, _CONFIG_HOME, _STATE_HOME
    connections: CREDENTIALS_ENV, CRED_KEYS, WRITE_VERBS, WRITE_DEFAULT
    plus the stdlib imports the helpers use: os, sys, json, Path, NoReturn.

The bare `help` verb is dispatched by `_contract` and reads the CLI's help
body from a module-level `HELP` constant if defined, else the module docstring
(`__doc__`) — either wire is acceptable, and every capability already carries
one or the other. It then appends the project's identifiers as a labelled
section rendered by `_render_ids_markdown` (the same format the manager's
`capabilities ids <NAME>` produces, so one home for the rendering).

`_contract`'s `connections` verb calls `_cmd_connections`, and every capability
defines its own `_cmd_connections` (its no-registry branch names that capability's
own primary key, or reports "no connections" for a core-only capability) — so
`_cmd_connections` stays OUTSIDE the fences, per capability. Likewise per-capability
(shape varies, never vendored): `_build_conn`/`_load_config`, `_resolve_conn`,
`_check_connection`, the doctor command, the declaration constants, the domain
verbs, argparse/`main`.

TWO DELIBERATE BEST-OF-BREED CHOICES baked into the canonical bodies below:
  - `_emit` is the empty-string-guarded variant (asana/callva/notion/telegram).
  - `_select_connection` also matches a connection by its own `address` field
    (generalized from mailbox); the loop never fires for connections without an
    `address`, so it is a no-op for every other capability.

This file is documentation above the opening fences; the codegen stamps only the
interiors. Edit the helpers here, then run `capabilities sync-contract`.
"""

# >>> contract: capability core (generated — edit contract/preamble.py, run `capabilities sync-contract`) >>>

# --- Error reporting ---------------------------------------------------------

def _die(exit_code: int, code: str, message: str,
         hint: str | None = None, status: int | None = None) -> NoReturn:
    err: dict = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    if status is not None:
        err["status"] = status
    sys.stderr.write(json.dumps({"error": err}, ensure_ascii=False) + "\n")
    sys.exit(exit_code)


def _emit(value) -> None:
    if isinstance(value, str):
        sys.stdout.write(value)
        if value and not value.endswith("\n"):
            sys.stdout.write("\n")
    else:
        sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


# --- Project / file plumbing -------------------------------------------------

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


def _project_root() -> Path | None:
    """Nearest project root, walking up from $CLAUDE_PROJECT_DIR (else cwd):
    the first directory holding capabilities/, legacy .capabilities/,
    .env(.local), or .git.
    $HOME is never a project root (the machine registry lives there)."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    home = Path.home().resolve()
    for d in (here, *here.parents):
        if d == home:
            return None
        if ((d / "capabilities" / "settings.json").is_file()
                or (d / ".capabilities").is_dir()
                or (d / ".env").exists()
                or (d / ".env.local").exists() or (d / ".git").is_dir()):
            return d
    return None


def _project_capabilities_dir(root: Path) -> Path:
    """The active project envelope root.

    `capabilities/` is canonical. A legacy `.capabilities/` remains readable
    until `capabilities init` migrates it. If both exist, the canonical tree
    wins once it carries settings.json; otherwise the legacy configured tree
    remains active so merely creating an empty visible directory loses no gate.
    """
    current = root / "capabilities"
    legacy = root / ".capabilities"
    if (current / "settings.json").is_file() or not legacy.is_dir():
        return current
    return legacy


def _project_env() -> dict:
    """Project .env(.local) at the project root. .env.local overrides .env."""
    root = _project_root()
    if root is None:
        return {}
    merged = _parse_env_file(root / ".env")
    merged.update(_parse_env_file(root / ".env.local"))  # .local wins
    return merged


def _auth_context() -> dict | None:
    """Optional runtime authority envelope passed by an ingress/service.

    Absence means the ordinary project gate is the whole policy. Presence means
    a request-scoped authority layer exists and must fail closed before any
    credential or network work happens.
    """
    raw = os.environ.get("CAPABILITIES_AUTH_CONTEXT")
    if not raw:
        return None
    try:
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
        else:
            data = json.loads(Path(raw).read_text())
    except (OSError, ValueError) as e:
        _die(4, "auth_context_unreadable",
             "runtime authority context could not be read",
             f"{raw}: {e}")
    if not isinstance(data, dict):
        _die(4, "auth_context_invalid",
             "runtime authority context is not an object")
    return data


def _auth_capability_allowed(rule) -> bool:
    if rule is True or rule == "*":
        return True
    if rule in (False, None):
        return False
    if isinstance(rule, dict):
        if rule.get("deny") is True:
            return False
        if rule.get("enabled") is False or rule.get("allow") is False:
            return False
        return True
    return False


def _auth_gate() -> None:
    ctx = _auth_context()
    if ctx is None:
        return
    allowed = ctx.get("allowed_capabilities")
    if allowed is None:
        return
    if allowed is True or allowed == "*":
        return
    if isinstance(allowed, list):
        if NAME in allowed or "*" in allowed:
            return
    elif isinstance(allowed, dict):
        if _auth_capability_allowed(allowed.get(NAME, allowed.get("*"))):
            return
    else:
        _die(4, "auth_context_invalid",
             "runtime authority context has invalid allowed_capabilities")
    source = ctx.get("source") or "runtime"
    role = ctx.get("sender_role") or ctx.get("role") or "unknown"
    chat = ctx.get("chat_id") or "unknown"
    _die(4, "capability_not_authorized",
         f"{NAME} is not authorized for this {source} request",
         f"role={role}; chat_id={chat}; adjust runtime authority policy instead of bypassing the gate")


def _policy_entry(path: Path):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        _die(6, "bad_policy", f"cannot read capability policy {path}: {e}")
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    if not isinstance(capabilities, dict):
        _die(6, "bad_policy", f"{path} is not a capability policy",
             'expected {"capabilities": {"<name>": {"enabled": true}}}')
    entry = capabilities.get(NAME)
    if entry is not None and (not isinstance(entry, dict)
                              or set(entry) != {"enabled"}
                              or not isinstance(entry.get("enabled"), bool)):
        _die(6, "bad_policy", f"invalid {NAME} policy entry in {path}",
             'expected {"enabled": true|false}')
    return entry


def _project_enabled_explicitly() -> bool:
    """True only for an explicit project enabled entry; global inheritance is
    deliberately insufficient for project-owned service activation."""
    root = _project_root()
    if root is None:
        return False
    entry = _policy_entry(_project_capabilities_dir(root) / "settings.json")
    return isinstance(entry, dict) and entry.get("enabled") is True


def _require_project_enabled_for_service() -> Path:
    root = _project_root()
    if root is None or not _project_enabled_explicitly():
        _die(4, "project_enable_required",
             f"{NAME} service activation requires an explicit project enable",
             f"ask the user, then run `capabilities enable {NAME} --project` "
             "before service init/start/run")
    return root


def _gate() -> None:
    """Project policy overrides global policy; absence inherits, and absence at
    both scopes is default deny. Safe local discovery verbs remain available so
    a disabled capability can be understood and configured before use.

    If an ingress supplied CAPABILITIES_AUTH_CONTEXT, that request-scoped gate
    is stricter and runs first, before credentials or network calls.
    """
    _auth_gate()
    verb = sys.argv[1] if len(sys.argv) > 1 else "help"
    service_action = sys.argv[2] if len(sys.argv) > 2 else None
    service_contract = globals().get("SERVICE")
    if isinstance(service_contract, dict) and verb == "service" \
            and service_action in {"init", "start", "run"}:
        _require_project_enabled_for_service()
    if verb in {"help", "stub", "manifest", "connections"}:
        return
    root = _project_root()
    project_file = (_project_capabilities_dir(root) / "settings.json") \
        if root is not None else None
    project_entry = _policy_entry(project_file) if project_file else None
    global_file = _CONFIG_HOME / "capabilities" / "settings.json"
    global_entry = _policy_entry(global_file)
    if isinstance(project_entry, dict) and project_entry.get("enabled") is True:
        return
    if isinstance(project_entry, dict) and project_entry.get("enabled") is False:
        _die(4, "disabled",
             f"{NAME} is disabled in this project ({project_file})",
             f"ask the user whether to enable {NAME} for this project; if yes, "
             f"run `capabilities enable {NAME} --project`")
    if isinstance(global_entry, dict) and global_entry.get("enabled") is True:
        return
    if isinstance(global_entry, dict) and global_entry.get("enabled") is False:
        message = f"{NAME} is disabled globally ({global_file})"
    else:
        message = f"{NAME} is not enabled by project or global policy"
    _die(4, "not_enabled", message,
         f"ask the user whether to enable {NAME} only for this project or "
         f"globally for every project; then run `capabilities enable {NAME} "
         "--project` or `--global` exactly as requested")


def _env_dir() -> Path | None:
    """The capability's envelope dir in the project's active capabilities/."""
    root = _project_root()
    return (_project_capabilities_dir(root) / NAME) if root is not None else None


def _state_dir() -> Path:
    """State follows the scope of the credentials that minted it; project state
    lands inside the envelope only where capabilities/ already exists."""
    if SCOPE == "project":
        root = _project_root()
        if root is not None:
            envelope = _project_capabilities_dir(root)
            if (envelope / "settings.json").is_file():
                return envelope / NAME / "state"
    return _STATE_HOME / NAME


# --- Capability contract — the declaration surface --------------------------

def _docs_base() -> str:
    key = f"{NAME.upper()}_DOCS_BASE"
    return (_project_env().get(key)
            or _parse_env_file(CREDENTIALS_ENV).get(key)
            or os.environ.get(key)
            or DOCS_BASE)


def _cmd_guide(argv: list[str]) -> None:
    if not argv:
        _emit(sorted(TOPICS)); return
    topic = argv[0]
    if topic not in TOPICS:
        _die(3, "not_found", f"no guide topic {topic!r}",
             f"topics: {', '.join(sorted(TOPICS)) or 'none shipped'}")
    import urllib.error as _ue
    import urllib.request as _ur
    url = _docs_base().rstrip("/") + "/" + topic + ".md"
    cache_dir = _STATE_HOME / NAME / "guides"
    cache, etag_f = cache_dir / f"{topic}.md", cache_dir / f"{topic}.etag"
    headers = {}
    if cache.exists() and etag_f.exists():
        headers["If-None-Match"] = etag_f.read_text().strip()
    try:
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=10.0) as resp:
            text = resp.read().decode(resp.headers.get_content_charset() or "utf-8")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(text)
            if resp.headers.get("etag"):
                etag_f.write_text(resp.headers["etag"])
            _emit(text); return
    except _ue.HTTPError as e:
        if e.code == 304 and cache.exists():
            _emit(cache.read_text()); return
        if e.code == 404:
            _die(3, "not_found", f"guide {topic!r} missing upstream", url)
        err = f"upstream returned {e.code}"
    except _ue.URLError as e:
        err = str(e.reason)
    except OSError as e:
        err = str(e)
    if cache.exists():
        sys.stderr.write(json.dumps(
            {"warning": f"upstream unreachable; serving cached copy ({err})"},
            ensure_ascii=False) + "\n")
        _emit(cache.read_text()); return
    _die(5, "network_error", f"could not fetch guide {topic!r}; no cache exists", url)


def _render_ids_markdown(data: dict) -> str:
    """Render the {label: {value, note}} envelope as the labelled markdown
    list the manager's `capabilities ids <NAME>` also produces — one shared
    format across the ids surface and the identifiers section of `help`."""
    lines: list[str] = []
    for label, entry in sorted(data.items()):
        entry = entry if isinstance(entry, dict) else {"value": entry}
        v = entry.get("value")
        vs = f"`{v}`" if isinstance(v, str) else "`" + json.dumps(v, ensure_ascii=False) + "`"
        line = f"- **{label}**: {vs}"
        note = entry.get("note")
        if note:
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)


def _identifiers_section() -> str:
    """The Identifiers block appended to the bare top-level help — deterministic
    first-touch surfacing of `capabilities/<NAME>/identifiers.json` so an
    agent following the `<NAME> help` startup protocol loads the discovered
    labels/values/notes into context at once. Empty when no project envelope
    or nothing recorded (do not bloat help with an empty section)."""
    envdir = _env_dir()
    if envdir is None:
        return ""
    ids_file = envdir / "identifiers.json"
    if not ids_file.exists():
        return ""
    try:
        data = json.loads(ids_file.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or not data:
        return ""
    header = (
        "\n═══ Identifiers ═════════════════════════════════════════════════════════════\n\n"
        f"Structural lookups discovered for `{NAME}` in this project. Fetch a single\n"
        f"raw value with `{NAME} ids get <label>`; the full envelope is also at\n"
        f"`capabilities ids {NAME}`.\n\n"
    )
    return header + _render_ids_markdown(data) + "\n"


def _cmd_help() -> None:
    """Bare top-level help: the CLI's HELP body (module-level `HELP` if
    defined, else the module docstring), then the project's Identifiers
    section. Only fires when no extra args follow — per-verb help stays
    untouched so `<NAME> help <subcommand>` is not intercepted."""
    g = globals()
    text = g.get("HELP") or g.get("__doc__") or ""
    if text.startswith("\n"):
        text = text.lstrip("\n")
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    section = _identifiers_section()
    if section:
        sys.stdout.write(section)


def _cmd_ids(argv: list[str]) -> None:
    sub = argv[0] if argv else "list"
    envdir = _env_dir()
    ids_file = (envdir / "identifiers.json") if envdir else None
    data: dict = {}
    if ids_file and ids_file.exists():
        try:
            data = json.loads(ids_file.read_text())
        except ValueError:
            _die(6, "bad_envelope", f"{ids_file} is not valid JSON")
    if sub == "list":
        _emit(data); return
    if sub == "get":
        if len(argv) < 2 or argv[1] not in data:
            _die(3, "not_found", "unknown identifier label",
                 f"`{NAME} ids list` shows the labels")
        _emit((data[argv[1]] or {}).get("value")); return
    if sub == "set":
        if len(argv) < 3:
            _die(6, "input", f"usage: {NAME} ids set <label> <value> [--note <text>]")
        if envdir is None or not (envdir.parent / "settings.json").is_file():
            _die(6, "no_envelope", "no capabilities/ envelope in this project",
                 "run `capabilities init` first")
        label, raw = argv[1], argv[2]
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        note = ""
        if "--note" in argv:
            ni = argv.index("--note")
            if ni + 1 >= len(argv):
                _die(6, "input", "--note needs a value")
            note = argv[ni + 1]
        data[label] = {"value": value, "note": note or (data.get(label) or {}).get("note", "")}
        envdir.mkdir(parents=True, exist_ok=True)
        ids_file.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _emit({"set": label}); return
    if sub == "rm":
        if len(argv) < 2 or argv[1] not in data:
            _die(3, "not_found", "unknown identifier label",
                 f"`{NAME} ids list` shows the labels")
        del data[argv[1]]
        ids_file.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _emit({"removed": argv[1]}); return
    _die(6, "input", f"unknown ids subcommand {sub!r}", f"{NAME} ids list|get|set|rm")


def _cmd_refs() -> None:
    envdir = _env_dir()
    refdir = (envdir / "reference") if envdir else None
    out = []
    if refdir and refdir.is_dir():
        for md in sorted(refdir.glob("*.md")):
            name = desc = None
            try:
                lines = md.read_text().splitlines()
            except OSError:
                continue
            if lines and lines[0].strip() == "---":
                for line in lines[1:30]:
                    if line.strip() == "---":
                        break
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip()
            if name:
                out.append({"name": name, "description": desc or "", "path": str(md)})
    _emit(out)


def _contract(argv: list[str]) -> None:
    """Dispatch the contract verbs; domain verbs fall through to the CLI's own
    parser. Runs after _gate(), before any credential is resolved.

    `help` is a contract verb only when bare (no extra args) — the top-level
    dump plus the Identifiers section. `<NAME> help <subcommand>` falls
    through to the CLI so per-verb help stays clean."""
    cmd = argv[0] if argv else ""
    if cmd == "stub":
        _emit(f"{SUMMARY} Run `{NAME} help`.")
    elif cmd == "manifest":
        _emit({"name": NAME, "summary": SUMMARY,
               "credentials": {"scope": SCOPE, "keys": CRED_KEYS},
               "docs": {"base": DOCS_BASE, "topics": sorted(TOPICS)},
               "state": STATE, "post_install": POST_INSTALL})
    elif cmd == "guide":
        _cmd_guide(argv[1:])
    elif cmd == "ids":
        _cmd_ids(argv[1:])
    elif cmd == "refs":
        _cmd_refs()
    elif cmd == "connections":
        _cmd_connections()
    elif cmd == "help" and len(argv) == 1:
        _cmd_help()
    else:
        return
    sys.exit(0)

# <<< contract: capability core <<<


# >>> contract: connections (generated — edit contract/preamble.py, run `capabilities sync-contract`) >>>

def _resolve_env_key(key: str) -> tuple[str | None, str | None, Path | None]:
    """Resolve one env key through cascade tiers 2-4: project .env(.local) →
    user credentials.env → process env. Returns (value, tier, source)."""
    root = _project_root()
    if root is not None:
        for fname in (".env.local", ".env"):
            val = _parse_env_file(root / fname).get(key)
            if val:
                return val, "project", root / fname
    val = _parse_env_file(CREDENTIALS_ENV).get(key)
    if val:
        return val, "user", CREDENTIALS_ENV
    val = os.environ.get(key)
    if val:
        return val, "env", None
    return None, None, None


def _mask(value: str) -> str:
    return ("…" + value[-4:]) if len(value) >= 8 else "****"


def _connections_registry() -> tuple[dict | None, Path | None]:
    """The connections envelope and its path: project envelope first, else the
    user config home. First found wins, never merged. Every connection-bearing
    capability requires a registry, even when it declares only one connection."""
    envdir = _env_dir()
    candidates = ([envdir / "connections.json"] if envdir else []) + \
                 [_CONFIG_HOME / NAME / "connections.json"]
    for reg_file in candidates:
        if not reg_file.is_file():
            continue
        try:
            raw = json.loads(reg_file.read_text())
        except (ValueError, OSError) as e:
            _die(6, "bad_config", f"cannot read {reg_file}: {e}")
        if not isinstance(raw, dict) or not isinstance(raw.get("connections"), dict) \
                or not raw["connections"]:
            _die(6, "bad_config", f"{reg_file} is not a connections envelope",
                 'expected {"default": "<id>", "connections": { ... }}')
        return raw, reg_file
    project_path = (envdir / "connections.json") if envdir else None
    homes = ([str(project_path)] if project_path else []) + \
            [str(_CONFIG_HOME / NAME / "connections.json")]
    _die(6, "connections_required",
         f"{NAME} requires an explicit connections registry",
         "create one of: " + ", ".join(homes) +
         '; expected {"default": "<id>", "connections": {"<id>": { ... }}}')


def _select_connection(reg: dict | None, wanted: str | None) -> tuple[str, dict | None]:
    """flag → default pointer → sole entry → die 6. A connection's own
    `address` field selects it too (used where a
    connection carries a human-recognizable address; absent fields never match)."""
    if reg is None:
        _die(6, "connections_required",
             f"{NAME} requires an explicit connections registry")
    conns = reg["connections"]
    if wanted:
        if wanted in conns:
            return wanted, conns[wanted]
        for cid, entry in conns.items():
            if (entry or {}).get("address", "").lower() == wanted.lower():
                return cid, entry
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
         f"registry defines {len(conns)} connections and no default; "
         f"pass --connection <id>",
         f"known: {', '.join(conns)}")


def _write_gate(conn_id: str, allow_write: bool, verb: str) -> None:
    """Policy from the committed registry; nothing in the cascade lifts it."""
    if verb in WRITE_VERBS and not allow_write:
        _die(4, "read_only",
             f"connection {conn_id!r} does not allow writes",
             "Do not lift the gate yourself — ask the user; granting is "
             "`allow_write: true` on this connection in connections.json.")


def _key_report(key: str, secret: bool, required: bool,
                value: str | None, tier: str | None, source) -> dict:
    return {"key": key, "secret": secret, "required": required,
            "set": bool(value), "tier": tier if value else None,
            "source": str(source) if (value and source) else None,
            "value": (_mask(value) if secret else value) if value else None}


def _missing_required(keys: list) -> list:
    """Required report rows (from _key_report) that did not resolve through the
    cascade — empty ⇒ credentials present. The primitive behind doctor's
    network-free readiness gate, read from the same per-key resolution
    `connections` reports, so the gate and the report can never disagree."""
    return [k["key"] for k in keys if k["required"] and not k["set"]]


def _doctor_gate(report: dict, wanted: str | None) -> None:
    """doctor's network-free readiness gate. Refuse with exit 2 — naming the
    unresolved required keys, before any round-trip — when a connection under
    test cannot resolve its required config through the cascade, so readiness is
    judged by the same resolution `connections` reports, never a parallel check.
    Checks the selected connection, else every connection in the report."""
    conns = report.get("connections") or {}
    targets = [wanted] if (wanted and wanted in conns) else list(conns)
    problems: dict = {}
    for cid in targets:
        miss = _missing_required((conns.get(cid) or {}).get("keys") or [])
        if miss:
            problems[cid] = miss
    if problems:
        detail = "; ".join(f"{c}: {', '.join(ks)}" for c, ks in sorted(problems.items()))
        _die(2, "credentials_missing",
             f"unresolved required config — {detail}",
             f"set each in the project .env/.env.local, the user credentials.env, "
             f"or process env; `{NAME} connections` shows where every value resolves")

# <<< contract: connections <<<
