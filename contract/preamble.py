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

THREE TIERS
===========
The contract has three fenced regions; the store tier is canonical in
`contract/store.py` because it is also imported by bundled services:

  - **capability core** — the capability declaration surface (summary/manifest,
    references, guide, ids) plus the file/project/IO plumbing. EVERY capability
    carries it, connection-bearing or not.

  - **store** — the records adapter used by identifiers, policy, settings and
    operational state. EVERY capability carries it because core calls into it.

  - **connections** — the credential cascade and connection resolver. Only a
    capability that implements connections carries this fence. Omitting it is the
    normal state for a connection-less capability.

A capability's archetype (API / CLI-wrapper / web-session) lives in the connections
tier; a connection-less capability has no archetype because it has no connection.

WHAT EACH CAPABILITY MUST DEFINE *ABOVE* THE FENCES (the vendored blocks read
these module-level names; they are the only coupling):
    core/store:  NAME, SUMMARY, SCOPE, DOCS_BASE, STATE, INVENTORY,
                 POST_INSTALL, _CONFIG_HOME, _STATE_HOME
    connections: CREDENTIALS_ENV, CRED_KEYS, WRITE_VERBS, WRITE_DEFAULT
    plus the stdlib imports the helpers use: os, sys, json, Path, NoReturn.
Optionally, beside WRITE_DEFAULT, `MACHINE_READS`: a tuple of the read verbs
that, outside any project, use the machine's own connections read-only.
Absent or empty means none; the manager validates it where it validates a
capability.
Optionally, beside STATE, `TABLES = True` when the capability keeps tables in
the machine's store, with a `_tables()` builder outside the fences returning
`(steps, major, minor)` for the shared database library. Absent or false means
none, and `migrate` is then left to the CLI's own parser.

The bare `help` verb is dispatched by `_contract` and reads the CLI's help
body from a module-level `HELP` constant if defined, else the module docstring
(`__doc__`) — either wire is acceptable, and every capability already carries
one or the other. It then appends the project's identifiers as a labelled
section rendered by `_render_ids_markdown` (the same format the manager's
`capabilities ids <NAME>` produces, so one home for the rendering).

`_contract`'s `inventory` verb calls `_inventory(network)` only when the
capability declares `INVENTORY`; that builder is per-capability and stays OUTSIDE
the fences, while the envelope, the flag parsing and the empty answer are vendored
so every capability reports in one shape.

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


def _home_dirs() -> set[Path]:
    """Every directory that is a home directory of the account running this.

    $HOME answers for the process and moves wherever a caller points it; the
    password database answers for the account and cannot be pointed anywhere.
    A caller that redirects $HOME - a dev lane, a service under a supervisor, a
    sandbox - would otherwise carry the guard onto the home it invented and stop
    excluding the account's own, which is the one the machine registry sits in.
    """
    import pwd

    homes: set[Path] = set()
    candidates = [Path.home()]
    try:
        account = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        account = ""
    # The account's field counts only when it is absolute. An empty or relative
    # one names no directory of its own: it resolves against the cwd, an empty
    # one to the cwd itself, so taken as a home it would stop the walk wherever
    # the process stands. Some service accounts carry an empty field.
    if os.path.isabs(account):
        candidates.append(Path(account))
    for candidate in candidates:
        try:
            homes.add(candidate.resolve())
        except OSError:
            continue
    return homes


def _is_machine_registry(path: Path) -> bool:
    """Whether this `.capabilities/` is a machine registry, not a legacy envelope.

    The two share the name, and only the registry carries `.manager/`. The
    registry $CAPABILITIES_HOME points at names itself even without it.
    """
    registry = Path(os.environ.get("CAPABILITIES_HOME") or (Path.home() / ".capabilities"))
    try:
        if path.resolve() == registry.resolve():
            return True
    except OSError:
        return False
    return (path / ".manager").is_dir()


