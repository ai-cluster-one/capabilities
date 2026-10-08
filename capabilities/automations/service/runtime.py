from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ACTIVE_STATUSES = ("pending", "starting", "running")
FINAL_STATUSES = ("succeeded", "failed", "canceled", "interrupted", "skipped")
ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
AGENT_ENGINES = ("claude", "codex")
AGENT_MODES = ("read", "write", "act")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
AGENT_KEYS = frozenset(
    {"engine", "model", "effort", "mode", "timeout_seconds", "service_tier"}
)
# Exactly the keys the block loop below reads. A key outside it was silently
# dropped, so a misspelled `arguments` left an automation running on schedule,
# exiting zero, and doing none of what it was declared to do.
# `name` and `description` are read for a person rather than for the engine:
# they change no scheduling, and they carry what an automation is called and
# why it exists into the listing, so reading one no longer means opening every
# script. Nothing executing consumes them, which is a fact about their reader.
AUTOMATION_KEYS = frozenset(
    {"id", "name", "description", "script", "enabled", "schedule",
     "every_seconds", "timeout_seconds", "max_parallel", "max_pending",
     "overlap", "retries", "arguments", "environments"}
)

# Shipped profiles name Claude's rolling aliases, which keep pointing at the
# newest model of each tier, so the capability carries no model id that ages.
# A Codex profile names a concrete model and is therefore declared by the
# project rather than shipped here.
BUILTIN_AGENTS: dict[str, dict[str, Any]] = {
    "sonnet": {"engine": "claude", "model": "sonnet", "effort": "high",
               "mode": "read", "timeout_seconds": 600.0, "service_tier": None},
    "opus": {"engine": "claude", "model": "opus", "effort": "high",
             "mode": "read", "timeout_seconds": 900.0, "service_tier": None},
    "haiku": {"engine": "claude", "model": "haiku", "effort": "low",
              "mode": "read", "timeout_seconds": 180.0, "service_tier": None},
}
BUILTIN_AGENT_DEFAULT = "sonnet"


class ConfigError(ValueError):
    pass


def automations_bin() -> str:
    """The absolute path of the CLI, for handing to a job so a script can call
    back into the capability without resolving anything itself. The CLI exports
    it; the bundle layout answers when the daemon was started another way."""
    declared = os.environ.get("AUTOMATIONS_BIN")
    if declared and os.access(declared, os.X_OK):
        return declared
    sibling = Path(__file__).resolve().parent.parent / "bin" / "automations"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("automations") or "automations"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{name} must be a number >= {minimum:g}")
    return float(value)


def _cron_values(field: str, minimum: int, maximum: int, *, sunday: bool = False) -> set[int]:
    values: set[int] = set()
    for token in field.split(","):
        token = token.strip()
        if not token:
            raise ConfigError(f"empty cron token in {field!r}")
        base, slash, step_raw = token.partition("/")
        step = 1
        if slash:
            try:
                step = int(step_raw)
            except ValueError as exc:
                raise ConfigError(f"invalid cron step {step_raw!r}") from exc
            if step <= 0:
                raise ConfigError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise ConfigError(f"invalid cron range {base!r}") from exc
            if start > end:
                raise ConfigError(f"descending cron range {base!r}")
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ConfigError(f"invalid cron value {base!r}") from exc
        allowed_max = 7 if sunday else maximum
        if start < minimum or end > allowed_max:
            raise ConfigError(f"cron value {base!r} outside {minimum}..{allowed_max}")
        for value in range(start, end + 1, step):
            values.add(0 if sunday and value == 7 else value)
    return values


def parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = expression.split()
    if len(fields) != 5:
        raise ConfigError("schedule must be a five-field cron expression")
    minute = _cron_values(fields[0], 0, 59)
    hour = _cron_values(fields[1], 0, 23)
    day = _cron_values(fields[2], 1, 31)
    month = _cron_values(fields[3], 1, 12)
    weekday = _cron_values(fields[4], 0, 6, sunday=True)
    return minute, hour, day, month, weekday


def cron_matches(expression: str, when: datetime) -> bool:
    minute, hour, day, month, weekday = parse_cron(expression)
    fields = expression.split()
    dom_match = when.day in day
    dow_match = ((when.weekday() + 1) % 7) in weekday
    if fields[2] == "*":
        day_match = dow_match
    elif fields[4] == "*":
        day_match = dom_match
    else:
        day_match = dom_match or dow_match
    return when.minute in minute and when.hour in hour and when.month in month and day_match


