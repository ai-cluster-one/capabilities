from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
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
AGENT_MODES = ("read", "write")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
AGENT_KEYS = frozenset(
    {"engine", "model", "effort", "mode", "timeout_seconds", "service_tier"}
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
# environment, the pending count against `runs` — which is the test for whether
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
    # The ledger. It differs from the one a file-mode project keeps in its own
    # SQLite in two ways, and both are the point: a run names its project, and
    # it names its automation by id rather than by the label a person types.
    #
    # `dedupe_key` was already project, environment, automation and scheduled
    # time, and already unique. On a store two machines share, that uniqueness
    # stops being bookkeeping and becomes the thing that keeps them from both
    # firing one schedule: whichever inserts first wins and the other is told.
    """
    CREATE TABLE IF NOT EXISTS runs (
        id                TEXT PRIMARY KEY,
        project_id        TEXT REFERENCES projects(id),
        -- The uuid when the automation is a row in this store, and null when
        -- the project still declares its automations in a file. The slug is
        -- always there, because a run has to say what it ran whether or not
        -- anything else knows about it.
        automation_id     TEXT REFERENCES automations(id),
        automation_slug   TEXT NOT NULL,
        environment       TEXT NOT NULL,
        trigger           TEXT NOT NULL,
        scheduled_for     TEXT,
        dedupe_key        TEXT UNIQUE,
        status            TEXT NOT NULL,
        attempt           INTEGER NOT NULL DEFAULT 1,
        parent_run_id     TEXT,
        queued_at         TEXT NOT NULL,
        started_at        TEXT,
        finished_at       TEXT,
        pid               INTEGER,
        exit_code         INTEGER,
        summary           TEXT,
        log_path          TEXT NOT NULL,
        cancel_requested  INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status, queued_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS runs_automation_idx ON runs (automation_id, queued_at)
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


def read_config_fingerprint(state_dir: Path) -> str | None:
    """What the running daemon recorded, or None if it recorded nothing."""
    try:
        return (state_dir / DAEMON_FINGERPRINT_FILE).read_text().strip() or None
    except OSError:
        return None


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
            "id": row[0], "enabled": bool(row[3]),
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
        automations.append({
            "id": automation_id,
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


def open_ledger(root: Path, config: dict[str, Any]):
    """The store this project's runs live in, and the ledger onto it.

    There is no file-mode ledger any more. A run, a queue, a cursor are records
    nobody authors and nobody reviews, so a capability keeping its own database
    for them was only ever the easy thing; the store exists unconditionally and
    this is where automations joins it. The caller closes the store."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import store as _store
    except ImportError as exc:
        raise ConfigError("the store module is not installed beside the service") from exc
    finally:
        sys.path.pop(0)
    identity = _project_identity(root)
    st = _store.open_store()
    try:
        st.migrate()
        st.project_register(identity["id"], identity["slug"])
        st.migrate(STORE_NAMESPACE, STORE_VERSION, STORE_MIGRATIONS)
    except _store.StoreError as exc:
        st.close()
        raise ConfigError(f"cannot prepare the store: {exc.message}") from exc
    project_id = st._project_id(identity["slug"])
    return st, RunLedger(st._conn, project_id, config["engine"]["environment"])


class RunLedger:
    """Every question and every claim about runs, in one place that knows whose.

    The ledger is shared: on a store two machines write to, a query that forgets
    which project it is asking about would answer with another project's runs.
    Scoping cannot therefore be a `WHERE` clause fifteen callers are trusted to
    remember — it is the reason this boundary exists, and it is applied here
    once rather than at each call.

    The same class serves a local SQLite store and a coordinated backend; the
    project scope and query shape do not change with the store deployment."""

    def __init__(self, db, project_id: str | None, environment: str):
        self.db = db
        self.project_id = project_id
        self.environment = environment
        self._scoped = project_id is not None

    # -- the scope, applied once ----------------------------------------------

    def _where(self, *predicates: str) -> tuple[str, list]:
        parts, params = [], []
        if self._scoped:
            parts.append("project_id = ?")
            params.append(self.project_id)
        parts.extend(predicates)
        return (" WHERE " + " AND ".join(parts)) if parts else "", params

    @staticmethod
    def _dicts(cursor) -> list[dict[str, Any]]:
        """Rows as dicts without depending on a row factory. The ledger runs
        against a bare store connection and, later, against a driver that has no
        such notion at all, so the column names come from the cursor."""
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _rows(self, predicates: str = "", params: Sequence[Any] = (),
              order: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        clause, scope_params = self._where(*([predicates] if predicates else []))
        sql = "SELECT * FROM runs" + clause + (f" ORDER BY {order}" if order else "")
        args = scope_params + list(params)
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return self._dicts(self.db.execute(sql, tuple(args)))

    # -- reads ----------------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows("id = ?", (run_id,))
        return rows[0] if rows else None

    def list(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._rows("status = ?", (status,), order="queued_at DESC", limit=limit)
        return self._rows(order="queued_at DESC", limit=limit)

    def counts(self) -> dict[str, int]:
        clause, params = self._where()
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS count FROM runs" + clause + " GROUP BY status",
            tuple(params)).fetchall()
        return {row[0]: row[1] for row in rows}

    def unfinished(self) -> list[dict[str, Any]]:
        return self._rows("status IN ('starting', 'running')")

    def pending(self) -> list[dict[str, Any]]:
        return self._rows("status = 'pending'", order="queued_at, id")

    def has_active(self, slug: str, statuses: Sequence[str]) -> bool:
        marks = ", ".join("?" for _ in statuses)
        clause, params = self._where("automation_slug = ?", f"status IN ({marks})")
        row = self.db.execute("SELECT 1 FROM runs" + clause + " LIMIT 1",
                              tuple(params + [slug] + list(statuses))).fetchone()
        return row is not None

    def count_for(self, slug: str, status: str) -> int:
        clause, params = self._where("automation_slug = ?", "status = ?")
        return self.db.execute("SELECT COUNT(*) FROM runs" + clause,
                               tuple(params + [slug, status])).fetchone()[0]

    def running(self) -> int:
        clause, params = self._where("status IN ('starting', 'running')")
        return self.db.execute("SELECT COUNT(*) FROM runs" + clause,
                               tuple(params)).fetchone()[0]

    # -- writes ---------------------------------------------------------------

    def claim(self, automation_uuid: str | None, slug: str, state_dir: Path, *,
              trigger: str, scheduled_for: str | None = None,
              dedupe_key: str | None = None, attempt: int = 1,
              parent_run_id: str | None = None) -> dict[str, Any] | None:
        """Take one firing, or find it already taken.

        `dedupe_key` is unique, so on a shared store this is the whole of the
        mutual exclusion: whichever machine inserts first owns the run and the
        other is told, rather than discovering the collision by running."""
        run_id = uuid.uuid4().hex
        log_path = state_dir / "runs" / f"{run_id}.log"
        try:
            self.db.execute(
                "INSERT INTO runs (id, project_id, automation_id, automation_slug, "
                "environment, trigger, scheduled_for, dedupe_key, status, attempt, "
                "parent_run_id, queued_at, log_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (run_id, self.project_id, automation_uuid, slug,
                 self.environment, trigger, scheduled_for, dedupe_key, attempt,
                 parent_run_id, iso(), str(log_path)))
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            if dedupe_key is not None and self._dedupe_taken(dedupe_key):
                return None
            raise
        return self.get(run_id)

    def _dedupe_taken(self, dedupe_key: str) -> bool:
        """Whether the key is already on a row, which is the only integrity
        failure this insert is allowed to treat as someone else's win. Anything
        else -- a column this store still declares NOT NULL, a reference it
        cannot satisfy -- is a schema the code no longer matches, and a run that
        vanishes quietly is worse than one that fails loudly."""
        row = self.db.execute("SELECT 1 FROM runs WHERE dedupe_key = ? LIMIT 1",
                              (dedupe_key,)).fetchone()
        return row is not None

    def update(self, run_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        clause, params = self._where("id = ?")
        self.db.execute(f"UPDATE runs SET {assignments}" + clause,
                        tuple(list(columns.values()) + params + [run_id]))
        self.db.commit()


def runs_schema_defect(store) -> str | None:
    """The name of a column this store still declares NOT NULL that the code
    expects to leave empty, or None when the table matches.

    `runs.automation_id` is empty for a project that declares its automations in
    a file: there is no row for it to point at. A store first created before
    that was true still carries the old NOT NULL, and `CREATE TABLE IF NOT
    EXISTS` never revisits a table that already exists, so the constraint
    outlives the release that relaxed it and no run can be recorded at all.
    """
    try:
        columns = store._execute("PRAGMA table_info(runs)").fetchall()
    except Exception:
        return None
    for column in columns:
        name, notnull = column[1], column[3]
        if name == "automation_id" and notnull:
            return name
    return None


def repair_runs_schema(store) -> dict[str, Any]:
    """Rebuild `runs` so the automation reference may be empty, keeping rows.

    This is a repair, not a migration: `migrate()` refuses a step that drops or
    renames, because a host on an older release shares this database and has to
    survive whatever a newer one does to it. Widening a column is safe for that
    older host -- it simply never writes an empty one -- but SQLite cannot widen
    in place, and the rebuild that does it is exactly the shape the guard
    refuses to take on trust. So it is asked for deliberately, once, by someone
    who has read what it will do.
    """
    defect = runs_schema_defect(store)
    if defect is None:
        return {"repaired": False, "reason": "runs already allows an empty automation reference"}
    create = [step for step in STORE_MIGRATIONS
              if "CREATE TABLE IF NOT EXISTS runs " in " ".join(step.split())]
    indexes = [step for step in STORE_MIGRATIONS
               if "CREATE INDEX" in step.upper() and " ON RUNS " in " ".join(step.split()).upper()]
    with store.transaction():
        store._execute("ALTER TABLE runs RENAME TO runs_pre_repair")
        for step in create:
            store._execute(step.format(**store._ddl_subs()))
        columns = [row[1] for row in store._execute("PRAGMA table_info(runs)").fetchall()]
        names = ", ".join(columns)
        store._execute(f"INSERT INTO runs ({names}) SELECT {names} FROM runs_pre_repair")
        moved = store._execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        # The old table's indexes carry its name and go down with it, so they are
        # rebuilt from the same steps that declare them rather than left missing.
        store._execute("DROP TABLE runs_pre_repair")
        for step in indexes:
            store._execute(step.format(**store._ddl_subs()))
    return {"repaired": True, "column": defect, "rows_preserved": moved}


def _automation_uuid(store, project_id: str | None, slug: str) -> str | None:
    row = store._execute(
        "SELECT id FROM automations WHERE scope = 'project' AND project_id = ? AND slug = ?",
        (project_id, slug)).fetchone()
    return row[0] if row else None


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
    store, ledger = open_ledger(root, config)
    try:
        row = ledger.claim(_automation_uuid(store, ledger.project_id, automation_id),
                           automation_id, state_dir, trigger="manual")
    finally:
        store.close()
    assert row is not None
    return row


def list_runs(root: Path, config: dict[str, Any], *, limit: int = 50,
              status: str | None = None) -> list[dict[str, Any]]:
    store, ledger = open_ledger(root, config)
    try:
        return ledger.list(limit=limit, status=status)
    finally:
        store.close()


def get_run(root: Path, config: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    store, ledger = open_ledger(root, config)
    try:
        return ledger.get(run_id)
    finally:
        store.close()


def counts(root: Path, config: dict[str, Any]) -> dict[str, int]:
    store, ledger = open_ledger(root, config)
    try:
        return ledger.counts()
    finally:
        store.close()


def request_cancel(root: Path, config: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    store, ledger = open_ledger(root, config)
    try:
        row = ledger.get(run_id)
        if row is None:
            return None
        if row["status"] == "pending":
            ledger.update(run_id, status="canceled", cancel_requested=1, finished_at=iso())
        elif row["status"] in {"starting", "running"}:
            ledger.update(run_id, cancel_requested=1)
        return ledger.get(run_id)
    finally:
        store.close()


def retry_run(root: Path, config: dict[str, Any], state_dir: Path,
              run_id: str) -> dict[str, Any] | None:
    store, ledger = open_ledger(root, config)
    try:
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
            _automation_uuid(store, ledger.project_id, slug), slug, state_dir,
            trigger="retry", attempt=int(row["attempt"]) + 1,
            parent_run_id=row["parent_run_id"] or row["id"])
    finally:
        store.close()


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
        self.store, self.runs = open_ledger(self.root, self.config)
        self.db = self.store._conn
        self._uuids = {
            item["id"]: _automation_uuid(self.store, self.runs.project_id, item["id"])
            for item in self.config["automations"]
        }

    def _claim(self, slug: str, **kwargs) -> dict[str, Any] | None:
        return self.runs.claim(self._uuids.get(slug), slug, self.state_dir, **kwargs)

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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.runs.running() >= self.config["engine"]["max_parallel"]:
                self.db.commit()
                return None
            for row in self.runs.pending():
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
                updated = self.db.execute(
                    "UPDATE runs SET status = 'starting' WHERE id = ? AND status = 'pending'",
                    (row["id"],)).rowcount
                if updated:
                    self.db.commit()
                    return row
            self.db.commit()
            return None
        except Exception:
            self.db.rollback()
            raise

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
        fingerprint_path.write_text(config_fingerprint(config) + "\n")
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
        # same process: which configuration the daemon still running loaded.
        fingerprint_path.write_text(config_fingerprint(self.config) + "\n")
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
                self.reap()
                self.schedule_due(utc_now())
                self.dispatch()
                time.sleep(self.config["engine"]["tick_seconds"])
        finally:
            self.shutdown()
            pid_path.unlink(missing_ok=True)
            fingerprint_path.unlink(missing_ok=True)
            self.store.close()
            lock.close()


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
    except Exception as exc:
        sys.stderr.write(f"automations daemon: {exc}\n")
        raise SystemExit(1)