def _project_root() -> Path | None:
    """Nearest project root, walking up from $CLAUDE_PROJECT_DIR (else cwd):
    the first directory holding capabilities/, legacy .capabilities/,
    .contextkit/config.toml, .env(.local), or .git.
    Markers are read from the filesystem alone: the root is what the envelope
    location is resolved against, so resolving one cannot depend on the other.
    A home directory is never a project root (the machine registry lives in
    one), and a `.capabilities/` that is a machine registry names that registry
    rather than a legacy envelope."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    homes = _home_dirs()
    for d in (here, *here.parents):
        if d in homes:
            return None
        legacy = d / ".capabilities"
        # Either marker names a project. `settings.json` is the gate and
        # `project.json` the identity; a project keeping its records in the
        # store needs the second and may eventually stop carrying the first,
        # so neither is required while both worlds are in use.
        if ((d / "capabilities" / "settings.json").is_file()
                or (d / "capabilities" / "project.json").is_file()
                or (d / ".contextkit" / "config.toml").is_file()
                or (legacy.is_dir() and not _is_machine_registry(legacy))
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


def _handed_off_envelope(root: Path) -> str:
    """The handoff meant for THIS project root, or "" when there is none.

    `CAPABILITIES_PROJECT_ENVELOPE` is inherited by every descendant process, and
    a descendant does not always stand in the project its parent stood in: a peer
    spawned in another project, or a worker whose cwd sits under a different root,
    must resolve its own envelope. `CAPABILITIES_PROJECT_ENVELOPE_ROOT` names the
    root the handed value answers for, so a handoff that crossed a project
    boundary is passed over instead of being applied to the wrong project. A
    handoff supplied without that companion belongs to whoever launched the
    process and is honored as given, which is how a context owner hands its answer
    over in advance.
    """
    override = os.environ.get("CAPABILITIES_PROJECT_ENVELOPE", "").strip()
    if not override:
        return ""
    scope = os.environ.get("CAPABILITIES_PROJECT_ENVELOPE_ROOT", "").strip()
    if not scope:
        return override
    try:
        if Path(scope).expanduser().resolve() != root.resolve():
            return ""
    except OSError:
        return ""
    return override


ENVELOPE_CACHE_VERSION = 1
IDENTITY_CACHE_VERSION = 1


def _record_file(kind: str, key: str) -> Path:
    """Where the manager records one kind of answer for one absolute project root.

    The file name only buckets roots apart; the entry's own `root` field is what
    proves identity, so two roots landing in one bucket cost a miss rather than
    a wrong answer.
    """
    import zlib

    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return (base / "capabilities" / kind
            / f"{format(zlib.crc32(key.encode()), '08x')}.json")


def _envelope_witness(path) -> dict:
    """One input a recorded answer rests on, as that input stands right now."""
    try:
        seen = os.stat(path)
    except OSError:
        return {"path": str(path), "present": False}
    return {"path": str(path), "present": True,
            "size": seen.st_size, "mtime_ns": seen.st_mtime_ns}


def _cached_record(kind: str, version: int, root: Path):
    """The recorded entry for this root while every input it rests on holds.

    The manager writes the record and names the inputs its answer consulted; a
    reader re-observes each of them and compares it whole. Anything at all in
    doubt — an unreadable file, an unexpected shape, an input that moved, an
    expiry already behind the clock or a write ahead of it — returns None and
    costs a full resolution, so the failure mode is slow and never wrong.
    """
    import time

    key = str(root.resolve())
    try:
        entry = json.loads(_record_file(kind, key).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("version") != version or entry.get("root") != key:
        return None
    written, expires = entry.get("written_at"), entry.get("expires_at")
    if not isinstance(written, (int, float)) or not isinstance(expires, (int, float)):
        return None
    now = time.time()
    if now < written or now >= expires:
        return None
    witness = entry.get("witness")
    if not isinstance(witness, list):
        return None
    for observed in witness:
        if (not isinstance(observed, dict)
                or _envelope_witness(observed.get("path")) != observed):
            return None
    return entry


def _cached_envelope(root: Path):
    """The recorded envelope for this root, while its record stands."""
    entry = _cached_record("envelope", ENVELOPE_CACHE_VERSION, root)
    if entry is None:
        return None
    envelope = Path(str(entry.get("envelope") or ""))
    if not envelope.is_absolute():
        return None
    try:
        envelope.resolve().relative_to(entry["root"])
    except (OSError, ValueError):
        return None
    return envelope


def _ask_manager(root: Path, *flags: str) -> tuple:
    """`capabilities path --json` for this root: (answer, None) or (None, error).

    The error carries the exit code the caller should use and the manager's own
    code, message and hint where it gave them."""
    import subprocess

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    # Asked at all, the manager answers from ContextKit: a live id hand-down for
    # this root is used before asking, so any in the environment is this
    # process's own and must not come back as the answer.
    env.pop("CAPABILITIES_PROJECT_ID", None)
    env.pop("CAPABILITIES_PROJECT_ID_ROOT", None)
    manager = os.environ.get("CAPABILITIES_MANAGER_BIN", "").strip()
    if not manager:
        source_manager = Path(__file__).resolve().parents[3] / "bin" / "capabilities"
        manager = str(source_manager) if source_manager.is_file() else "capabilities"
    try:
        proc = subprocess.run(
            [manager, "path", "--json", *flags], cwd=root, env=env,
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return None, {"exit": 6, "code": "capabilities_manager_unavailable",
                      "message": "the capabilities manager is required to resolve the project",
                      "hint": "install or repair the `capabilities` command"}
    except subprocess.TimeoutExpired:
        return None, {"exit": 6, "code": "capabilities_path_timeout",
                      "message": "the capabilities manager did not resolve the project within 10 seconds"}
    except OSError as exc:
        return None, {"exit": 6, "code": "capabilities_manager_unavailable",
                      "message": f"could not run the capabilities manager: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        try:
            error = json.loads(detail).get("error", {})
        except (AttributeError, ValueError):
            error = {}
        return None, {"exit": proc.returncode if proc.returncode in (3, 4, 5, 6) else 6,
                      "code": str(error.get("code") or "capabilities_path_failed"),
                      "message": str(error.get("message") or "the capabilities manager could not resolve the project"),
                      "hint": str(error.get("hint") or detail or "run `capabilities path --json` for details")}
    try:
        answer = json.loads(proc.stdout)
    except ValueError as exc:
        return None, {"exit": 6, "code": "capabilities_path_malformed",
                      "message": f"the capabilities manager returned invalid JSON: {exc}"}
    if not isinstance(answer, dict):
        return None, {"exit": 6, "code": "capabilities_path_malformed",
                      "message": "the capabilities manager path answer must be a JSON object"}
    manager_root = Path(str(answer.get("project_root") or ""))
    if not manager_root.is_absolute() or manager_root.resolve() != root.resolve():
        return None, {"exit": 6, "code": "capabilities_project_mismatch",
                      "message": "the capabilities manager resolved a different project root"}
    return answer, None


def _manager_project_envelope(root: Path) -> Path:
    """Consume the capabilities manager's one authoritative project answer."""
    override = _handed_off_envelope(root)
    if override:
        return _validated_project_envelope(root, override, "environment handoff")
    answer, error = _ask_manager(root)
    if error:
        _die(error["exit"], error["code"], error["message"], error.get("hint"))
    provider = str(answer.get("provider") or "capabilities manager")
    return _validated_project_envelope(
        root, answer.get("project_envelope"), provider)