def _agent_profile(label: str, item: Any, base: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError(f"{label} must be a table")
    unknown = sorted(set(item) - AGENT_KEYS)
    if unknown:
        raise ConfigError(f"{label} has unknown key(s): {', '.join(unknown)}")
    resolved = dict(base) if base else {}
    engine = item.get("engine", resolved.get("engine"))
    if engine not in AGENT_ENGINES:
        raise ConfigError(f"{label}.engine must be one of {', '.join(AGENT_ENGINES)}")
    model = item.get("model", resolved.get("model"))
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{label}.model must be a non-empty string")
    mode = item.get("mode", resolved.get("mode", "read"))
    if mode not in AGENT_MODES:
        raise ConfigError(f"{label}.mode must be one of {', '.join(AGENT_MODES)}")
    effort = item.get("effort", resolved.get("effort"))
    if effort is not None:
        if not isinstance(effort, str) or not effort.strip():
            raise ConfigError(f"{label}.effort must be a non-empty string")
        if engine == "claude" and effort not in CLAUDE_EFFORTS:
            raise ConfigError(
                f"{label}.effort must be one of {', '.join(CLAUDE_EFFORTS)} on claude"
            )
    service_tier = item.get("service_tier", resolved.get("service_tier"))
    if service_tier is not None:
        if engine != "codex":
            raise ConfigError(f"{label}.service_tier applies to the codex engine only")
        if not isinstance(service_tier, str) or not service_tier.strip():
            raise ConfigError(f"{label}.service_tier must be a non-empty string")
    return {
        "engine": engine,
        "model": model,
        "effort": effort,
        "mode": mode,
        "timeout_seconds": _number(
            item.get("timeout_seconds", resolved.get("timeout_seconds", 600.0)),
            f"{label}.timeout_seconds", 1,
        ),
        "service_tier": service_tier,
    }


def load_agents(raw: dict[str, Any]) -> dict[str, Any]:
    """Shipped profiles are always present; a declared profile of the same name
    overrides the shipped one field by field, so a project can retune effort or
    timeout without restating the engine."""
    section = raw.get("agents") or {}
    if not isinstance(section, dict):
        raise ConfigError("agents must be a table")
    unknown = sorted(set(section) - {"default", "workers"})
    if unknown:
        raise ConfigError(f"agents has unknown key(s): {', '.join(unknown)}")
    declared = section.get("workers") or {}
    if not isinstance(declared, dict):
        raise ConfigError("agents.workers must be a table")
    profiles = {name: dict(spec) for name, spec in BUILTIN_AGENTS.items()}
    for name, item in declared.items():
        if not AGENT_ID_RE.fullmatch(name):
            raise ConfigError(f"agents.workers key {name!r} must match {AGENT_ID_RE.pattern}")
        profiles[name] = _agent_profile(
            f"agents.workers.{name}", item, BUILTIN_AGENTS.get(name)
        )
    default = section.get("default", BUILTIN_AGENT_DEFAULT)
    if not isinstance(default, str) or default not in profiles:
        raise ConfigError(
            f"agents.default must name a declared profile; have {', '.join(sorted(profiles))}"
        )
    return {"default": default, "workers": profiles}


# --- the store schema this capability owns -----------------------------------

# `automations` declares its own namespace and migrates it itself; the core
# tier knows nothing about these columns. They are columns rather than a JSON
# blob because the scheduler filters on them on every tick — `enabled`, the
# environment, the pending count in the run ledger — which is the test for whether
# a record class has earned a table of its own.
#
# `script_key` names a document, not a path. The version that runs is whichever
# one the document's pin names, so editing a script and deploying it stay two
# separate acts.
STORE_NAMESPACE = "automations"
STORE_VERSION = 2
STORE_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS automations (
        id               TEXT PRIMARY KEY,
        scope            TEXT NOT NULL,
        project_id       TEXT REFERENCES projects(id),
        slug             TEXT NOT NULL,
        name             TEXT,
        description      TEXT,
        enabled          INTEGER NOT NULL DEFAULT 1,
        script_key       TEXT NOT NULL,
        schedule         TEXT,
        every_seconds    REAL,
        timeout_seconds  REAL NOT NULL DEFAULT 300,
        max_parallel     INTEGER NOT NULL DEFAULT 1,
        max_pending      INTEGER NOT NULL DEFAULT 1,
        overlap          TEXT NOT NULL DEFAULT 'skip',
        retries          INTEGER NOT NULL DEFAULT 0,
        arguments        {json},
        environments     {json},
        updated_at       TEXT NOT NULL
    )
    """,
    # The slug is what a person types and what a run refers to; the id is what
    # a row refers to. Unique per scope, because two projects may each have an
    # automation they both call `nightly`.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS automations_slug_idx
        ON automations (scope, COALESCE(project_id, ''), slug)
    """,
    """
    CREATE INDEX IF NOT EXISTS automations_due_idx
        ON automations (scope, project_id, enabled)
    """,
]


def store_upsert(store, scope: str, project_id: str | None, item: dict) -> str:
    """Write one automation row and return its id. The table is this
    capability's, so the SQL that touches it lives here beside the schema rather
    than in whatever tool happens to be filling it in."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = store._execute(
        "SELECT id FROM automations WHERE scope = ? AND COALESCE(project_id,'') = ? "
        "AND slug = ?", (scope, project_id or "", item["slug"])).fetchone()
    values = (item["name"], item["description"], item["enabled"], item["script_key"],
              item["schedule"], item["every_seconds"], item["timeout_seconds"],
              item["max_parallel"], item["max_pending"], item["overlap"], item["retries"],
              store._encode(item["arguments"]), store._encode(item["environments"]), now)
    if existing:
        store._execute(
            "UPDATE automations SET name = ?, description = ?, enabled = ?, script_key = ?, "
            "schedule = ?, every_seconds = ?, timeout_seconds = ?, max_parallel = ?, "
            "max_pending = ?, overlap = ?, retries = ?, arguments = ?, environments = ?, "
            "updated_at = ? WHERE id = ?", values + (existing[0],))
        return existing[0]
    row_id = str(uuid.uuid4())
    store._execute(
        "INSERT INTO automations (id, scope, project_id, slug, name, description, enabled, "
        "script_key, schedule, every_seconds, timeout_seconds, max_parallel, max_pending, "
        "overlap, retries, arguments, environments, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, scope, project_id, item["slug"]) + values)
    return row_id


def load_effective_config(root: Path, config_path: Path,
                          state_dir: Path) -> dict[str, Any]:
    """The config the scheduler acts on, from whichever source this project
    keeps its records in. One entry point, so no caller has to know which."""
    if _store_mode(root)[0] == "db":
        return load_config_from_store(root, state_dir)
    return load_config(root, config_path)


# The daemon reads its configuration once, at startup, and a scheduler is the
# kind of process nobody looks at while it is right. So it writes down what it
# read, and `doctor` compares: a declaration that changed after the daemon
# loaded it stops being an invisible fact and becomes a failing health answer,
# which whatever supervises the daemon already knows how to act on.
DAEMON_FINGERPRINT_FILE = "daemon.fingerprint"


def config_fingerprint(config: dict[str, Any]) -> str:
    """Twelve hex characters standing for everything a person declared.

    `environment` and `namespace` are excluded deliberately: both are taken
    from the process environment, so a daemon started under one and a doctor
    invoked under another would disagree about them permanently, and every
    comparison would report a change that no restart could ever settle. What
    remains is the declaration itself, which is what a restart actually
    reloads."""
    engine = {key: value for key, value in config["engine"].items()
              if key not in {"environment", "namespace"}}
    material = {"version": config["version"], "engine": engine,
                "automations": config["automations"], "agents": config["agents"]}
    # `default=str` is here for the resolved script paths, which are the one
    # non-primitive the normalised config carries.
    text = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def daemon_record(config: dict[str, Any]) -> str:
    """Everything the running daemon publishes about what it loaded.

    The fingerprint answers *which declaration*; the environment answers *which
    of it is in force*, and the second is taken from the daemon's own process
    environment, which nothing outside that process can see. A supervisor that
    exports one environment and a terminal opened later that carries none are
    the ordinary case, so the loaded value is written down rather than
    re-derived by whoever asks. It rides in the file the daemon already writes
    beside its pid and behind the same lock, because a second file answering
    for the same process is a second thing to disagree with.
    """
    return json.dumps({
        "config": config_fingerprint(config),
        "environment": config["engine"]["environment"],
    }, sort_keys=True) + "\n"


def read_daemon_record(state_dir: Path) -> dict[str, Any]:
    """What the running daemon published, as far as it published anything.

    A daemon started by an older payload wrote the bare fingerprint and no
    environment, and it keeps running across an upgrade, so that form is read
    as what it is rather than as a damaged record."""
    try:
        text = (state_dir / DAEMON_FINGERPRINT_FILE).read_text().strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"config": text}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def read_config_fingerprint(state_dir: Path) -> str | None:
    """What the running daemon recorded, or None if it recorded nothing."""
    value = read_daemon_record(state_dir).get("config")
    return value if isinstance(value, str) and value else None


def read_daemon_environment(state_dir: Path) -> str | None:
    """The environment the running daemon loaded, or None if it published none."""
    value = read_daemon_record(state_dir).get("environment")
    return value if isinstance(value, str) and value else None


def _store_mode(root: Path) -> tuple[str, str]:
    """Where this project keeps its records, asked of the one place that knows.

    An automation is a table row with no file half on purpose -- `config.toml`
    carries its description in comments a writer would destroy -- so this fork
    stays. What does not stay is a second opinion about which mode is in force."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import store as _store
    except ImportError:
        return "files", "no store module beside the service"
    finally:
        sys.path.pop(0)
    try:
        return _store.records_mode(root / "capabilities")
    except _store.StoreError as exc:
        raise ConfigError(exc.message) from exc


