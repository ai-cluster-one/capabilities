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
    core:        NAME, SUMMARY, SCOPE, DOCS_BASE, STATE,
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
    .contextkit/config.toml, .env(.local), or .git.
    Markers are read from the filesystem alone: the root is what the envelope
    location is resolved against, so resolving one cannot depend on the other.
    $HOME is never a project root (the machine registry lives there)."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    home = Path.home().resolve()
    for d in (here, *here.parents):
        if d == home:
            return None
        # Either marker names a project. `settings.json` is the gate and
        # `project.json` the identity; a project keeping its records in the
        # store needs the second and may eventually stop carrying the first,
        # so neither is required while both worlds are in use.
        if ((d / "capabilities" / "settings.json").is_file()
                or (d / "capabilities" / "project.json").is_file()
                or (d / ".contextkit" / "config.toml").is_file()
                or (d / ".capabilities").is_dir()
                or (d / ".env").exists()
                or (d / ".env.local").exists() or (d / ".git").is_dir()):
            return d
    return None


_ENVELOPE_HOME: dict = {}


def _validated_project_envelope(root: Path, raw, provider: str) -> Path:
    """Validate the manager/context-owner answer without selecting a fallback."""
    value = str(raw or "").strip()
    if not value:
        _die(6, "project_envelope_empty",
             f"{provider} returned an empty capabilities envelope path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        _die(6, "project_envelope_relative",
             f"{provider} returned a relative capabilities envelope path: {value!r}")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        _die(6, "project_envelope_outside_project",
             f"{provider} returned a capabilities envelope outside the project: {candidate}")
    return resolved