def _envelope_home(root: Path) -> Path:
    """The project's envelope root, resolved once per invocation.

    The capabilities manager owns project-envelope discovery, including any
    delegation to ContextKit. Capability CLIs consume that answer and never
    duplicate context-owner detection.

    The answer is then handed to this process's descendants, scoped to the root
    it answers for. Asking for it costs two interpreter starts — the manager and
    the context owner it delegates to — so a worker, a peer, or a nested
    capability call that would otherwise repeat the whole chain under whatever
    load the machine is under by then reads it instead.

    A fresh top-level call has no such handoff, and reads the manager's recorded
    answer for this root instead, which stands only while every input that answer
    rests on is unchanged. A live handoff outranks the record, and any doubt about
    the record asks the manager again.
    """
    cached = _ENVELOPE_HOME.get(root)
    if cached is not None:
        return cached
    home = None if _handed_off_envelope(root) else _cached_envelope(root)
    if home is None:
        home = _manager_project_envelope(root)
    _ENVELOPE_HOME[root] = home
    os.environ["CAPABILITIES_PROJECT_ENVELOPE"] = str(home)
    os.environ["CAPABILITIES_PROJECT_ENVELOPE_ROOT"] = str(root)
    # The id travels with the envelope: resolved at read level, handed down
    # only where a write may stamp it, and never a reason to refuse here.
    try:
        _project_id_state_at(root)
    except Exception:
        pass
    return home


def _project_capabilities_dir(root: Path) -> Path:
    """The active project envelope root.

    The resolved envelope is canonical. A legacy `.capabilities/` remains
    readable until `capabilities init` migrates it. If both exist, the canonical
    tree wins once it carries either the legacy file gate or the project identity;
    otherwise the legacy configured tree remains active so merely creating an
    empty visible directory loses no gate.
    """
    current = _envelope_home(root)
    legacy = root / ".capabilities"
    if ((current / "settings.json").is_file()
            or (current / "project.json").is_file()
            or not legacy.is_dir()):
        return current
    return legacy


_CONTEXT_IDENTITY: dict = {}


def _context_identity(root: Path) -> dict:
    """Whether a context owner binds this project, and the id it holds for it.

    The capabilities manager owns the answer, as it owns the envelope's: this
    reads the manager's record while every input it rests on stands, and asks
    the manager otherwise. `{"bound", "id"}`, or `{"error"}` when the manager
    cannot answer, which leaves reads on project.json and refuses writes."""
    root = root.resolve()
    known = _CONTEXT_IDENTITY.get(root)
    if known is not None:
        return known
    if not (root / ".contextkit" / "config.toml").is_file():
        # The binding the manager asks ContextKit about is absent, so the
        # manager's answer is already known: unbound, with no id of its own.
        _CONTEXT_IDENTITY[root] = {"bound": False, "id": None}
        return _CONTEXT_IDENTITY[root]
    entry = _cached_record("identity", IDENTITY_CACHE_VERSION, root)
    if (entry is not None and isinstance(entry.get("bound"), bool)
            and (entry.get("id") is None or isinstance(entry.get("id"), str))):
        answer = {"bound": entry["bound"], "id": entry.get("id")}
    else:
        reply, error = _ask_manager(root, "--identity")
        state = (reply or {}).get("project_identity")
        if error or not isinstance(state, dict) or not isinstance(state.get("bound"), bool):
            answer = {"error": error or {
                "code": "capabilities_path_malformed",
                "message": "the capabilities manager returned no project identity"}}
        else:
            answer = {"bound": state["bound"], "id": state.get("contextkit_id") or None}
    _CONTEXT_IDENTITY[root] = answer
    return answer


def _identity_verdict(bound: bool, contextkit_id, cached_id) -> dict:
    """Which id a project answers to, from the three facts that decide it.

    Unbound, the project's own project.json is the source. Bound, ContextKit's
    id is authoritative and project.json holds at most its copy; until
    ContextKit has an id, the copy is the id it will adopt."""
    if not bound:
        return {"state": "unbound", "id": cached_id}
    if contextkit_id is None:
        return {"state": "pending", "id": cached_id}
    if cached_id is not None and cached_id != contextkit_id:
        return {"state": "mismatch", "id": contextkit_id}
    return {"state": "adopted", "id": contextkit_id}


def _identity_refusal(state: dict):
    """Why a write may not stamp this project's id, or None when it may.

    An unbound project without an id is left to the caller, whose refusal
    already names `capabilities init`."""
    if state["state"] == "unresolved":
        return state["error"]
    if state["state"] == "mismatch":
        return {"code": "project_id_mismatch",
                "message": f"project.json holds project id {state['project_json_id']}, "
                           f"but ContextKit answers {state['contextkit_id']}",
                "hint": "reconcile the two by hand: ContextKit's id is the "
                        "project's, and project.json keeps only its copy"}
    if state["state"] == "pending" and not state["id"]:
        return {"code": "project_id_unassigned",
                "message": "this project's ContextKit identity has no id yet",
                "hint": "run `contextkit identity adopt` in the project"}
    return None