def _project_identity(root: Path) -> dict:
    try:
        return json.loads((root / "capabilities" / "project.json").read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"no project identity under {root}") from exc


# The id a registration stamps is the one the launching CLI resolves for a
# write: in process the CLI installs its resolver here, called with `strict`
# False on a read path so a project whose id may not be stamped reads instead of
# refusing, and the daemon it launches is handed the answer as
# CAPABILITIES_PROJECT_ID. project.json's own id stands in only where no
# launcher resolved one.
PROJECT_ID_FOR_WRITE = None


def _handed_project_id(root: Path) -> str:
    """The id handed down for this root, scoped the way the envelope is."""
    handed = os.environ.get("CAPABILITIES_PROJECT_ID", "").strip()
    scope = os.environ.get("CAPABILITIES_PROJECT_ID_ROOT", "").strip()
    if not handed or not scope:
        return handed
    try:
        return handed if Path(scope).resolve() == Path(root).resolve() else ""
    except OSError:
        return ""


def _registration_id(root: Path, identity: dict, strict: bool = True) -> str | None:
    if callable(PROJECT_ID_FOR_WRITE):
        return PROJECT_ID_FOR_WRITE(strict)
    return _handed_project_id(root) or identity.get("id")


def materialise_script(state_dir: Path, key: str, version: str, body: str) -> Path:
    """Write a script's active version where a subprocess can run it.

    The file is named by the version's hash, so a cached copy can never be the
    wrong text: a different version is a different filename, and the old one
    simply stops being asked for."""
    cache = state_dir / "scripts"
    cache.mkdir(parents=True, exist_ok=True)
    suffix = ".py" if not key.endswith(".schema") else ".json"
    path = cache / f"{key}.{version}{suffix}"
    if not path.is_file() or path.read_text() != body:
        path.write_text(body)
    return path