def _manager_project_envelope(root: Path) -> Path:
    """Consume the capabilities manager's one authoritative project answer."""
    override = os.environ.get("CAPABILITIES_PROJECT_ENVELOPE", "").strip()
    if override:
        return _validated_project_envelope(root, override, "environment handoff")

    import subprocess

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    manager = os.environ.get("CAPABILITIES_MANAGER_BIN", "").strip()
    if not manager:
        source_manager = Path(__file__).resolve().parents[3] / "bin" / "capabilities"
        manager = str(source_manager) if source_manager.is_file() else "capabilities"
    try:
        proc = subprocess.run(
            [manager, "path", "--json"], cwd=root, env=env,
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        _die(6, "capabilities_manager_unavailable",
             "the capabilities manager is required to resolve the project envelope",
             "install or repair the `capabilities` command")
    except subprocess.TimeoutExpired:
        _die(6, "capabilities_path_timeout",
             "the capabilities manager did not resolve the project envelope within 10 seconds")
    except OSError as exc:
        _die(6, "capabilities_manager_unavailable",
             f"could not run the capabilities manager: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        try:
            error = json.loads(detail).get("error", {})
        except (AttributeError, ValueError):
            error = {}
        _die(proc.returncode if proc.returncode in (3, 4, 5, 6) else 6,
             str(error.get("code") or "capabilities_path_failed"),
             str(error.get("message") or "the capabilities manager could not resolve the project envelope"),
             str(error.get("hint") or detail or "run `capabilities path --json` for details"))
    try:
        answer = json.loads(proc.stdout)
    except ValueError as exc:
        _die(6, "capabilities_path_malformed",
             f"the capabilities manager returned invalid JSON: {exc}")
    if not isinstance(answer, dict):
        _die(6, "capabilities_path_malformed",
             "the capabilities manager path answer must be a JSON object")
    manager_root = Path(str(answer.get("project_root") or ""))
    if not manager_root.is_absolute() or manager_root.resolve() != root.resolve():
        _die(6, "capabilities_project_mismatch",
             "the capabilities manager resolved a different project root")
    provider = str(answer.get("provider") or "capabilities manager")
    return _validated_project_envelope(
        root, answer.get("project_envelope"), provider)


def _envelope_home(root: Path) -> Path:
    """The project's envelope root, resolved once per invocation.

    The capabilities manager owns project-envelope discovery, including any
    delegation to ContextKit. Capability CLIs consume that answer and never
    duplicate context-owner detection.
    """
    cached = _ENVELOPE_HOME.get(root)
    if cached is not None:
        return cached
    home = _manager_project_envelope(root)
    _ENVELOPE_HOME[root] = home
    return home


def _project_capabilities_dir(root: Path) -> Path:
    """The active project envelope root.

    The resolved envelope is canonical. A legacy `.capabilities/` remains
    readable until `capabilities init` migrates it. If both exist, the canonical
    tree wins once it carries settings.json; otherwise the legacy configured tree
    remains active so merely creating an empty visible directory loses no gate.
    """
    current = _envelope_home(root)
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


_RECORDS = None


def _store_source_label() -> str:
    """A credential-free label for the store that answered.

    Core-only capabilities need this just as much as connection-bearing ones,
    so it belongs with the records adapter rather than in the connections tier.
    """
    url = os.environ.get("CAPABILITIES_STORE_URL") or "?"
    if "://" not in url:
        return f"store(sqlite:{url})"
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return f"store({scheme}://{rest.split('?', 1)[0]})"


def _records():
    """The adapter this project's records are read and written through.

    Everything below takes what this returns and none of it can tell which it
    got. That is the whole of the arrangement: a call site that can tell where
    a record lives is a call site that will eventually decide for itself, and
    then there are two answers to a question that has one."""
    global _RECORDS
    if _RECORDS is None:
        root = _project_root()
        envelope = (_project_capabilities_dir(root) if root is not None
                    else _CONFIG_HOME / "no-project")
        try:
            project_only = os.environ.get("CAPABILITIES_RECORDS_PROJECT_ONLY", "") \
                .strip().lower() in ("1", "true", "yes", "on")
            _RECORDS = open_records(
                envelope, _CONFIG_HOME, project_only=project_only)
        except StoreError as e:
            _die(6, e.slug, e.message, e.hint)
        if _RECORDS.mode == "db":
            _RECORDS.source = _store_source_label()
    return _RECORDS


def _store_mode() -> tuple[str, str]:
    """Where this project keeps its records, as a report rather than a fork.

    Nothing branches on this any more; it is here because a status surface may
    still want to say which source answered. One project may be moved without moving any other, so the answer is
    per project and lives beside the project's identity — it has to be readable
    before the store is reachable.

    The declaration itself is read in one place, `records_mode`, and acted on
    in one place, `open_records`."""
    adapter = _records()
    return (adapter.mode, adapter.source)


def _project_identity() -> dict:
    root = _project_root()
    if root is None:
        _die(6, "no_project", "no project here to resolve records for")
    identity_file = _project_capabilities_dir(root) / "project.json"
    try:
        identity = json.loads(identity_file.read_text())
    except (OSError, ValueError) as e:
        _die(6, "no_project_identity", f"cannot read {identity_file}: {e}",
             "run `capabilities init` in the project")
    if not identity.get("slug"):
        _die(6, "no_project_identity", f"{identity_file} declares no slug")
    return identity


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


def _guide_dir() -> Path:
    executable = Path(__file__).resolve()
    bundle = (executable.parent.parent
              if executable.parent.name == "bin" else executable.parent)
    return bundle / "guides"


def _guide_menu() -> list[dict[str, str]]:
    menu = []
    guide_dir = _guide_dir()
    files = sorted(guide_dir.glob("*.md")) if guide_dir.is_dir() else []
    for path in files:
        lines = path.read_text().splitlines()
        title = ""
        title_index = -1
        for index, line in enumerate(lines):
            if line.startswith("# "):
                title = line[2:].strip()
                title_index = index
                break
        paragraph = []
        for line in lines[title_index + 1:]:
            stripped = line.strip()
            if not stripped:
                if paragraph:
                    break
                continue
            if stripped.startswith("#"):
                break
            paragraph.append(stripped)
        topic = path.stem
        menu.append({
            "topic": topic,
            "title": title,
            "preview": " ".join(paragraph),
            "command": f"{NAME} guide {topic}",
        })
    return menu


def _cmd_guide(argv: list[str]) -> None:
    menu = _guide_menu()
    if not argv:
        _emit(menu); return
    topic = argv[0]
    topics = {entry["topic"] for entry in menu}
    if topic not in topics:
        _die(3, "not_found", f"no guide topic {topic!r}",
             f"run `{NAME} guide` to list available guides")
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


def _identifiers_scopes() -> "Scopes":
    identity = _project_identity()
    return Scopes(project=identity["slug"])


def _identifiers_load() -> dict:
    """The identifiers envelope, from wherever this project keeps its records.

    The shape is the same either way — `{label: {value, note}}` — so the help
    section, the report and `ids get` never learn which answered."""
    try:
        resolved = _records().resolve(NAME, "identifier")
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)
    return {label: {"note": row["note"] or "", "value": row["value"]}
            for label, row in sorted(resolved.items())}


def _identifiers_write(label: str, value, note: str) -> None:
    """Rule 15 holds either way: a capability is the sole writer of its own
    identifiers, addressed by (capability, collection) rather than by path."""
    kept = note or (_identifiers_load().get(label) or {}).get("note", "")
    try:
        _records().set(NAME, "identifier", label, value,
                       actor=f"{NAME} ids set", note=kept or None)
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)


