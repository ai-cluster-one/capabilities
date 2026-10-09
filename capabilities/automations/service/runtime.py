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


def load_effective_config(root: Path, config_path: Path,
                          state_dir: Path) -> dict[str, Any]:
    """The config the scheduler acts on: the project's config file."""
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


class StoreLink:
    """One held connection to the store the ledger lives in, migrated on open.

    A project's daemon holds one for its own ledger. The machine service holds
    one for every project it serves: each project's ledger is a view onto it,
    so the projects on a machine cost the store one connection rather than one
    each. The scheduler is single-threaded, so a transaction on the link is the
    only one open on it, whichever project's ledger opened it."""

    def __init__(self):
        self.conn = None
        self.warnings: list[str] = []
        self.in_transaction = False

    def open(self) -> "StoreLink":
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
        self.in_transaction = False

    def lost(self) -> bool:
        """Whether the connection is gone, so the next statement cannot succeed
        on it whatever it is."""
        return self.conn is None or self.conn.closed or self.conn.broken

    def reopen(self) -> None:
        self.close()
        self.open()


class RunLedger:
    """Every question and every claim about runs, in one place that knows whose.

    The table is shared: one database holds the runs of every project on every
    machine, so a query that forgot which project it is asking about would answer
    with another project's runs. Scoping cannot therefore be a `WHERE` clause
    fifteen callers are trusted to remember — it is the reason this boundary
    exists, and it is applied here once rather than at each call.

    A ledger opens a link of its own unless it is handed one; a handed link is
    its owner's to open, reopen and close.

    Rows come back as they always have: times as the ISO text they were written
    as, and the cancel flag as 0 or 1, so what `runs` and `show` print does not
    depend on how the store keeps them."""

    def __init__(self, project_id: str, environment: str, link: StoreLink | None = None):
        self.project_id = project_id
        self.environment = environment
        self.host = socket.gethostname()
        self._owns_link = link is None
        self.link = link if link is not None else StoreLink()

    @property
    def conn(self):
        return self.link.conn

    @property
    def warnings(self) -> list[str]:
        return self.link.warnings if self._owns_link else []

    def open(self) -> "RunLedger":
        if self._owns_link or self.link.conn is None:
            self.link.open()
        return self

    def close(self) -> None:
        if self._owns_link:
            self.link.close()

    def __enter__(self) -> "RunLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lost(self) -> bool:
        """Whether the connection is gone, so the next statement cannot succeed
        on it whatever it is."""
        return self.link.lost()

    def reopen(self) -> None:
        self.link.reopen()

    def _execute(self, sql: str, params: Sequence[Any] = ()):
        return self.conn.execute(sql, list(params))

    @contextlib.contextmanager
    def transaction(self):
        """One unit of work, joined rather than nested when already inside one,
        so a write made while a claim holds its lock commits with that claim."""
        if self.link.in_transaction:
            yield
            return
        with self.conn.transaction():
            self.link.in_transaction = True
            try:
                yield
            finally:
                self.link.in_transaction = False

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



# --- the service's files and its log -----------------------------------------

LOCK_FILE = "daemon.lock"
PID_FILE = "daemon.pid"
LOG_FILE = "daemon.log"
# The machine process's status, in its own state root. A project's state root
# carries the pid and the fingerprint in both modes, and no status file.
STATUS_FILE = "daemon.json"
# A project's pause, in its state root: written by `service pause`, removed by
# `service resume`, read on every pass in either mode. It is runtime state and
# not part of the declaration, so it moves no fingerprint and outlives a restart.
PAUSE_FILE = "paused.json"

# The tag on every line the machine process writes about itself, its store or
# its cap; a line about one project carries that project's slug.
MACHINE_TAG = "machine"
# The longest the machine process goes without publishing its status, so a
# probe can tell a process that is alive from one that hangs.
PUBLISH_SECONDS = 30.0
# How often a project on the opt-in list that is not served yet is asked again
# whether it can be: its folder, its identity, its enable, its lock.
ADMIT_SECONDS = 1.0
# The longest the machine process sleeps between passes.
LONGEST_SLEEP_SECONDS = 1.0


def log_line(project: str, message: str) -> str:
    """One service log line, `<moment> automations service [<project>]:
    <message>`, the project named by its slug. A message that spans lines, as a
    store's error may, is written on one, so every line names its project."""
    message = " ".join(part.strip() for part in str(message).splitlines() if part.strip())
    return f"{iso()} automations service [{project}]: {message}\n"


def append_line(path: Path, line: str) -> None:
    with contextlib.suppress(OSError):
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(line)


def write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    spare = path.with_name(f".{path.name}.{os.getpid()}")
    spare.write_text(text)
    os.replace(spare, path)