def _handed_off_project_id(root: Path) -> str:
    """The project id handed down for THIS root, or "" when there is none.

    A process that resolved an id a write may stamp hands it to its descendants
    beside the envelope, as `CAPABILITIES_PROJECT_ID`, scoped by
    `CAPABILITIES_PROJECT_ID_ROOT` the way the envelope handoff is scoped: one
    that crossed a project boundary is passed over, and one supplied without the
    companion belongs to whoever launched the process and is honored as given.
    """
    handed = os.environ.get("CAPABILITIES_PROJECT_ID", "").strip()
    if not handed:
        return ""
    scope = os.environ.get("CAPABILITIES_PROJECT_ID_ROOT", "").strip()
    if not scope:
        return handed
    try:
        if Path(scope).expanduser().resolve() != root.resolve():
            return ""
    except OSError:
        return ""
    return handed


_PROJECT_ID_HANDED: dict = {}


def _project_id_state():
    """The id of the project this command stands in, where it came from, and
    whether a write may stamp it; None outside a project.

    A live handoff for this root outranks ContextKit, as the envelope handoff
    outranks the record: the process that handed it verified it. Otherwise the
    manager's answer decides. An id a write may stamp is handed on to this
    process's descendants. Reading it asks no records backend: the id is what
    selects a project's records, so it is read before any of them."""
    root = _project_root()
    if root is None:
        return None
    return _project_id_state_at(root)


def _project_id_state_at(root: Path) -> dict:
    """`_project_id_state` for a known project root."""
    try:
        declared = json.loads((_project_capabilities_dir(root) / "project.json").read_text())
    except (OSError, ValueError):
        declared = None
    cached = (str(declared.get("id") or "").strip() or None
              if isinstance(declared, dict) else None)
    handed = _handed_off_project_id(root)
    if handed and _PROJECT_ID_HANDED.get(str(root)) == handed:
        handed = ""  # this process's own hand-down, not its launcher's
    if handed:
        state = _identity_verdict(True, handed, cached)
        if state["state"] != "mismatch":
            state["state"] = "handed"
        state.update(bound=None, contextkit_id=handed)
    else:
        context = _context_identity(root)
        if "error" in context:
            state = {"state": "unresolved", "id": cached, "bound": True,
                     "contextkit_id": None, "error": context["error"]}
        else:
            state = _identity_verdict(context["bound"], context["id"], cached)
            state.update(bound=context["bound"], contextkit_id=context["id"])
    state["project_json_id"] = cached
    if not handed and state["id"] and _identity_refusal(state) is None:
        os.environ["CAPABILITIES_PROJECT_ID"] = state["id"]
        os.environ["CAPABILITIES_PROJECT_ID_ROOT"] = str(root)
        _PROJECT_ID_HANDED[str(root)] = state["id"]
    return state


def _project_id(write: bool = False, refuse: bool = True):
    """The project id this command stands in, or None where there is none.

    A read takes the id in every state. A write that stamps it takes it only
    once it is verified for this invocation, and otherwise exits 6 naming what
    to settle, or, with `refuse=False`, answers None so a read path that would
    stamp the id can leave that step out instead of refusing. An unbound project
    that declares no id is still None, left to the caller's own refusal."""
    state = _project_id_state()
    if state is None:
        return None
    if write:
        refusal = _identity_refusal(state)
        if refusal:
            if not refuse:
                return None
            _die(6, refusal["code"], refusal["message"], refusal.get("hint"))
    return state["id"]


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
    if isinstance(rule, list):
        return True
    if isinstance(rule, dict):
        if rule.get("deny") is True:
            return False
        if rule.get("enabled") is False or rule.get("allow") is False:
            return False
        return True
    return False


# Verbs a rule's verb list never withholds: they say what the capability is and
# how it is configured, which is what an agent should reach for before acting.
_AUTH_CONTRACT_VERBS = frozenset(
    {"help", "guide", "ids", "connections", "refs", "stub", "manifest"})


def _auth_rule_verbs(rule) -> list | None:
    """The verbs a capability rule limits its grant to, or None when it grants
    the whole capability. A bare list is shorthand for {"verbs": [...]}."""
    if isinstance(rule, list):
        verbs = rule
    elif isinstance(rule, dict) and "verbs" in rule:
        verbs = rule["verbs"]
    else:
        return None
    if not isinstance(verbs, list) or not all(isinstance(v, str) for v in verbs):
        _die(4, "auth_context_invalid",
             f"runtime authority context has invalid verbs for {NAME}",
             "verbs must be a list of verb names")
    return verbs


def _auth_gate() -> None:
    ctx = _auth_context()
    if ctx is None:
        return
    allowed = ctx.get("allowed_capabilities")
    if allowed is None:
        return
    if allowed is True or allowed == "*":
        return
    source = ctx.get("source") or "runtime"
    role = ctx.get("sender_role") or ctx.get("role") or "unknown"
    chat = ctx.get("chat_id") or "unknown"
    if isinstance(allowed, list):
        if NAME in allowed or "*" in allowed:
            return
    elif isinstance(allowed, dict):
        rule = allowed.get(NAME, allowed.get("*"))
        if _auth_capability_allowed(rule):
            verbs = _auth_rule_verbs(rule)
            verb = sys.argv[1] if len(sys.argv) > 1 else "help"
            if verbs is None or verb in verbs or verb in _AUTH_CONTRACT_VERBS:
                return
            _die(4, "verb_not_authorized",
                 f"verb refused: `{NAME} {verb}` is not among the verbs this "
                 f"{source} request may run ({', '.join(verbs) or 'none'})",
                 f"role={role}; chat_id={chat}; adjust runtime authority policy instead of bypassing the gate")
    else:
        _die(4, "auth_context_invalid",
             "runtime authority context has invalid allowed_capabilities")
    _die(4, "capability_not_authorized",
         f"{NAME} is not authorized for this {source} request",
         f"role={role}; chat_id={chat}; adjust runtime authority policy instead of bypassing the gate")


