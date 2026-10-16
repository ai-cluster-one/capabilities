"""The project-identity read of the contract as released before the id moved to
project.json alone (contract/preamble.py at 4fb2b66), verbatim. Capabilities
stamped with it keep running after a manager self-update until they are
updated, so the manager's `path --json --identity` answer must keep resolving
for it. Loaded by tests/test_manager_project_identity.py; not shipped code."""

import json
import os
from pathlib import Path

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