def take_lock(state_dir: Path):
    """The one-process lock of a state root, taken without waiting: the open
    file holding it, or None while another holder has it. A project's lock is
    the same file in both modes, so it is what keeps a project-mode daemon and
    the machine service from serving one project at once."""
    import fcntl

    Path(state_dir).mkdir(parents=True, exist_ok=True)
    handle = (Path(state_dir) / LOCK_FILE).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def read_pid(state_dir: Path) -> int | None:
    try:
        return int((Path(state_dir) / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_status(state_dir: Path) -> dict | None:
    try:
        found = json.loads((Path(state_dir) / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def read_pause(state_dir: Path) -> dict | None:
    """The project's pause, or None while it is not paused. A pause file that
    does not read is still a pause: a scheduler that cannot tell whether it was
    told to hold holds."""
    path = Path(state_dir) / PAUSE_FILE
    if not path.exists():
        return None
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        found = None
    if not isinstance(found, dict):
        found = {}
    return {key: found.get(key) if isinstance(found.get(key), str) else None
            for key in ("reason", "at", "by")}


def pause_words(pause: dict) -> str:
    said = "paused"
    if pause.get("by"):
        said += f" by {pause['by']}"
    if pause.get("at"):
        said += f" at {pause['at']}"
    if pause.get("reason"):
        said += f": {pause['reason']}"
    return said


def _why(exc: BaseException) -> str:
    if isinstance(exc, StoreUnavailable):
        return f"{exc.slug}: {exc.message}"
    if isinstance(exc, ConfigError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _project_slug(root: Path) -> str:
    try:
        slug = json.loads((root / "capabilities" / "project.json").read_text()).get("slug")
    except (OSError, ValueError, AttributeError):
        slug = None
    return slug.strip() if isinstance(slug, str) and slug.strip() else root.name


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
    kill_at: float | None = None
    stop_summary: str | None = None
    started_at: str = ""
    started: bool = False
    outcome: dict[str, Any] | None = None


class Daemon:
    """One project's scheduler: its declaration, its ledger and its running jobs.

    In project mode a process runs one, through `run`. The machine service holds
    one for every project it serves and drives the same methods - `recover`,
    `reap`, `take_pause`, `schedule_due`, `dispatch`, `reload_declaration`,
    `shutdown` - so what is registered, how a job starts and how it ends are one
    code in both modes. What the machine service hands in is what is the
    machine's to hand: the project's ledger on its one shared store link, the
    loader that reads the project's declaration as the project itself resolves
    it, the launcher that starts each job inside the project's own environment,
    and the log, which names the project on every line."""

    def __init__(self, root: Path, config_path: Path, state_dir: Path, *,
                 slug: str | None = None, loader=None, runs: RunLedger | None = None,
                 launcher=None, log=None):
        self.root = root.resolve()
        self.config_path = config_path.resolve()
        self.state_dir = state_dir.resolve()
        self.slug = slug or _project_slug(self.root)
        self._loader = loader
        self._launcher = launcher
        self._log = log
        self.config = self.load()
        self.by_id = automation_map(self.config)
        self.children: dict[str, Child] = {}
        self.stop_requested = False
        self.reload_requested = False
        self.reload_error: str | None = None
        self.pause: dict | None = None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "runs").mkdir(parents=True, exist_ok=True)
        if runs is None:
            self.runs = open_ledger(self.root, self.config)
        else:
            self.runs = runs
            self.runs.environment = self.config["engine"]["environment"]
        for warning in self.runs.warnings:
            self.log(warning)

    def load(self) -> dict[str, Any]:
        if self._loader is not None:
            return self._loader()
        return load_effective_config(self.root, self.config_path, self.state_dir)

    def log(self, message: str) -> None:
        if self._log is not None:
            self._log(message)
            return
        sys.stderr.write(log_line(self.slug, message))
        sys.stderr.flush()

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

    def take_pause(self) -> None:
        """Read the pause, and say so when it comes or goes."""
        pause = read_pause(self.state_dir)
        if pause is not None and self.pause is None:
            self.log(f"{pause_words(pause)}; registering and starting nothing until "
                     "`automations service resume`, and running jobs finish")
        elif pause is None and self.pause is not None:
            self.log("resumed")
        self.pause = pause

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

    def dispatch(self, limit: int | None = None) -> int:
        """Start pending runs within the project's own limits, at most `limit`
        of them when the machine's cap deals them one at a time, and say how
        many were taken."""
        taken = 0
        while len(self.children) < self.config["engine"]["max_parallel"] \
                and (limit is None or taken < limit):
            row = self._claim_one()
            if row is None:
                break
            self._start(row)
            taken += 1
        return taken

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
        # In project mode this process already carries the project's
        # environment and the job is started as declared. The machine service
        # carries none, so its launcher puts the project's own environment
        # under the job in a process of the job's own, and execs into it.
        argv = self._launcher(command) if self._launcher is not None else command
        try:
            proc = subprocess.Popen(
                argv,
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
        # Held before it is recorded: a job whose running write fails is still
        # this daemon's to time out and stop, and `reap` writes it again.
        child = self.children[row["id"]] = Child(
            run_id=row["id"],
            automation_id=row["automation_slug"],
            process=proc,
            log_handle=log_handle,
            log_path=log_path,
            timeout_seconds=item["timeout_seconds"],
            started_monotonic=time.monotonic(),
            started_at=iso(),
        )
        self._record(child)

    def _finish_without_child(
        self, row: dict[str, Any], status: str, exit_code: int | None, summary: str
    ) -> None:
        self.runs.update(row["id"], status=status, finished_at=iso(), exit_code=exit_code,
                         summary=summary, pid=None)
        self._maybe_retry(row["id"])

    def reap(self) -> None:
        """Stop what overran or was canceled, then record what ended.

        Only the cancel flags come from the store, and a read that fails defers
        cancels and nothing else. An outcome the store does not take stays held
        and is written again on the next reap; the first failure is raised once
        every job has been seen to, so a tick still learns its store is gone."""
        failure: Exception | None = None
        cancels: set[str] = set()
        for run_id, child in self.children.items():
            if child.outcome is not None or child.stopping_at is not None:
                continue
            try:
                row = self.runs.get(run_id)
            except Exception as exc:
                failure = exc
                break
            if row and row["cancel_requested"]:
                cancels.add(run_id)
        self.supervise(cancels)
        for child in list(self.children.values()):
            try:
                self._record(child)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def supervise(self, cancels: set[str] = frozenset()) -> None:
        """Signal, time out and kill from this process's own table of jobs, and
        note how each that ended did. It asks the store nothing."""
        now_mono = time.monotonic()
        for run_id, child in self.children.items():
            if child.outcome is not None:
                continue
            cancel = run_id in cancels
            timed_out = now_mono - child.started_monotonic >= child.timeout_seconds
            if child.process.poll() is None and child.stopping_at is None and (cancel or timed_out):
                child.stop_reason = "canceled" if cancel else "timeout"
                child.stopping_at = now_mono
                child.kill_at = now_mono + 5.0
                _signal_group(child.process.pid, signal.SIGTERM)
            if (
                child.process.poll() is None
                and child.kill_at is not None
                and now_mono >= child.kill_at
            ):
                _signal_group(child.process.pid, signal.SIGKILL)
            code = child.process.poll()
            if code is None:
                continue
            child.log_handle.close()
            if child.stop_reason == "interrupted":
                status, summary = "interrupted", child.stop_summary or "daemon stopped"
            elif child.stop_reason == "canceled":
                status, summary = "canceled", "canceled by request"
            elif child.stop_reason == "timeout":
                status, summary = "failed", f"timed out after {child.timeout_seconds:g}s"
            elif code == 0:
                status, summary = "succeeded", _summary(child.log_path)
            else:
                status, summary = "failed", _summary(child.log_path) or f"exited {code}"
            child.outcome = {"status": status, "finished_at": iso(), "exit_code": code,
                             "summary": summary}

    def live(self) -> bool:
        """Whether a job this daemon started may still be running."""
        return any(child.outcome is None for child in self.children.values())

    def _record(self, child: Child) -> None:
        """Write what the ledger has not yet taken about one job: that it runs,
        or how it ended, after which the daemon lets go of it."""
        if child.outcome is None:
            if not child.started:
                self.runs.update(child.run_id, status="running", started_at=child.started_at,
                                 pid=child.process.pid)
                child.started = True
            return
        columns = dict(child.outcome, pid=None)
        if not child.started:
            columns["started_at"] = child.started_at
        self.runs.update(child.run_id, **columns)
        del self.children[child.run_id]
        if columns["status"] == "failed":
            self._maybe_retry(child.run_id)

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

    def interrupt(self, summary: str = "daemon stopped") -> None:
        """Ask every running job to stop, killing what is still running after
        the project's shutdown grace. `reap` records each as interrupted when it
        ends, so a caller that cannot wait goes on reaping instead."""
        now = time.monotonic()
        grace = self.config["engine"]["shutdown_grace_seconds"]
        for child in self.children.values():
            if child.stop_reason == "interrupted" or child.outcome is not None:
                continue
            child.stop_reason = "interrupted"
            child.stop_summary = summary
            child.stopping_at = now
            child.kill_at = now + grace
            _signal_group(child.process.pid, signal.SIGTERM)

    def shutdown(self, summary: str = "daemon stopped") -> None:
        """Stop every running job within the grace, killing what outlives it,
        and only then record how each ended, each record on its own. The store
        is not asked anything until every job has stopped, so a store that is
        gone cannot leave one running. An outcome the store does not take is
        logged with its run and exit code; the next start finds that run still
        open and records it interrupted."""
        self.interrupt(summary)
        deadline = max([child.kill_at or 0.0 for child in self.children.values()
                        if child.outcome is None], default=0.0)
        while self.live() and time.monotonic() < deadline:
            self.supervise()
            time.sleep(0.05)
        for child in self.children.values():
            if child.outcome is not None:
                continue
            _signal_group(child.process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                child.process.wait(timeout=2)
            child.log_handle.close()
            child.outcome = {"status": "interrupted", "finished_at": iso(),
                             "summary": summary}
        for child in list(self.children.values()):
            try:
                self._record(child)
            except Exception as exc:
                if self.children.pop(child.run_id, None) is not None:
                    self.log(f"run {child.run_id} ended with exit code "
                             f"{child.process.returncode} and its outcome is not "
                             f"recorded, so the next start records it interrupted: {_why(exc)}")

    def reload_declaration(self, fingerprint_path: Path) -> bool:
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
            config = self.load()
        except Exception as exc:
            self.reload_error = _why(exc)
            self.log(f"reload rejected, keeping the loaded configuration: {self.reload_error}")
            return False
        self.config = config
        self.by_id = automation_map(config)
        self.runs.environment = config["engine"]["environment"]
        self.reload_error = None
        fingerprint_path.write_text(daemon_record(config))
        self.log(f"reloaded, {len(self.by_id)} automations declared")
        return True

    def run(self) -> None:
        lock_path = self.state_dir / LOCK_FILE
        pid_path = self.state_dir / PID_FILE
        fingerprint_path = self.state_dir / DAEMON_FINGERPRINT_FILE
        lock = take_lock(self.state_dir)
        if lock is None:
            holder = read_pid(self.state_dir)
            raise RuntimeError(f"another automations daemon holds {lock_path}"
                               + (f" (pid {holder})" if pid_alive(holder) else ""))
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

    def prepare(self) -> None:
        """Reap what ended, take up the pause, and register the firings due
        unless paused: everything a tick does short of starting work."""
        self.reap()
        self.take_pause()
        if self.pause is None:
            self.schedule_due(utc_now())

    def tick(self) -> None:
        """Reap, schedule and dispatch once.

        The store is across a network, and a connection it drops is no reason
        to end the children this daemon is running: a tick that finds its
        connection gone says so, opens a new one if it can, and leaves the work
        to the next tick, whose dedupe keys make a repeated firing harmless."""
        try:
            self.prepare()
            if self.pause is None:
                self.dispatch()
        except Exception as exc:
            if not self.runs.lost():
                raise
            self.log(f"lost the store connection, reconnecting: {exc}")
            try:
                self.runs.reopen()
            except StoreUnavailable as again:
                self.log(f"the store is still unavailable: {again.message}")


# --- machine mode ----------------------------------------------------------------

class _StoreLost(Exception):
    """The machine's one store link went away under a project's work."""


class ProjectSlot:
    """A project on the opt-in list that the machine process holds: the
    project's own lock, its scheduler once its declaration loads, and why it is
    not served while it is not. Everything about the project reaches the slot
    through `host`, which the executable makes for it and which answers every
    question inside the project's own resolution."""

    def __init__(self, machine: "MachineService", entry: dict, host, lock):
        self.machine = machine
        self.entry = entry
        self.host = host
        self.lock = lock
        self.state_dir = Path(host.state_dir)
        self.daemon: Daemon | None = None
        self.recovered = False
        self.load_error: str | None = None
        self.tick_error: str | None = None
        self.refusal: tuple[str, str] | None = None
        self.next_tick = 0.0
        self.leaving: str | None = None

    @property
    def slug(self) -> str:
        return self.entry["slug"]

    @property
    def project_id(self) -> str:
        return self.entry["id"]

    def log(self, message: str) -> None:
        self.machine.project_log(self.slug, self.state_dir, message)

    def open(self) -> None:
        """Stand in the project's state root as its daemon, then load."""
        write_atomic(self.state_dir / PID_FILE, f"{os.getpid()}\n")
        self.log(f"the machine service, pid {os.getpid()}, holds this project's lock")
        self.load()

    def load(self) -> bool:
        """Build the project's scheduler from its declaration, or say why not.
        A declaration that does not load leaves the lock held and the project in
        `error` until a reload takes one."""
        try:
            daemon = Daemon(self.host.root, self.host.config_path, self.state_dir,
                            slug=self.slug, loader=self.host.load,
                            runs=RunLedger(self.project_id, "", link=self.machine.link),
                            launcher=self.host.launcher, log=self.log)
        except Exception as exc:
            reason = _why(exc)
            if reason != self.load_error:
                self.log(f"error: its declaration does not load, so nothing is registered "
                         f"or started for it until a reload takes one: {reason}")
            self.load_error = reason
            with contextlib.suppress(OSError):
                (self.state_dir / DAEMON_FINGERPRINT_FILE).unlink()
            return False
        self.daemon, self.load_error, self.recovered = daemon, None, False
        write_atomic(self.state_dir / DAEMON_FINGERPRINT_FILE, daemon_record(daemon.config))
        self.log(f"served: {len(daemon.by_id)} automations declared, environment "
                 f"{daemon.config['engine']['environment']}")
        return True

    def tick_seconds(self) -> float:
        return self.daemon.config["engine"]["tick_seconds"] if self.daemon else ADMIT_SECONDS

    def check(self) -> bool:
        """The checks before every action for the project, asked again: that it
        still enables automations for itself and still stands where it joined."""
        refusal = self.host.check()
        if refusal != self.refusal:
            if refusal is not None:
                self.log(f"{refusal[0]}: {refusal[1]}; registering and starting nothing "
                         "for it until that changes, and running jobs finish")
            elif self.refusal is not None:
                self.log("may be served again")
        self.refusal = refusal
        return refusal is None

    def prepare(self) -> None:
        """The project's part of a pass short of starting work: reap what ended
        and, while it may be served and is not paused, register what is due."""
        daemon = self.daemon
        if daemon is None:
            return
        if not self.recovered:
            daemon.recover()
            self.recovered = True
        daemon.reap()
        if self.leaving or not self.check():
            return
        daemon.take_pause()
        if daemon.pause is None:
            daemon.schedule_due(utc_now())
        if self.tick_error is not None:
            self.log("served again")
            self.tick_error = None

    def can_start(self) -> bool:
        return (self.daemon is not None and self.recovered and not self.leaving
                and self.refusal is None and self.tick_error is None
                and self.daemon.pause is None)

    def fail(self, exc: BaseException) -> None:
        reason = _why(exc)
        if reason != self.tick_error:
            self.log(f"error: {reason}; the other projects are served")
        self.tick_error = reason

    def state(self) -> tuple[str, str | None]:
        if self.load_error is not None:
            return "error", self.load_error
        if self.tick_error is not None:
            return "error", self.tick_error
        if self.refusal is not None:
            return self.refusal
        if self.daemon is not None and self.daemon.pause is not None:
            return "paused", pause_words(self.daemon.pause)
        return "served", None

    def reload(self) -> None:
        if self.daemon is None:
            self.load()
        else:
            self.daemon.reload_declaration(self.state_dir / DAEMON_FINGERPRINT_FILE)

    def release(self, said: str) -> None:
        """Let go of the project: its pid and fingerprint, then its lock."""
        if read_pid(self.state_dir) == os.getpid():
            for name in (PID_FILE, DAEMON_FINGERPRINT_FILE):
                with contextlib.suppress(OSError):
                    (self.state_dir / name).unlink()
        self.lock.close()
        self.log(said)

    def row(self) -> dict:
        state, reason = self.state()
        row = {"state": state, "reason": reason, "state_dir": str(self.state_dir)}
        if self.daemon is not None:
            row.update({
                "environment": self.daemon.config["engine"]["environment"],
                "fingerprint": config_fingerprint(self.daemon.config),
                "reload_error": self.daemon.reload_error,
                "automations": len(self.daemon.by_id),
                "running": len(self.daemon.children),
                "pause": self.daemon.pause,
            })
        return row


class MachineService:
    """The scheduler once per machine, for every project on its opt-in list.

    It decides when work starts and nothing about what the work is. It holds
    what is the machine's: one store link every project's ledger is a view
    onto, an optional cap on jobs running at once across every project, and
    the order projects are dealt starts in under that cap. Everything it knows
    about the machine and its projects arrives through `host`, the executable's
    answer for the list, the machine settings, and each project. It loads no
    project's environment: every job is started through the project's launcher.

    It follows the list without a reload: a project that joins is served from
    the first pass at which it can be, and one that leaves has its running jobs
    interrupted and its lock let go once they have ended. A project that cannot
    be served is reported with the reason and asked again every
    `ADMIT_SECONDS`; one whose declaration does not load holds its lock and is
    `error` until a reload takes one; the others are served throughout."""

    def __init__(self, host):
        self.host = host
        self.state_dir = Path(host.state_dir)
        self.link = StoreLink()
        # Every project on the list by its id: what the list says of it, why it
        # is not served while it is not, and its slot once it is.
        self.entries: dict[str, dict] = {}
        self.draining: list[ProjectSlot] = []
        self.list_witness: Any = ()
        self.list_error: str | None = None
        self.settings: dict = {}
        self.settings_error: str | None = None
        self.store_error: str | None = None
        self.reloads = 0
        self.turn: str | None = None
        self.binding = False
        self.started_at = iso()
        self.stop_requested = False
        self.reload_requested = False
        self._lock = None
        self._log_path = self.state_dir / LOG_FILE
        self._log_to_stderr = True
        self._published: str | None = None
        self._published_at = 0.0

    # --- what it says ----------------------------------------------------------

    def _write(self, line: str) -> None:
        append_line(self._log_path, line)
        if self._log_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def log(self, message: str) -> None:
        """A line about the process, its store or its cap, tagged `[machine]`."""
        self._write(log_line(MACHINE_TAG, message))

    def project_log(self, slug: str, state_dir: Path, message: str) -> None:
        """A line about one project: in the machine log, and in the project's
        own service log where its state root exists, so `service logs` in the
        project keeps answering."""
        line = log_line(slug, message)
        if Path(state_dir).is_dir():
            append_line(Path(state_dir) / LOG_FILE, line)
        self._write(line)

    def _stderr_is_log(self) -> bool:
        try:
            mine, log = os.fstat(sys.stderr.fileno()), os.stat(self._log_path)
        except (OSError, ValueError, AttributeError):
            return False
        return (mine.st_dev, mine.st_ino) == (log.st_dev, log.st_ino)

    def slots(self) -> list[ProjectSlot]:
        return [entry["slot"] for _, entry in sorted(self.entries.items())
                if entry.get("slot") is not None]

    def running(self) -> int:
        return sum(len(slot.daemon.children) for slot in (*self.slots(), *self.draining)
                   if slot.daemon is not None)

    def cap(self) -> int | None:
        return self.settings.get("max_parallel")

    def status(self) -> dict:
        cap = self.cap()
        return {
            "mode": "machine",
            "pid": os.getpid(),
            "started_at": self.started_at,
            "fingerprint": self.settings.get("fingerprint"),
            "reload_error": self.settings_error,
            "reloads": self.reloads,
            "publish_seconds": PUBLISH_SECONDS,
            "projects_file": {"path": str(self.host.projects_file),
                              "joined": len(self.entries), "error": self.list_error},
            "cap": {"max_parallel": cap, "running": self.running(),
                    "binding": self.binding},
            "store": {"connected": not self.link.lost(), "error": self.store_error},
            "projects": [self._entry_row(entry) for entry in
                         sorted(self.entries.values(), key=lambda e: (e["slug"], e["id"]))],
            "stopping": self.stop_requested,
        }

    def _entry_row(self, entry: dict) -> dict:
        row = {"project": entry["slug"], "project_id": entry["id"], "root": entry["root"],
               "joined_at": entry.get("joined_at"), "joined_by": entry.get("joined_by")}
        slot = entry.get("slot")
        if slot is None:
            row.update(state=entry["state"], reason=entry["reason"])
        else:
            row.update(slot.row())
        return row

    def publish(self) -> None:
        """Write the machine status when anything in it moved, and at least
        every `PUBLISH_SECONDS`, so its age tells a live process from a hung one."""
        text = json.dumps(self.status(), indent=2, default=str)
        now = time.monotonic()
        if text == self._published and now - self._published_at < PUBLISH_SECONDS:
            return
        found = json.loads(text)
        found["published_at"] = iso()
        with contextlib.suppress(OSError):
            write_atomic(self.state_dir / STATUS_FILE, json.dumps(found, indent=2) + "\n")
        self._published, self._published_at = text, now

    def _settings_words(self) -> str:
        cap = self.cap()
        return (f"machine cap {cap} job(s) at once" if cap is not None
                else "no machine cap, each project's own limits apply")

    # --- lifecycle -------------------------------------------------------------

    def run(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_to_stderr = not self._stderr_is_log()
        self._lock = take_lock(self.state_dir)
        if self._lock is None:
            holder = read_pid(self.state_dir)
            raise RuntimeError(f"another machine automations service holds "
                               f"{self.state_dir / LOCK_FILE}"
                               + (f" (pid {holder})" if pid_alive(holder) else ""))
        try:
            self.settings = self.host.load_settings()
            self.link.open()
        except BaseException:
            self._lock.close()
            raise
        for warning in self.link.warnings:
            self.log(warning)
        write_atomic(self.state_dir / PID_FILE, f"{os.getpid()}\n")
        write_atomic(self.state_dir / DAEMON_FINGERPRINT_FILE, self.settings["fingerprint"] + "\n")

        def stop(_signum: int, _frame: Any) -> None:
            self.stop_requested = True

        def reload(_signum: int, _frame: Any) -> None:
            self.reload_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, reload)
        self.log(f"started, pid {os.getpid()}, serving the projects on "
                 f"{self.host.projects_file}; {self._settings_words()}")
        try:
            while not self.stop_requested:
                if self.reload_requested:
                    self.reload_requested = False
                    self.reload()
                self.step()
                time.sleep(self.sleep_seconds())
        finally:
            self.shutdown()
            self.link.close()
            for name in (PID_FILE, DAEMON_FINGERPRINT_FILE, STATUS_FILE):
                with contextlib.suppress(OSError):
                    (self.state_dir / name).unlink()
            self.log("stopped")
            self._lock.close()
            self._lock = None

    def sleep_seconds(self) -> float:
        now = time.monotonic()
        waits = [slot.next_tick - now for slot in (*self.slots(), *self.draining)]
        return max(0.02, min([LONGEST_SLEEP_SECONDS, *waits]))

    def reload(self) -> None:
        """SIGHUP: the machine settings, then every project's declaration on its
        own. Settings that do not load leave the ones in force, said in the log
        and the status; a declaration that does not load leaves its project's."""
        try:
            settings = self.host.load_settings()
        except (Exception, SystemExit) as exc:
            self.settings_error = _why(exc)
            self.log(f"reload rejected, keeping the machine settings loaded: "
                     f"{self.settings_error}")
        else:
            self.settings, self.settings_error = settings, None
            write_atomic(self.state_dir / DAEMON_FINGERPRINT_FILE, settings["fingerprint"] + "\n")
            self.log(f"reloaded the machine settings: {self._settings_words()}")
        for slot in self.slots():
            try:
                slot.reload()
            except Exception as exc:
                slot.fail(exc)
        self.reloads += 1

    def step(self) -> None:
        """One pass: follow the list, admit what can be served, and give every
        project whose tick is due its tick, starting work under the cap."""
        self._take_list()
        now = time.monotonic()
        for entry in list(self.entries.values()):
            if entry.get("slot") is None and now >= entry["next_try"] \
                    and not self.stop_requested:
                entry["next_try"] = now + ADMIT_SECONDS
                self._admit(entry)
        if self.link.lost():
            self._reconnect("the store connection is closed")
        if self.link.lost():
            # Without the store nothing is registered, started or recorded,
            # but every running job is still timed out and stopped.
            for slot in (*self.draining, *self.slots()):
                if slot.daemon is not None:
                    slot.daemon.supervise()
            self.publish()
            return
        try:
            due = []
            for slot in (*self.draining, *self.slots()):
                if now < slot.next_tick:
                    continue
                slot.next_tick = now + slot.tick_seconds()
                if self._guard(slot, slot.prepare) is not False:
                    due.append(slot)
            self.deal([slot for slot in due if slot.can_start()])
        except _StoreLost as lost:
            self._reconnect(lost)
        self._release_drained()
        self.publish()

    def _guard(self, slot: ProjectSlot, action):
        """Run one project's action; a fault of the project's stays the
        project's, and a lost store link is the machine's."""
        try:
            return action()
        except Exception as exc:
            if self.link.lost():
                raise _StoreLost(exc) from exc
            slot.fail(exc)
            return False

    def deal(self, candidates: list[ProjectSlot]) -> None:
        """Start work. Without a cap each project starts what its own limits
        allow. Under one, projects with work are dealt one start each in turn,
        from the project after the last one dealt, until the cap is reached or
        none takes another, so with a cap of one starts go round the projects."""
        cap = self.cap()
        if cap is None:
            for slot in candidates:
                self._guard(slot, slot.daemon.dispatch)
            self.binding = False
            return
        order = sorted(candidates, key=lambda slot: slot.project_id)
        if self.turn is not None:
            after = [slot for slot in order if slot.project_id > self.turn]
            order = after + [slot for slot in order if slot.project_id <= self.turn]
        moved = True
        while moved and self.running() < cap:
            moved = False
            for slot in order:
                if self.running() >= cap:
                    break
                if slot.tick_error is None and self._guard(
                        slot, lambda slot=slot: slot.daemon.dispatch(limit=1)):
                    moved = True
                    self.turn = slot.project_id
        self.binding = self.running() >= cap

    def _reconnect(self, why) -> None:
        if self.store_error is None:
            self.log(f"lost the store connection, reconnecting every pass: {why}")
        try:
            self.link.reopen()
        except StoreUnavailable as again:
            if again.message != self.store_error:
                self.log(f"the store is unavailable: {again.slug}: {again.message}")
            self.store_error = again.message
            return
        self.log("the store answers again")
        self.store_error = None

    # --- the list --------------------------------------------------------------

    def _take_list(self) -> None:
        witness = self.host.list_witness()
        if witness == self.list_witness:
            return
        self.list_witness = witness
        try:
            listed = self.host.read_list()
        except (Exception, SystemExit) as exc:
            why = _why(exc)
            if why != self.list_error:
                self.log(f"error: the opt-in list does not load, so the projects already "
                         f"served stay served and no project joins: {why}")
            self.list_error = why
            return
        if self.list_error is not None:
            self.log("the opt-in list loads again")
        self.list_error = None
        for project in list(self.entries):
            entry = self.entries[project]
            found = listed.get(project)
            if found is None or found["root"] != entry["root"] or found["slug"] != entry["slug"]:
                self._leave(project)
        for project, found in listed.items():
            if project in self.entries:
                self.entries[project].update(joined_at=found.get("joined_at"),
                                             joined_by=found.get("joined_by"))
                continue
            entry = {**found, "id": project, "state": "refused", "reason": "not yet checked",
                     "slot": None, "next_try": 0.0, "said": None}
            self.entries[project] = entry
            self.project_log(entry["slug"], self.host.project_state_dir(entry["slug"]),
                             f"on the machine service's opt-in list, joined "
                             f"{found.get('joined_at')} by {found.get('joined_by')}; "
                             f"project {project} at {found['root']}")

    def _leave(self, project: str) -> None:
        """A project no longer on the list: its running jobs are interrupted as
        a stop of its own daemon would, and its lock is let go once they end."""
        entry = self.entries.pop(project)
        slot = entry.get("slot")
        if slot is None:
            self.project_log(entry["slug"], self.host.project_state_dir(entry["slug"]),
                             "left the machine service's opt-in list")
            return
        slot.leaving = "left the machine service's opt-in list"
        running = len(slot.daemon.children) if slot.daemon is not None else 0
        if running:
            slot.daemon.interrupt("the project left the machine service")
        slot.log(f"{slot.leaving}"
                 + (f"; interrupting its {running} running job(s) before letting go "
                    "of its lock" if running else ""))
        self.draining.append(slot)

    def _release_drained(self) -> None:
        for slot in list(self.draining):
            if slot.daemon is None or not slot.daemon.children:
                self.draining.remove(slot)
                slot.release(f"{slot.leaving}; let go of its lock")

    def _admit(self, entry: dict) -> None:
        """Serve a project on the list if it can be: the checks a project takes
        before every action, asked of the host; then the project's lock; then
        its declaration. What stops it is said once, until it changes. A state
        root the project cannot be served from is the project's `error`, and
        asked again like any other refusal."""
        host, refusal = self.host.admit(entry)
        lock = None
        if refusal is None:
            state_dir = Path(host.state_dir)
            try:
                lock = take_lock(state_dir)
            except OSError as exc:
                refusal = ("error", f"its state root {state_dir} cannot be used: {_why(exc)}")
            if refusal is None and lock is None:
                holder = read_pid(state_dir)
                if holder == os.getpid():
                    refusal = ("refused", "the machine service is still letting go of it")
                elif pid_alive(holder):
                    refusal = ("refused", f"a project-mode daemon, pid {holder}, serves "
                                          "this project; stop it there")
                else:
                    refusal = ("refused", "another process holds this project's lock "
                                          f"{state_dir / LOCK_FILE}")
        if refusal is None:
            slot = ProjectSlot(self, entry, host, lock)
            try:
                slot.open()
            except OSError as exc:
                if read_pid(slot.state_dir) == os.getpid():
                    with contextlib.suppress(OSError):
                        (slot.state_dir / PID_FILE).unlink()
                lock.close()
                refusal = ("error", f"its state root {slot.state_dir} cannot be used: "
                                    f"{_why(exc)}")
            else:
                entry["slot"], entry["said"] = slot, None
                return
        entry["state"], entry["reason"] = refusal
        if entry.get("said") != refusal:
            entry["said"] = refusal
            self.project_log(entry["slug"], self.host.project_state_dir(entry["slug"]),
                             f"{refusal[0]}: {refusal[1]}; asking again every "
                             f"{ADMIT_SECONDS:g}s and starting nothing for it meanwhile")

    # --- stopping --------------------------------------------------------------

    def shutdown(self) -> None:
        """Interrupt every project's running jobs at once, give them their
        project's grace, record them, and let go of every project."""
        slots = [*self.slots(), *self.draining]
        for slot in slots:
            if slot.daemon is not None:
                slot.daemon.interrupt()
        deadline = max([child.kill_at or 0.0 for slot in slots if slot.daemon is not None
                        for child in slot.daemon.children.values()], default=0.0) + 2.0
        while time.monotonic() < deadline and any(
                slot.daemon.live() for slot in slots if slot.daemon is not None):
            for slot in slots:
                if slot.daemon is not None:
                    slot.daemon.supervise()
            time.sleep(0.05)
        for slot in slots:
            if slot.daemon is not None:
                with contextlib.suppress(Exception):
                    slot.daemon.shutdown()
        for entry in self.entries.values():
            slot = entry.get("slot")
            if slot is not None:
                slot.release("the machine service stopped; let go of its lock")
                entry["slot"] = None
        for slot in self.draining:
            slot.release(f"{slot.leaving}; let go of its lock")
        self.draining = []


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
    _root = os.environ.get("AUTOMATIONS_PROJECT_ROOT")
    _tag = _project_slug(Path(_root)) if _root else "unknown"
    try:
        run_from_env()
    except StoreUnavailable as exc:
        sys.stderr.write(log_line(_tag, f"{exc.slug}: {exc.message}"
                                  + (f"; {exc.hint}" if exc.hint else "")))
        raise SystemExit(1)
    except Exception as exc:
        sys.stderr.write(log_line(_tag, str(exc)))
        raise SystemExit(1)