def _policy_row():
    """The effective policy row from the same records adapter as everything else."""
    try:
        row = _records().resolve("capabilities", "policy").get(NAME)
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)
    if row is None:
        return None
    entry = row.get("value")
    if (not isinstance(entry, dict) or set(entry) != {"enabled"}
            or not isinstance(entry.get("enabled"), bool)):
        _die(6, "bad_policy",
             f"invalid {NAME} policy entry in {_records().source}",
             'expected {"enabled": true|false}')
    return row


def _project_enabled_explicitly() -> bool:
    """True only for an explicit project enabled entry; global inheritance is
    deliberately insufficient for project-owned service activation."""
    row = _policy_row()
    return bool(row and row.get("scope") == "project"
                and row["value"].get("enabled") is True)


def _require_project_enabled_for_service() -> Path:
    root = _project_root()
    if root is None or not _project_enabled_explicitly():
        _die(4, "project_enable_required",
             f"{NAME} service activation requires an explicit project enable",
             f"ask the user, then run `capabilities enable {NAME} --project` "
             "before service init/start/run")
    return root


def _read_only_gate(action: str) -> None:
    """Refuse `action` when this process runs under the read-only switch.

    The switch is decided by the store tier's `read_only_switch`; this is the
    one refusal every writer that consults it gives, so a caller branches on one
    code, and that code is distinct from a connection's own read-only grant."""
    if read_only_switch():
        _die(4, "read_only_switch",
             f"{action} is refused: {READ_ONLY_ENV} is set, so {NAME} may read "
             "but change nothing in this process",
             "Do not lift the switch yourself — ask the user; the change has to "
             f"run in a process without {READ_ONLY_ENV}.")


_MACHINE_SCHEMA = "capabilities.machine.v1"


def _machine_state() -> str:
    """This machine's ceiling for this capability: `allowed` or `quarantined`.

    The manager alone writes `$XDG_CONFIG_HOME/capabilities/machine.json`. A
    capability with no entry, and every capability while the file is absent,
    is allowed, so the ceiling only ever closes what the two scopes open."""
    path = _CONFIG_HOME / "capabilities" / "machine.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return "allowed"
    except (OSError, ValueError) as e:
        _die(6, "bad_machine_state", f"cannot read the machine ceiling {path}: {e}",
             "the manager alone writes it; `capabilities list` reports each "
             "capability's machine state")
    entries = data.get("capabilities") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("schema") != _MACHINE_SCHEMA
            or not isinstance(entries, dict)):
        _die(6, "bad_machine_state", f"{path} is not a machine ceiling",
             f'expected {{"schema": "{_MACHINE_SCHEMA}", "capabilities": {{...}}}}')
    entry = entries.get(NAME)
    if entry is None:
        return "allowed"
    state = entry.get("state") if isinstance(entry, dict) else None
    if state not in ("allowed", "quarantined"):
        _die(6, "bad_machine_state", f"invalid {NAME} entry in {path}",
             'expected {"state": "allowed"|"quarantined", "at": ..., "by": ...}')
    return state


def _machine_gate() -> None:
    """The ceiling above both scopes: quarantined is disabled everywhere."""
    if _machine_state() == "quarantined":
        _die(4, "quarantined",
             f"{NAME} is quarantined on this machine, so no project and no "
             "machine service may use it, whatever project or global policy says",
             f"ask the user whether to allow {NAME} on this machine; if yes, they "
             f"run `capabilities allow {NAME}` - an agent never lifts a "
             "quarantine itself")


def _gate() -> None:
    """Project policy overrides global policy; absence inherits, and absence at
    both scopes is default deny. Safe local discovery verbs remain available so
    a disabled capability can be understood and configured before use.

    If an ingress supplied CAPABILITIES_AUTH_CONTEXT, that request-scoped gate
    is stricter and runs first, before credentials or network calls. The
    machine ceiling runs next: a capability quarantined on this machine is
    refused before any project or policy row is resolved.

    Under the read-only switch a bundled service is neither set up nor started
    nor handed a new declaration.

    A service that declares `machine` takes `--machine` on its service verbs;
    such a call resolves no project and reads no policy row, because the
    machine service checks each opted-in project's own policy before every
    action it takes for that project. `join` is a project act and always takes
    the ordinary path.

    `migrate` of a capability that declares `TABLES` acts on the machine's
    store rather than for a project, so it too reads no policy row: it answers
    to the machine ceiling, and `migrate` (not `migrate status`) to the
    read-only switch.
    """
    _auth_gate()
    verb = sys.argv[1] if len(sys.argv) > 1 else "help"
    safe = verb in {"help", "stub", "manifest", "connections"}
    if not safe:
        _machine_gate()
    service_action = sys.argv[2] if len(sys.argv) > 2 else None
    if verb == "migrate" and _tables_declared():
        if service_action != "status":
            _read_only_gate(f"`{NAME} migrate`")
        return
    service_contract = globals().get("SERVICE")
    service_call = isinstance(service_contract, dict) and verb == "service"
    machine_service = service_call and isinstance(service_contract.get("machine"), dict)
    if service_call and (service_action in {"init", "start", "run", "reload"}
                         or (machine_service and service_action in {"join", "leave"})):
        _read_only_gate(f"`{NAME} service {service_action}`")
    if machine_service and service_action != "join" and "--machine" in sys.argv[3:]:
        return
    if service_call and (service_action in {"init", "start", "run"}
                         or (machine_service and service_action == "join")):
        _require_project_enabled_for_service()
    if safe:
        return
    row = _policy_row()
    entry = row.get("value") if row else None
    scope = row.get("scope") if row else None
    if isinstance(entry, dict) and entry.get("enabled") is True:
        return
    if isinstance(entry, dict) and entry.get("enabled") is False and scope == "project":
        _die(4, "disabled",
             f"{NAME} is disabled in this project ({_records().source})",
             f"ask the user whether to enable {NAME} for this project; if yes, "
             f"run `capabilities enable {NAME} --project`")
    if isinstance(entry, dict) and entry.get("enabled") is False:
        message = f"{NAME} is disabled globally ({_records().source})"
    else:
        message = f"{NAME} is not enabled by project or global policy"
    _die(4, "not_enabled", message,
         f"ask the user whether to enable {NAME} only for this project or "
         f"globally for every project; then run `capabilities enable {NAME} "
         "--project` or `--global` exactly as requested")