def _identifiers_remove(label: str) -> None:
    try:
        _records().delete(NAME, "identifier", label)
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)


def _identifiers_section() -> str:
    """The Identifiers block appended to the bare top-level help — deterministic
    first-touch surfacing of `capabilities/<NAME>/identifiers.json` so an
    agent following the `<NAME> help` startup protocol loads the discovered
    labels/values/notes into context at once. Empty when no project envelope
    or nothing recorded (do not bloat help with an empty section)."""
    data = _identifiers_load()
    if not data:
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
    data = _identifiers_load()
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
        _identifiers_write(label, value, note)
        _emit({"set": label}); return
    if sub == "rm":
        if len(argv) < 2 or argv[1] not in data:
            _die(3, "not_found", "unknown identifier label",
                 f"`{NAME} ids list` shows the labels")
        _identifiers_remove(argv[1])
        _emit({"removed": argv[1]}); return
    _die(6, "input", f"unknown ids subcommand {sub!r}", f"{NAME} ids list|get|set|rm")


REFERENCE_PREFIX = "reference."


def _front_matter(body: str) -> tuple[str | None, str | None]:
    """`name` and `description` out of a reference's leading front matter."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    name = desc = None
    for line in lines[1:30]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("description:"):
            desc = line.split(":", 1)[1].strip()
    return name, desc


def _reference_bodies() -> dict[str, str]:
    """Every reference this project can see, keyed by its document key.

    In the store a reference is a pinned document like any other long text, so
    an edit reaches every host at once and an unpinned draft reaches none."""
    out: dict[str, str] = {}
    try:
        adapter = _records()
        for key in adapter.document_keys(NAME):
            if not key.startswith(REFERENCE_PREFIX):
                continue
            doc = adapter.document_read(NAME, key)
            if doc:
                out[key] = doc["body"]
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)
    return out


def _cmd_refs(argv: list[str] | None = None) -> None:
    """`refs` lists what this project can load; `refs show <name>` prints one.

    The listing carries a `source` rather than a path, because a reference kept
    in the store has no path to open — `refs show` is the one way to read a
    body that works whichever source answered."""
    argv = argv or []
    bodies = _reference_bodies()
    adapter = _records()
    entries = []
    for key, body in sorted(bodies.items()):
        name, desc = _front_matter(body)
        if not name:
            continue
        entry = {"name": name, "description": desc or "", "key": key}
        # A path when there is one to open, a source when there is not. The
        # adapter answers that; the listing does not ask where it lives.
        path = adapter.document_path(NAME, key)
        if path is None:
            entry["source"] = adapter.source
        else:
            entry["path"] = str(path)
        entries.append((name, entry, body))

    if argv and argv[0] == "show":
        if len(argv) < 2:
            _die(6, "input", f"usage: {NAME} refs show <name>")
        wanted = argv[1]
        for name, entry, body in entries:
            if wanted in (name, entry["key"], entry["key"][len(REFERENCE_PREFIX):]):
                sys.stdout.write(body if body.endswith("\n") else body + "\n")
                return
        _die(3, "not_found", f"no reference named {wanted!r}",
             f"`{NAME} refs` lists them")
    if argv:
        _die(6, "input", f"unknown refs subcommand {argv[0]!r}", f"{NAME} refs [show <name>]")
    _emit([entry for _name, entry, _body in entries])


def _context_edit_files(key: str) -> tuple[Path, Path]:
    token = hashlib.sha256(f"{NAME}\0{key}".encode()).hexdigest()[:16]
    root = _state_dir() / "record-edits"
    return root / f"{token}.md", root / f"{token}.json"


def _cmd_context(argv: list[str]) -> None:
    """Read or edit project long text without exposing its backend."""
    sub = argv[0] if argv else "list"
    adapter = _records()
    if sub == "list":
        _emit({"documents": adapter.document_keys(NAME),
               "records": {"mode": adapter.mode, "source": adapter.source}})
        return
    if len(argv) < 2:
        _die(6, "input", f"usage: {NAME} context show|edit|put <key>")
    key = argv[1]
    if sub == "show":
        doc = adapter.document_read(NAME, key)
        if doc is None:
            _die(3, "not_found", f"no context document {key!r}",
                 f"`{NAME} context list` shows the keys")
        sys.stdout.write(doc["body"] if doc["body"].endswith("\n")
                         else doc["body"] + "\n")
        return
    work, meta = _context_edit_files(key)
    if sub == "edit":
        doc = adapter.document_read(NAME, key)
        direct = adapter.document_path(NAME, key)
        base = doc["hash"] if doc else None
        path = direct or work
        if direct is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(doc["body"] if doc else "")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"key": key, "path": str(path),
                                    "base": base, "direct": direct is not None},
                                   ensure_ascii=False, indent=2) + "\n")
        _emit({"key": key, "path": str(path), "base": base,
               "records": {"mode": adapter.mode, "source": adapter.source},
               "next": f"edit the path, then run `{NAME} context put {key}`"})
        return
    if sub == "put":
        try:
            checkout = json.loads(meta.read_text())
        except (OSError, ValueError) as exc:
            _die(6, "edit_not_started",
                 f"no valid edit checkout for {key!r}: {exc}",
                 f"run `{NAME} context edit {key}` first")
        if checkout.get("key") != key:
            _die(6, "edit_mismatch", f"edit checkout does not belong to {key!r}")
        path = Path(str(checkout.get("path") or ""))
        try:
            body = path.read_text()
            version = adapter.document_put(
                NAME, key, body, author=f"{NAME} context put",
                media_type="text/markdown", base=checkout.get("base"))
        except OSError as exc:
            _die(6, "edit_unreadable", f"cannot read edit path {path}: {exc}")
        except StoreError as exc:
            _die(6, exc.slug, exc.message, exc.hint)
        meta.unlink(missing_ok=True)
        if not checkout.get("direct"):
            path.unlink(missing_ok=True)
        _emit({"put": key, "version": version,
               "records": {"mode": adapter.mode, "source": adapter.source}})
        return
    _die(6, "input", f"unknown context subcommand {sub!r}",
         f"{NAME} context list|show|edit|put")


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
               "docs": {"base": DOCS_BASE,
                        "topics": [entry["topic"] for entry in _guide_menu()]},
               "state": STATE, "post_install": POST_INSTALL})
    elif cmd == "guide":
        _cmd_guide(argv[1:])
    elif cmd == "ids":
        _cmd_ids(argv[1:])
    elif cmd == "refs":
        _cmd_refs(argv[1:])
    elif cmd == "context":
        _cmd_context(argv[1:])
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


def _connections_composed() -> dict:
    """The connections envelope shape, composed out of resolved records.

    Deliberately reconstituted rather than returned in some store-shaped form:
    selection, the write gate and the `connections` report all read the envelope
    shape, and they should not learn where the rows came from. What changes here
    is the source, never the shape.

    The permission a project was granted is folded back onto the entry as
    `allow_write`, which is where the rest of the contract looks for it."""
    adapter = _records()
    try:
        effective = adapter.connections(NAME, write_default=WRITE_DEFAULT)
        default = adapter.get(NAME, "setting", "connection.default")
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)
    if not effective:
        _die(6, "connections_required",
             f"{NAME} requires an explicit connections registry and "
             f"{adapter.source} holds none",
             'expected {"default": "<id>", "connections": {"<id>": { ... }}}')
    return {
        "default": default,
        "connections": {cid: {**entry["value"], "allow_write": entry["allow_write"]}
                        for cid, entry in effective.items()},
    }


def _connections_registry() -> tuple[dict | None, Path | str | None]:
    """The connections envelope and where it came from.

    Composed from the two records a connection is kept as -- who it is, and
    what this project may do with it -- and handed on in the shape the rest of
    the contract already reads, so selection, the write gate and the report
    never learn which source answered."""
    return _connections_composed(), _records().collection_source(NAME, "connection")


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