def load_config_from_store(root: Path, state_dir: Path) -> dict[str, Any]:
    """The same normalised config, composed out of the store.

    Everything the scheduler reads on a tick is here: the engine settings, the
    agent profiles, and one row per automation. A script is not a path into the
    repository any more — it is a context item, and the version that runs is
    whichever one is active, written out under its own hash so a subprocess has
    something to execute."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import store as _store
    except ImportError as exc:
        raise ConfigError("this project keeps its records in the store, but the "
                          "store module is not installed beside the service") from exc
    finally:
        sys.path.pop(0)

    identity = _project_identity(root)
    scopes = _store.Scopes(project=identity["slug"])
    try:
        with _store.open_store() as st:
            engine = st.config_get("automations", "setting", "engine", scopes) or {}
            agents = st.config_get("automations", "setting", "agents", scopes) or {}
            chain = st._chain(scopes)
            clause, params = st._chain_clause(chain)
            rows = st._execute(
                "SELECT slug, name, description, enabled, script_key, schedule, "
                "every_seconds, timeout_seconds, max_parallel, max_pending, overlap, "
                f"retries, arguments, environments FROM automations WHERE ({clause}) "
                "ORDER BY slug", params).fetchall()
            scripts = {}
            for row in rows:
                doc = st.context_read("automations", row[4], scopes)
                if doc is None:
                    raise ConfigError(
                        f"automation {row[0]!r} names script {row[4]!r}, which has no "
                        "active version")
                scripts[row[4]] = materialise_script(state_dir, row[4], doc["hash"],
                                                     doc["body"])
    except _store.StoreError as exc:
        raise ConfigError(f"cannot read the store: {exc.message}") from exc

    raw = {"version": 1, "engine": dict(engine), "agents": dict(agents), "automations": []}
    for row in rows:
        raw["automations"].append({
            # The record names a versioned document, not an operator-supplied
            # filesystem path. Keep the materialized name relative to its XDG
            # cache and fence it to that cache below.
            "id": row[0], "name": row[1], "description": row[2],
            "enabled": bool(row[3]),
            "script": str(scripts[row[4]].relative_to(state_dir.resolve())),
            "schedule": row[5], "every_seconds": row[6], "timeout_seconds": row[7],
            "max_parallel": row[8], "max_pending": row[9], "overlap": row[10],
            "retries": row[11],
            "arguments": st_decode(row[12]), "environments": st_decode(row[13]),
        })
    return normalise_config(root, raw, script_root=state_dir)


def st_decode(value: Any) -> Any:
    if value is None:
        return []
    return json.loads(value) if isinstance(value, str) else value


def load_config(root: Path, config_path: Path) -> dict[str, Any]:
    """The config as the file declares it."""
    try:
        raw = tomllib.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    return normalise_config(root, raw)


def normalise_config(root: Path, raw: dict[str, Any],
                     script_root: Path | None = None) -> dict[str, Any]:
    """Validation and defaulting, shared by both sources. A record that came
    from the store is checked exactly as one that came from the file, so the
    two cannot drift into disagreeing about what a valid automation is."""
    if raw.get("version") != 1:
        raise ConfigError("config version must be 1")
    engine = raw.get("engine") or {}
    if not isinstance(engine, dict):
        raise ConfigError("engine must be a table")
    timezone_name = str(engine.get("timezone") or "UTC")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone {timezone_name!r}") from exc
    normalized_engine = {
        "tick_seconds": _number(engine.get("tick_seconds", 1.0), "engine.tick_seconds", 0.1),
        "max_parallel": _int(engine.get("max_parallel", 4), "engine.max_parallel", 1),
        "timezone": timezone_name,
        "shutdown_grace_seconds": _number(
            engine.get("shutdown_grace_seconds", 15.0),
            "engine.shutdown_grace_seconds",
            0.0,
        ),
        "environment": os.environ.get("AUTOMATIONS_ENVIRONMENT") or str(
            engine.get("environment") or "development"
        ),
        "namespace": os.environ.get("AUTOMATIONS_NAMESPACE") or str(
            engine.get("namespace") or root.name
        ),
    }
    recovery = str(engine.get("recovery") or "fail")
    if recovery not in {"fail", "retry"}:
        raise ConfigError("engine.recovery must be fail or retry")
    normalized_engine["recovery"] = recovery
    entries = raw.get("automations") or []
    if not isinstance(entries, list):
        raise ConfigError("automations must be an array of tables")
    seen: set[str] = set()
    automations: list[dict[str, Any]] = []
    for index, item in enumerate(entries):
        label = f"automations[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be a table")
        unknown = sorted(set(item) - AUTOMATION_KEYS)
        if unknown:
            raise ConfigError(f"{label} has unknown key(s): {', '.join(unknown)}")
        automation_id = item.get("id")
        if not isinstance(automation_id, str) or not ID_RE.fullmatch(automation_id):
            raise ConfigError(f"{label}.id must match {ID_RE.pattern}")
        if automation_id in seen:
            raise ConfigError(f"duplicate automation id {automation_id!r}")
        seen.add(automation_id)
        script_raw = item.get("script")
        if not isinstance(script_raw, str) or not script_raw.strip():
            raise ConfigError(f"{label}.script must be a non-empty relative path")
        script = Path(script_raw)
        if script.is_absolute():
            boundary = "script cache" if script_root is not None else "project root"
            raise ConfigError(f"{label}.script must be relative to the {boundary}")
        allowed_root = (script_root or root).resolve()
        resolved_script = (allowed_root / script).resolve()
        try:
            resolved_script.relative_to(allowed_root)
        except ValueError as exc:
            boundary = "script cache" if script_root is not None else "project root"
            raise ConfigError(f"{label}.script escapes the {boundary}") from exc
        schedule = item.get("schedule")
        every_seconds = item.get("every_seconds")
        if schedule is not None and every_seconds is not None:
            raise ConfigError(f"{label} may declare schedule or every_seconds, not both")
        if schedule is not None:
            if not isinstance(schedule, str):
                raise ConfigError(f"{label}.schedule must be a string")
            parse_cron(schedule)
        if every_seconds is not None:
            every_seconds = _int(every_seconds, f"{label}.every_seconds", 1)
        overlap = str(item.get("overlap") or "skip")
        if overlap not in {"skip", "queue"}:
            raise ConfigError(f"{label}.overlap must be skip or queue")
        arguments = item.get("arguments") or []
        if not isinstance(arguments, list) or not all(isinstance(v, str) for v in arguments):
            raise ConfigError(f"{label}.arguments must be an array of strings")
        environments = item.get("environments") or []
        if not isinstance(environments, list) or not all(
            isinstance(v, str) and v for v in environments
        ):
            raise ConfigError(f"{label}.environments must be an array of strings")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"{label}.name must be a string")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            raise ConfigError(f"{label}.description must be a string")
        automations.append({
            "id": automation_id,
            "name": name or None,
            "description": description or None,
            "script": script_raw,
            "script_path": resolved_script,
            "enabled": bool(item.get("enabled", True)),
            "schedule": schedule,
            "every_seconds": every_seconds,
            "timeout_seconds": _number(
                item.get("timeout_seconds", 300), f"{label}.timeout_seconds", 1
            ),
            "max_parallel": _int(item.get("max_parallel", 1), f"{label}.max_parallel", 1),
            "max_pending": _int(item.get("max_pending", 1), f"{label}.max_pending", 0),
            "overlap": overlap,
            "retries": _int(item.get("retries", 0), f"{label}.retries", 0),
            "arguments": arguments,
            "environments": environments,
        })
    return {"version": 1, "engine": normalized_engine,
            "automations": automations, "agents": load_agents(raw)}


def automation_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in config["automations"]}


def applies(item: dict[str, Any], environment: str) -> bool:
    return item["enabled"] and (
        not item["environments"] or environment in item["environments"]
    )


# --- the run ledger -------------------------------------------------------------

# The ledger is what every daemon of a project coordinates through, so it lives
# in the machine's store, reached through the shared database library, in this
# capability's own table and migration ledger (DOCTRINE rule 22). The owner the
# library records the steps under is the capability's name, and every object a
# step creates is named after it.
#
# `dedupe_key` is project, environment, automation and scheduled time. It is the
# whole of the mutual exclusion between machines running one project: whichever
# inserts a firing first owns it and the other is told. It is unique within its
# project rather than across the table, because one database serves every
# project on every machine, and two projects living in directories of the same
# name would otherwise take each other's firings.
#
# `host` names the machine a run was recorded on, because the log a run points
# at is a file on that machine and means nothing on any other.
LEDGER_OWNER = "automations"
LEDGER_MAJOR = 1
LEDGER_MINOR = 0
LEDGER_STEPS = [
    ("0001-runs", """
    CREATE TABLE automations_runs (
        id                text PRIMARY KEY,
        project_id        text NOT NULL,
        automation_slug   text NOT NULL,
        environment       text NOT NULL,
        trigger           text NOT NULL,
        scheduled_for     timestamptz,
        dedupe_key        text,
        status            text NOT NULL,
        attempt           integer NOT NULL DEFAULT 1,
        parent_run_id     text,
        queued_at         timestamptz NOT NULL,
        started_at        timestamptz,
        finished_at       timestamptz,
        pid               integer,
        exit_code         integer,
        summary           text,
        log_path          text NOT NULL,
        cancel_requested  boolean NOT NULL DEFAULT false,
        host              text NOT NULL,
        CONSTRAINT automations_runs_dedupe_key UNIQUE (project_id, dedupe_key)
    )
    """),
    ("0002-runs-status-index", """
    CREATE INDEX automations_runs_status_idx
        ON automations_runs (project_id, status, queued_at)
    """),
]


class StoreUnavailable(Exception):
    """The store the ledger lives in cannot be used: none is configured, its
    setting cannot be read, or the database cannot be reached or refuses the
    migration. `slug` is the shared library's, so every capability names one
    cause with one word, and `hint` says what to do about it."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug = slug
        self.message = message
        self.hint = hint