def _invoked_verb() -> str | None:
    """The verb this process was invoked with: its first argument once the
    contract's own `--connection <id>` selection is lifted out of the way."""
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--connection":
            i += 2
        elif argv[i].startswith("--connection="):
            i += 1
        else:
            return argv[i]
    return None


def _machine_read(verb: str | None = None) -> bool:
    """Whether `verb`, else the invoked one, is a machine read here.

    Outside any project there is no scope a grant could have been written in,
    so a verb the capability declares in `MACHINE_READS` uses the machine's own
    connections instead, read-only. Inside a project the grant model alone
    decides, and a capability that declares nothing has no machine reads."""
    declared = globals().get("MACHINE_READS")
    if not declared or not isinstance(declared, tuple):
        return False
    if _project_root() is not None:
        return False
    return (verb if verb is not None else _invoked_verb()) in declared


def _policy_state() -> dict:
    """This capability's effective policy, in the words `capabilities list`
    uses: `effective` enabled or disabled, the `source` that decided it -
    `machine` (the ceiling), `project`, `global` or `default` - and the
    `machine` ceiling itself. A record that cannot be read is reported as the
    refusal the gate would give, so the report around it still answers."""
    import contextlib
    import io
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            machine = _machine_state()
            row = None if machine == "quarantined" else _policy_row()
    except SystemExit:
        lines = errors.getvalue().strip().splitlines()
        try:
            return {"error": json.loads(lines[-1])["error"]}
        except (IndexError, ValueError, KeyError, TypeError):
            return {"error": {"code": "policy_unreadable",
                              "message": errors.getvalue().strip()}}
    if machine == "quarantined":
        return {"effective": "disabled", "source": "machine", "machine": machine}
    if row is None:
        return {"effective": "disabled", "source": "default", "machine": machine}
    return {"effective": "enabled" if row["value"]["enabled"] else "disabled",
            "source": row.get("scope"), "machine": machine}


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
            if ((envelope / "project.json").is_file()
                    or (envelope / "settings.json").is_file()):
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
    if sub in ("set", "rm"):
        _read_only_gate(f"`{NAME} ids {sub}`")
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
    if sub in ("edit", "put"):
        _read_only_gate(f"`{NAME} context {sub}`")
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


INVENTORY_NETWORK_MODES = ("none", "optional", "required")


def _inventory_declaration() -> dict | None:
    """The manifest's `inventory` block, derived from the INVENTORY constant.

    `None` says the capability has nothing of its own to report here, so a
    reader assembling a whole project knows before it asks; the verb still
    answers, with the empty envelope, so nothing branches on absence."""
    if not INVENTORY:
        return None
    network = INVENTORY.get("network", "none")
    return {"network": network if network in INVENTORY_NETWORK_MODES else "none"}


def _cmd_inventory(argv: list[str]) -> None:
    """What this capability holds in this project, in one shape for all of them.

    The read is local, secret-free, and leaves nothing changed behind it.
    `--network` is the caller's explicit consent to reach the remote system for
    detail a local read cannot supply; without it that detail is named in
    `deferred` rather than fetched, so assembling a whole project never waits
    on a network the caller did not choose. `--json` is accepted for
    explicitness; the output is always this JSON."""
    network = False
    for arg in argv:
        if arg == "--network":
            network = True
        elif arg != "--json":
            _die(6, "input", f"unknown inventory option {arg!r}",
                 f"{NAME} inventory [--network]")
    root = _project_root()
    report: dict = {"capability": NAME,
                    "project": str(root) if root is not None else None,
                    "network": network,
                    "metrics": [], "items": [], "service": None, "deferred": []}
    if INVENTORY:
        built = _inventory(network) or {}
        for field in ("metrics", "items", "deferred"):
            report[field] = list(built.get(field) or [])
        report["service"] = built.get("service")
    _emit(report)


# A store setting that cannot be used is a configuration fault; a store that
# cannot be reached, is busy, or refuses the migration is the environment's.
_STORE_SETTING_SLUGS = frozenset({
    "store_not_configured", "store_not_postgres", "store_setting_unreadable",
    "store_setting_too_new", "bad_store_setting", "sslmode_too_weak",
    "bad_schema_name", "driver_missing"})


def _tables_declared() -> bool:
    """Whether the capability keeps tables in the machine's store (`TABLES`)."""
    return bool(globals().get("TABLES"))