def _database():
    try:
        from capabilities_contract import db
    except ImportError as exc:
        raise StoreUnavailable(
            "driver_missing", "the shared database library is not installed",
            "reinstall automations, whose script header pins capabilities-contract") from exc
    return db


def store_in_force() -> dict[str, str]:
    """Which store the ledger uses, read and never written: the machine's store
    setting, or the `CAPABILITIES_STORE_URL` override, and the schema it binds.
    Raises StoreUnavailable when there is none to use."""
    db = _database()
    try:
        setting = db.read_setting()
    except db.DbError as exc:
        raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
    return {"store": "CAPABILITIES_STORE_URL" if setting.url is not None else "setting",
            "schema": setting.schema}


def open_ledger(root: Path, config: dict[str, Any], strict: bool = True) -> "RunLedger":
    """The ledger of this project's runs, open on the store. The caller closes it.

    A run is recorded under the project's id, the one the launching CLI resolves
    for a write. A read path passes `strict` False and reads under the id the
    project declares even where it may not be stamped; a project with no id at
    all reads as empty, never as every project."""
    identity = _project_identity(root)
    project_id = _registration_id(root, identity, strict)
    if not strict:
        project_id = project_id or _handed_project_id(root) or identity.get("id") or ""
    elif not project_id:
        raise ConfigError("this project declares no id to record its runs under")
    return RunLedger(project_id, config["engine"]["environment"]).open()