def _cmd_migrate(argv: list[str]) -> None:
    """`migrate` applies the capability's pending steps to its tables in the
    machine's store, through the shared database library and under its lock;
    `migrate status` reports where they stand, without a lock, changing nothing.
    The manager runs `migrate` when it installs or updates the payload, and a
    service runs it when it starts."""
    if argv not in ([], ["status"]):
        _die(6, "input", f"unknown migrate arguments {' '.join(argv)!r}",
             f"{NAME} migrate [status]")
    steps, major, minor = _tables()
    try:
        from capabilities_contract import db
    except ImportError:
        _die(6, "driver_missing",
             "the shared database library capabilities-contract is not installed",
             f"run {NAME} through its own script header, which pins it")
    conn = None
    try:
        conn = db.connect(application_name=NAME)
        if argv:
            found = db.status(conn, NAME, steps, major=major, minor=minor)
            stored = (None if found.stored_major is None
                      else {"major": found.stored_major, "minor": found.stored_minor})
            report = {"owner": found.owner, "schema": found.schema, "state": found.state,
                      "code": {"major": found.major, "minor": found.minor},
                      "store": stored, "applied": found.applied,
                      "pending": found.pending, "changed": found.changed,
                      "warnings": found.warnings}
        else:
            done = db.migrate(conn, NAME, steps, major=major, minor=minor)
            report = {"owner": done.owner, "schema": done.schema,
                      "applied": done.applied, "skipped": done.skipped,
                      "version": {"major": done.major, "minor": done.minor},
                      "warnings": done.warnings}
    except db.DbError as exc:
        _die(6 if exc.slug in _STORE_SETTING_SLUGS else 5, exc.slug, exc.message, exc.hint)
    finally:
        if conn is not None:
            conn.close()
    _emit(report)


MACHINE_READ_EFFECT = ("outside any project these verbs use the machine's own "
                       "connections, read-only; inside a project its grants "
                       "alone decide")


def _cmd_connections_reported() -> None:
    """The capability's own connections report, with what the contract knows
    beside it. The report stays the capability's: it is taken whole, and the
    capability's effective policy state is added to it. Under the read-only
    switch every connection in it is marked read-only and the switch is named
    beside it, so a reader sees why no connection may write; in a machine read
    each connection the machine lent is named as the machine's, read-only."""
    import contextlib
    import io
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            _cmd_connections()
    except SystemExit:
        sys.stdout.write(captured.getvalue())
        raise
    try:
        report = json.loads(captured.getvalue())
    except ValueError:
        report = None
    if not isinstance(report, dict):
        sys.stdout.write(captured.getvalue())
        return
    conns = report.get("connections")
    conns = conns if isinstance(conns, dict) else {}
    if read_only_switch():
        for entry in conns.values():
            if isinstance(entry, dict):
                entry["allow_write"] = False
        report["read_only_switch"] = {
            "variable": READ_ONLY_ENV, "on": True,
            "effect": READ_ONLY_EFFECT}
    lent = globals().get("_MACHINE_LENT") or {}
    if lent:
        for cid in lent:
            if isinstance(conns.get(cid), dict):
                conns[cid]["allow_write"] = False
        report["machine_reads"] = {
            "verbs": list(MACHINE_READS),
            "connections": {cid: {"scope": "machine", "source": source}
                            for cid, source in sorted(lent.items())},
            "effect": MACHINE_READ_EFFECT}
    report["policy"] = _policy_state()
    _emit(report)


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
               "state": STATE, "inventory": _inventory_declaration(),
               "post_install": POST_INSTALL, "tables": _tables_declared()})
    elif cmd == "migrate" and _tables_declared():
        _cmd_migrate(argv[1:])
    elif cmd == "guide":
        _cmd_guide(argv[1:])
    elif cmd == "ids":
        _cmd_ids(argv[1:])
    elif cmd == "refs":
        _cmd_refs(argv[1:])
    elif cmd == "context":
        _cmd_context(argv[1:])
    elif cmd == "connections":
        _cmd_connections_reported()
    elif cmd == "inventory":
        _cmd_inventory(argv[1:])
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


def _grant_hint(cid: str) -> str:
    """One wording for the one way a project grants an inherited connection.

    Same shape as `_write_gate`'s: the gate is the human's to lift, never the
    agent's, and the hint carries the exact command rather than a description
    of one. Where no project resolves there is no scope to grant in, so the
    hint names the command that makes one and then the grant, in the order they
    have to happen."""
    grant = f"`capabilities set {NAME} grant {cid} '{{\"enabled\": true}}'`"
    if _project_root() is None:
        return ("Do not lift the gate yourself — ask the user; run "
                f"`capabilities init` in the project that should use {NAME}, "
                f"then {grant} there.")
    return ("Do not lift the gate yourself — ask the user; granting is "
            f"{grant} run inside this project.")


# The connections the machine lent this process for a machine read, by id, each
# with the file it was declared in; empty in every other process.
_MACHINE_LENT: dict = {}


def _grant_gate(reg: dict, cid: str) -> None:
    """Refuse a connection this project may not use, for read as for write.

    A connection declared outside the project is not the project's to use until
    the project itself says so, and one the project switched off is off;
    nothing in the cascade lifts either. Outside a project there is no scope in
    which anything could have said so, which is a different situation and says
    so rather than asserting a project that is not there."""
    entry = (reg.get("withheld") or {}).get(cid)
    if entry is None:
        return
    if entry.get("scope") == "project":
        message = f"connection {cid!r} is disabled in this project"
    elif _machine_read():
        message = (f"connection {cid!r} is switched off on this machine by "
                   f"its grant")
    elif _project_root() is None:
        message = (f"connection {cid!r} is declared globally and there is no "
                   f"project here to grant it in")
    else:
        message = (f"connection {cid!r} is declared outside this project and "
                   f"this project has not granted it")
    _die(4, "connection_not_granted", message, _grant_hint(cid))


def _connections_composed() -> dict:
    """The connections envelope shape, composed out of resolved records.

    Deliberately reconstituted rather than returned in some store-shaped form:
    selection, the write gate and the `connections` report all read the envelope
    shape, and they should not learn where the rows came from. What changes here
    is the source, never the shape.

    The permission a project was granted is folded back onto the entry as
    `allow_write`, which is where the rest of the contract looks for it.

    A connection this project may not use is kept apart under `withheld`, not
    dropped: `connections` holds exactly what may be acted on — which is what
    every capability's own report iterates — while selection still has the name
    it needs to explain a refusal instead of pretending the connection was
    never declared.

    `sources` names, per connection, the file its identity was read out of. An
    identity is taken whole from one scope, so each connection has exactly one
    origin, and a project that merely granted an inherited connection is not
    where that connection's values are written. It is kept beside the entries
    rather than folded into them because it describes a record rather than
    configuring one: nothing resolves through it, and its one reader is each
    capability's own report, naming the origin of its `connection`-tier keys."""
    adapter = _records()
    machine = _machine_read()
    _MACHINE_LENT.clear()
    try:
        effective = adapter.connections(NAME, write_default=WRITE_DEFAULT,
                                        include_disabled=True,
                                        machine_read=machine)
        default = adapter.get(NAME, "setting", "connection.default")
    except StoreError as e:
        _die(6, e.slug, e.message, e.hint)
    if not effective:
        _die(6, "connections_required",
             f"{NAME} requires an explicit connections registry and "
             f"{adapter.source} holds none",
             'expected {"default": "<id>", "connections": {"<id>": { ... }}}')
    usable = {cid: e for cid, e in effective.items() if e["enabled"]}
    withheld = {cid: e for cid, e in effective.items() if not e["enabled"]}
    if not usable:
        named = ", ".join(sorted(withheld))
        if machine:
            refusal = (f"every {NAME} connection this machine declares is "
                       f"switched off by its grant: {named}")
        elif _project_root() is None:
            refusal = (f"there is no project here to use a {NAME} connection in, "
                       f"so every connection declared globally stays "
                       f"withheld: {named}")
        else:
            refusal = (f"no {NAME} connection is usable in this project; every "
                       f"one it can see is withheld: {named}")
        _die(4, "connection_not_granted", refusal, _grant_hint("<id>"))
    sources = {cid: adapter.scope_source(NAME, "connection", entry["scope"][0])
               for cid, entry in usable.items()}
    _MACHINE_LENT.update({cid: sources[cid] for cid, entry in usable.items()
                          if entry.get("machine_read")})
    return {
        "default": default,
        "connections": {cid: {**entry["value"], "allow_write": entry["allow_write"]}
                        for cid, entry in usable.items()},
        "sources": sources,
        "withheld": {cid: {"scope": entry["scope"][0]}
                     for cid, entry in withheld.items()},
    }


def _inventory_connections(machine_read: bool | None = None) -> dict:
    """Every connection this project declares, granted or not.

    An inventory reports where selection refuses, so this is deliberately the
    one connection read that never dies: no registry is zero connections, and
    a connection the project may not act on is still a connection worth
    showing. `granted` carries which is which. `machine_read` asks as one of
    the capability's machine reads would, else as the invoked verb does."""
    machine = _machine_read() if machine_read is None else bool(machine_read)
    try:
        adapter = _records()
        effective = adapter.connections(NAME, write_default=WRITE_DEFAULT,
                                        include_disabled=True,
                                        machine_read=machine) or {}
        default = adapter.get(NAME, "setting", "connection.default")
    except StoreError:
        return {"default": None, "connections": {}}
    return {"default": default,
            "connections": {
                cid: {**entry["value"], "allow_write": entry["allow_write"],
                      "granted": bool(entry["enabled"])}
                for cid, entry in effective.items()}}


def _connections_registry() -> tuple[dict | None, Path | str | None]:
    """The connections envelope and where it came from.

    Composed from the two records a connection is kept as -- who it is, and
    what this project may do with it -- and handed on in the shape the rest of
    the contract already reads, so selection, the write gate and the report
    never learn which source answered.

    The path returned beside it answers for the collection -- where this project
    reads connections from. Where one connection's own values came from is a
    question about that connection, and `reg["sources"][cid]` is what answers
    it."""
    return _connections_composed(), _records().collection_source(NAME, "connection")


def _select_connection(reg: dict | None, wanted: str | None) -> tuple[str, dict | None]:
    """flag → default pointer → sole entry → die 6. A connection's own
    `address` field selects it too (used where a
    connection carries a human-recognizable address; absent fields never match).

    A connection the project was never granted is refused here rather than
    reported missing, so the one enforcement point every capability already
    routes through is also the one that answers "why not"."""
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
        _grant_gate(reg, wanted)
        _die(6, "unknown_connection", f"no connection matches {wanted!r}",
             f"known: {', '.join(conns)}")
    default = reg.get("default")
    if default:
        if default not in conns:
            _grant_gate(reg, default)
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
    """Policy from the committed registry; nothing in the cascade lifts it.
    The read-only switch closes it for every connection and names itself."""
    if verb in WRITE_VERBS:
        _read_only_gate(f"write verb {verb!r} on connection {conn_id!r}")
    if verb in WRITE_VERBS and not allow_write and conn_id in _MACHINE_LENT:
        _die(4, "read_only",
             f"connection {conn_id!r} is the machine's, lent read-only to a "
             f"machine read outside any project",
             "Do not lift the gate yourself — ask the user; a write runs "
             "inside a project that grants this connection.")
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