class RunLedger:
    """Every question and every claim about runs, in one place that knows whose.

    The table is shared: one database holds the runs of every project on every
    machine, so a query that forgot which project it is asking about would answer
    with another project's runs. Scoping cannot therefore be a `WHERE` clause
    fifteen callers are trusted to remember — it is the reason this boundary
    exists, and it is applied here once rather than at each call.

    Rows come back as they always have: times as the ISO text they were written
    as, and the cancel flag as 0 or 1, so what `runs` and `show` print does not
    depend on how the store keeps them."""

    def __init__(self, project_id: str, environment: str):
        self.project_id = project_id
        self.environment = environment
        self.host = socket.gethostname()
        self.conn = None
        self.warnings: list[str] = []
        self._in_transaction = False

    def open(self) -> "RunLedger":
        db = _database()
        try:
            conn = db.connect(application_name=LEDGER_OWNER)
        except db.DbError as exc:
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
        try:
            result = db.migrate(conn, LEDGER_OWNER, LEDGER_STEPS,
                                major=LEDGER_MAJOR, minor=LEDGER_MINOR)
            conn.autocommit = True
        except db.DbError as exc:
            conn.close()
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
        except BaseException:
            conn.close()
            raise
        self.conn = conn
        self.warnings = list(result.warnings)
        return self

    def close(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.close()
            self.conn = None
        self._in_transaction = False

    def __enter__(self) -> "RunLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lost(self) -> bool:
        """Whether the connection is gone, so the next statement cannot succeed
        on it whatever it is."""
        return self.conn is None or self.conn.closed or self.conn.broken

    def reopen(self) -> None:
        self.close()
        self.open()

    def _execute(self, sql: str, params: Sequence[Any] = ()):
        return self.conn.execute(sql, list(params))

    @contextlib.contextmanager
    def transaction(self):
        """One unit of work, joined rather than nested when already inside one,
        so a write made while a claim holds its lock commits with that claim."""
        if self._in_transaction:
            yield
            return
        with self.conn.transaction():
            self._in_transaction = True
            try:
                yield
            finally:
                self._in_transaction = False

    # -- the scope, applied once ----------------------------------------------

    def _where(self, *predicates: str) -> tuple[str, list]:
        return " WHERE " + " AND ".join(("project_id = %s", *predicates)), [self.project_id]

    @staticmethod
    def _value(value: Any) -> Any:
        if isinstance(value, datetime):
            return iso(value)
        if isinstance(value, bool):
            return int(value)
        return value

    @classmethod
    def _dicts(cls, cursor) -> list[dict[str, Any]]:
        names = [c[0] for c in cursor.description]
        return [{name: cls._value(value) for name, value in zip(names, row)}
                for row in cursor.fetchall()]

    def _rows(self, predicates: str = "", params: Sequence[Any] = (),
              order: str = "", limit: int | None = None,
              lock: bool = False) -> list[dict[str, Any]]:
        clause, scope_params = self._where(*([predicates] if predicates else []))
        sql = "SELECT * FROM automations_runs" + clause + (f" ORDER BY {order}" if order else "")
        args = scope_params + list(params)
        if limit is not None:
            sql += " LIMIT %s"
            args.append(limit)
        if lock:
            sql += " FOR UPDATE"
        return self._dicts(self._execute(sql, args))

    # -- reads ----------------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows("id = %s", (run_id,))
        return rows[0] if rows else None

    def list(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._rows("status = %s", (status,), order="queued_at DESC", limit=limit)
        return self._rows(order="queued_at DESC", limit=limit)

    def counts(self) -> dict[str, int]:
        clause, params = self._where()
        rows = self._execute(
            "SELECT status, COUNT(*) AS count FROM automations_runs" + clause
            + " GROUP BY status", params).fetchall()
        return {row[0]: row[1] for row in rows}

    def unfinished(self) -> list[dict[str, Any]]:
        return self._rows("status IN ('starting', 'running')")

    def pending(self, *, lock: bool = False) -> list[dict[str, Any]]:
        """The queue in order. `lock` holds the rows for the transaction it is
        read in, so two dispatchers cannot take the same one."""
        return self._rows("status = 'pending'", order="queued_at, id", lock=lock)

    def has_active(self, slug: str, statuses: Sequence[str]) -> bool:
        clause, params = self._where("automation_slug = %s", "status = ANY(%s)")
        row = self._execute("SELECT 1 FROM automations_runs" + clause + " LIMIT 1",
                            params + [slug, list(statuses)]).fetchone()
        return row is not None

    def count_for(self, slug: str, status: str) -> int:
        clause, params = self._where("automation_slug = %s", "status = %s")
        return self._execute("SELECT COUNT(*) FROM automations_runs" + clause,
                             params + [slug, status]).fetchone()[0]

    def running(self) -> int:
        clause, params = self._where("status IN ('starting', 'running')")
        return self._execute("SELECT COUNT(*) FROM automations_runs" + clause,
                             params).fetchone()[0]

    # -- writes ---------------------------------------------------------------

    def claim(self, slug: str, state_dir: Path, *,
              trigger: str, scheduled_for: str | None = None,
              dedupe_key: str | None = None, attempt: int = 1,
              parent_run_id: str | None = None) -> dict[str, Any] | None:
        """Take one firing, or find it already taken.

        The key is the only conflict the insert treats as someone else's win.
        Anything else is a schema the code no longer matches, and a run that
        vanishes quietly is worse than one that fails loudly."""
        run_id = uuid.uuid4().hex
        log_path = state_dir / "runs" / f"{run_id}.log"
        with self.transaction():
            inserted = self._execute(
                "INSERT INTO automations_runs (id, project_id, automation_slug, "
                "environment, trigger, scheduled_for, dedupe_key, status, attempt, "
                "parent_run_id, queued_at, log_path, host) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s) "
                "ON CONFLICT (project_id, dedupe_key) DO NOTHING",
                (run_id, self.project_id, slug, self.environment, trigger,
                 scheduled_for, dedupe_key, attempt, parent_run_id, iso(),
                 str(log_path), self.host)).rowcount
        if not inserted:
            return None
        return self.get(run_id)

    def update(self, run_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{name} = %s" for name in columns)
        clause, params = self._where("id = %s")
        with self.transaction():
            self._execute(f"UPDATE automations_runs SET {assignments}" + clause,
                          list(columns.values()) + params + [run_id])

    def take(self, run_id: str) -> bool:
        """Move one pending run to starting, unless another dispatcher did."""
        clause, params = self._where("id = %s", "status = 'pending'")
        with self.transaction():
            return self._execute("UPDATE automations_runs SET status = 'starting'" + clause,
                                 params + [run_id]).rowcount == 1


def enqueue_manual(
    root: Path, config: dict[str, Any], state_dir: Path, automation_id: str
) -> dict[str, Any]:
    item = automation_map(config).get(automation_id)
    if item is None:
        raise KeyError(automation_id)
    if not applies(item, config["engine"]["environment"]):
        raise ConfigError(
            f"automation {automation_id!r} is disabled or not enabled for environment "
            f"{config['engine']['environment']!r}"
        )
    with open_ledger(root, config) as ledger:
        row = ledger.claim(automation_id, state_dir, trigger="manual")
    assert row is not None
    return row


def list_runs(root: Path, config: dict[str, Any], *, limit: int = 50,
              status: str | None = None) -> list[dict[str, Any]]:
    with open_ledger(root, config, strict=False) as ledger:
        return ledger.list(limit=limit, status=status)


def get_run(root: Path, config: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    with open_ledger(root, config, strict=False) as ledger:
        return ledger.get(run_id)


def counts(root: Path, config: dict[str, Any]) -> dict[str, int]:
    with open_ledger(root, config, strict=False) as ledger:
        return ledger.counts()


def request_cancel(root: Path, config: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    with open_ledger(root, config) as ledger:
        row = ledger.get(run_id)
        if row is None:
            return None
        if row["status"] == "pending":
            ledger.update(run_id, status="canceled", cancel_requested=True, finished_at=iso())
        elif row["status"] in {"starting", "running"}:
            ledger.update(run_id, cancel_requested=True)
        return ledger.get(run_id)


def retry_run(root: Path, config: dict[str, Any], state_dir: Path,
              run_id: str) -> dict[str, Any] | None:
    with open_ledger(root, config) as ledger:
        row = ledger.get(run_id)
        if row is None:
            return None
        if row["status"] not in FINAL_STATUSES:
            raise ConfigError(f"run {run_id} is still {row['status']}")
        slug = row["automation_slug"]
        item = automation_map(config).get(slug)
        if item is None or not applies(item, config["engine"]["environment"]):
            raise ConfigError(
                f"automation {slug!r} is missing, disabled, or outside "
                f"environment {config['engine']['environment']!r}"
            )
        return ledger.claim(
            slug, state_dir, trigger="retry", attempt=int(row["attempt"]) + 1,
            parent_run_id=row["parent_run_id"] or row["id"])


def _summary(log_path: Path) -> str | None:
    try:
        lines = [line.strip() for line in log_path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return None
    return lines[-1][-1000:] if lines else None


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)


@dataclass
class Child:
    run_id: str
    automation_id: str
    process: subprocess.Popen[Any]
    log_handle: Any
    log_path: Path
    timeout_seconds: float
    started_monotonic: float
    stopping_at: float | None = None
    stop_reason: str | None = None


class Daemon:
    def __init__(self, root: Path, config_path: Path, state_dir: Path):
        self.root = root.resolve()
        self.config_path = config_path.resolve()
        self.state_dir = state_dir.resolve()
        self.config = load_effective_config(self.root, self.config_path,
                                            self.state_dir)
        self.by_id = automation_map(self.config)
        self.children: dict[str, Child] = {}
        self.stop_requested = False
        self.reload_requested = False
        self.reload_error: str | None = None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "runs").mkdir(parents=True, exist_ok=True)
        self.runs = open_ledger(self.root, self.config)
        for warning in self.runs.warnings:
            _say(warning)

    def _claim(self, slug: str, **kwargs) -> dict[str, Any] | None:
        return self.runs.claim(slug, self.state_dir, **kwargs)

    def recover(self) -> None:
        rows = self.runs.unfinished()
        live_pids = [int(row["pid"]) for row in rows if row["pid"]]
        for pid in live_pids:
            _signal_group(pid, signal.SIGTERM)
        if live_pids:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                remaining = []
                for pid in live_pids:
                    try:
                        os.kill(pid, 0)
                        remaining.append(pid)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        remaining.append(pid)
                if not remaining:
                    break
                live_pids = remaining
                time.sleep(0.05)
            for pid in live_pids:
                _signal_group(pid, signal.SIGKILL)
        for row in rows:
            self.runs.update(row["id"], status="interrupted", finished_at=iso(), pid=None,
                             summary="daemon restarted while run was active")
            slug = row["automation_slug"]
            item = self.by_id.get(slug)
            if (
                self.config["engine"]["recovery"] == "retry"
                and item is not None
                and applies(item, self.config["engine"]["environment"])
            ):
                self._claim(slug, trigger="recovery", attempt=int(row["attempt"]) + 1,
                            parent_run_id=row["parent_run_id"] or row["id"])

    def _has_active(self, automation_id: str) -> bool:
        return self.runs.has_active(automation_id, ACTIVE_STATUSES)

    def schedule_due(self, now: datetime) -> None:
        engine = self.config["engine"]
        local = now.astimezone(ZoneInfo(engine["timezone"]))
        for item in self.config["automations"]:
            if not applies(item, engine["environment"]):
                continue
            scheduled: datetime | None = None
            if item["every_seconds"]:
                seconds = item["every_seconds"]
                scheduled = datetime.fromtimestamp(
                    int(now.timestamp()) // seconds * seconds, timezone.utc
                )
            elif item["schedule"] and cron_matches(item["schedule"], local):
                scheduled = local.replace(second=0, microsecond=0).astimezone(timezone.utc)
            if scheduled is None:
                continue
            scheduled_text = iso(scheduled)
            dedupe = ":".join(
                (engine["namespace"], engine["environment"], item["id"], scheduled_text)
            )
            if item["overlap"] == "skip" and self._has_active(item["id"]):
                self._insert_skipped(item["id"], scheduled_text, dedupe, "overlap policy")
                continue
            pending = self.runs.count_for(item["id"], "pending")
            if pending >= item["max_pending"]:
                self._insert_skipped(item["id"], scheduled_text, dedupe, "pending limit")
                continue
            self._claim(item["id"], trigger="schedule",
                        scheduled_for=scheduled_text, dedupe_key=dedupe)

    def _insert_skipped(self, automation_id: str, scheduled: str, dedupe: str, reason: str) -> None:
        row = self._claim(
            automation_id,
            trigger="schedule",
            scheduled_for=scheduled,
            dedupe_key=dedupe,
        )
        if row:
            self.runs.update(row["id"], status="skipped", finished_at=iso(), summary=reason)

    def _claim_one(self) -> dict[str, Any] | None:
        """Take the next pending run, under a lock so two dispatchers cannot
        take the same one. Every question inside asks the ledger, so the answers
        are about this project even where the table is shared."""
        with self.runs.transaction():
            pending = self.runs.pending(lock=True)
            if self.runs.running() >= self.config["engine"]["max_parallel"]:
                return None
            for row in pending:
                slug = row["automation_slug"]
                item = self.by_id.get(slug)
                if item is None or not applies(item, self.config["engine"]["environment"]):
                    self.runs.update(
                        row["id"], status="skipped", finished_at=iso(),
                        summary="automation missing, disabled, or outside environment")
                    continue
                if self.runs.count_for(slug, "starting") + \
                        self.runs.count_for(slug, "running") >= item["max_parallel"]:
                    continue
                if self.runs.take(row["id"]):
                    return row
            return None

    def dispatch(self) -> None:
        while len(self.children) < self.config["engine"]["max_parallel"]:
            row = self._claim_one()
            if row is None:
                return
            self._start(row)

    def _start(self, row: dict[str, Any]) -> None:
        item = self.by_id[row["automation_slug"]]
        script = item["script_path"]
        if not script.is_file():
            self._finish_without_child(row, "failed", None, f"script not found: {script}")
            return
        command = [str(script), *item["arguments"]]
        if script.suffix == ".py" and not os.access(script, os.X_OK):
            command = [sys.executable, str(script), *item["arguments"]]
        elif not os.access(script, os.X_OK):
            self._finish_without_child(row, "failed", None, f"script is not executable: {script}")
            return
        log_path = Path(row["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "AUTOMATION_ID": row["automation_slug"],
            "AUTOMATION_RUN_ID": row["id"],
            "AUTOMATION_ATTEMPT": str(row["attempt"]),
            "AUTOMATION_TRIGGER": row["trigger"],
            "AUTOMATION_ENVIRONMENT": row["environment"],
            "AUTOMATION_NAMESPACE": self.config["engine"]["namespace"],
            "AUTOMATION_PROJECT_ROOT": str(self.root),
            "AUTOMATION_STATE_DIR": str(self.state_dir),
            "AUTOMATIONS_BIN": automations_bin(),
        })
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.root),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            log_handle.close()
            self._finish_without_child(row, "failed", None, f"spawn failed: {exc}")
            return
        self.runs.update(row["id"], status="running", started_at=iso(), pid=proc.pid)
        self.children[row["id"]] = Child(
            run_id=row["id"],
            automation_id=row["automation_slug"],
            process=proc,
            log_handle=log_handle,
            log_path=log_path,
            timeout_seconds=item["timeout_seconds"],
            started_monotonic=time.monotonic(),
        )

    def _finish_without_child(
        self, row: dict[str, Any], status: str, exit_code: int | None, summary: str
    ) -> None:
        self.runs.update(row["id"], status=status, finished_at=iso(), exit_code=exit_code,
                         summary=summary, pid=None)
        self._maybe_retry(row["id"])

    def reap(self) -> None:
        now_mono = time.monotonic()
        for run_id, child in list(self.children.items()):
            row = self.runs.get(run_id)
            cancel = bool(row and row["cancel_requested"])
            timed_out = now_mono - child.started_monotonic >= child.timeout_seconds
            if child.process.poll() is None and child.stopping_at is None and (cancel or timed_out):
                child.stop_reason = "canceled" if cancel else "timeout"
                child.stopping_at = now_mono
                _signal_group(child.process.pid, signal.SIGTERM)
            if (
                child.process.poll() is None
                and child.stopping_at is not None
                and now_mono - child.stopping_at >= 5.0
            ):
                _signal_group(child.process.pid, signal.SIGKILL)
            code = child.process.poll()
            if code is None:
                continue
            child.log_handle.close()
            if child.stop_reason == "canceled":
                status, summary = "canceled", "canceled by request"
            elif child.stop_reason == "timeout":
                status, summary = "failed", f"timed out after {child.timeout_seconds:g}s"
            elif code == 0:
                status, summary = "succeeded", _summary(child.log_path)
            else:
                status, summary = "failed", _summary(child.log_path) or f"exited {code}"
            self.runs.update(run_id, status=status, finished_at=iso(), exit_code=code,
                             summary=summary, pid=None)
            del self.children[run_id]
            if status == "failed":
                self._maybe_retry(run_id)

    def _maybe_retry(self, run_id: str) -> None:
        row = self.runs.get(run_id)
        if row is None:
            return
        slug = row["automation_slug"]
        item = self.by_id.get(slug)
        if item is None or int(row["attempt"]) > item["retries"]:
            return
        self._claim(slug, trigger="retry", attempt=int(row["attempt"]) + 1,
                    parent_run_id=row["parent_run_id"] or row["id"])

    def shutdown(self) -> None:
        grace = self.config["engine"]["shutdown_grace_seconds"]
        for child in self.children.values():
            child.stop_reason = "interrupted"
            child.stopping_at = time.monotonic()
            _signal_group(child.process.pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while self.children and time.monotonic() < deadline:
            for run_id, child in list(self.children.items()):
                code = child.process.poll()
                if code is None:
                    continue
                child.log_handle.close()
                self.runs.update(run_id, status="interrupted", finished_at=iso(),
                                 exit_code=code, summary="daemon stopped", pid=None)
                del self.children[run_id]
            time.sleep(0.05)
        for run_id, child in list(self.children.items()):
            _signal_group(child.process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                child.process.wait(timeout=2)
            child.log_handle.close()
            self.runs.update(run_id, status="interrupted", finished_at=iso(),
                             summary="daemon stopped", pid=None)
            del self.children[run_id]

    def reload_declaration(self, fingerprint_path: Path) -> None:
        """Take up a declaration edited since start, without dropping work.

        A daemon reads its declaration once, so everything written after it
        started is declared and not in force. This closes that gap between two
        ticks, which is the only moment at which nothing is being decided.

        The new declaration is proved loadable before it replaces anything, so a
        malformed edit leaves a healthy daemon scheduling exactly what it already
        had. Children are subprocesses held by run id and the swap does not touch
        them: work already dispatched finishes under the declaration that started
        it, which is the whole reason to reload rather than restart.

        The published fingerprint moves last and only on success, so nothing can
        report itself current while running something else.
        """
        try:
            config = load_effective_config(self.root, self.config_path, self.state_dir)
        except Exception as exc:
            self.reload_error = str(exc)
            sys.stderr.write(
                f"automations daemon: reload rejected, keeping the loaded "
                f"configuration: {exc}\n"
            )
            sys.stderr.flush()
            return
        self.config = config
        self.by_id = automation_map(config)
        self.reload_error = None
        fingerprint_path.write_text(daemon_record(config))
        sys.stderr.write(
            f"automations daemon: reloaded, {len(self.by_id)} automations declared\n"
        )
        sys.stderr.flush()

    def run(self) -> None:
        import fcntl

        lock_path = self.state_dir / "daemon.lock"
        pid_path = self.state_dir / "daemon.pid"
        fingerprint_path = self.state_dir / DAEMON_FINGERPRINT_FILE
        lock = lock_path.open("a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another automations daemon holds {lock_path}") from exc
        pid_path.write_text(f"{os.getpid()}\n")
        # Written behind the lock and beside the pid, because it answers for the
        # same process: which configuration the daemon still running loaded, and
        # which environment it loaded it under.
        fingerprint_path.write_text(daemon_record(self.config))
        self.recover()

        def stop(_signum: int, _frame: Any) -> None:
            self.stop_requested = True

        def reload(_signum: int, _frame: Any) -> None:
            self.reload_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, reload)
        try:
            while not self.stop_requested:
                if self.reload_requested:
                    self.reload_requested = False
                    self.reload_declaration(fingerprint_path)
                self.tick()
                time.sleep(self.config["engine"]["tick_seconds"])
        finally:
            self.shutdown()
            pid_path.unlink(missing_ok=True)
            fingerprint_path.unlink(missing_ok=True)
            self.runs.close()
            lock.close()

    def tick(self) -> None:
        """Reap, schedule and dispatch once.

        The store is across a network, and a connection it drops is no reason
        to end the children this daemon is running: a tick that finds its
        connection gone says so, opens a new one if it can, and leaves the work
        to the next tick, whose dedupe keys make a repeated firing harmless."""
        try:
            self.reap()
            self.schedule_due(utc_now())
            self.dispatch()
        except Exception as exc:
            if not self.runs.lost():
                raise
            _say(f"lost the store connection, reconnecting: {exc}")
            try:
                self.runs.reopen()
            except StoreUnavailable as again:
                _say(f"the store is still unavailable: {again.message}")


def _say(message: str) -> None:
    sys.stderr.write(f"automations daemon: {message}\n")
    sys.stderr.flush()


def run_from_env() -> None:
    root_raw = os.environ.get("AUTOMATIONS_PROJECT_ROOT")
    config_raw = os.environ.get("AUTOMATIONS_CONFIG")
    state_raw = os.environ.get("AUTOMATIONS_STATE_DIR")
    if not root_raw or not config_raw or not state_raw:
        raise RuntimeError(
            "AUTOMATIONS_PROJECT_ROOT, AUTOMATIONS_CONFIG, and AUTOMATIONS_STATE_DIR are required"
        )
    Daemon(Path(root_raw), Path(config_raw), Path(state_raw)).run()


if __name__ == "__main__":
    try:
        run_from_env()
    except StoreUnavailable as exc:
        sys.stderr.write(f"automations daemon: {exc.slug}: {exc.message}"
                         + (f"; {exc.hint}" if exc.hint else "") + "\n")
        raise SystemExit(1)
    except Exception as exc:
        sys.stderr.write(f"automations daemon: {exc}\n")
        raise SystemExit(1)
