from __future__ import annotations

import contextlib
import hashlib
import json
import math
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
# The capability whose placement rows say where this project's service runs,
# and how often a running daemon reads them again.
PLACEMENT_CAPABILITY = "automations"
PLACEMENT_REFRESH_SECONDS = 30.0
AGENT_ENGINES = ("claude", "codex")
AGENT_MODES = ("read", "write", "act")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
AGENT_KEYS = frozenset(
    {"engine", "model", "effort", "mode", "timeout_seconds", "service_tier"}
)
# The keys an `[[automations]]` entry of config.toml could carry before the
# definitions moved into the store. Only `import` reads such an entry now, and
# it refuses one carrying any other key, as the loader did: a key the runtime
# never read was a declaration silently doing nothing.
LEGACY_AUTOMATION_KEYS = frozenset(
    {"id", "name", "description", "script", "enabled", "schedule",
     "every_seconds", "timeout_seconds", "max_parallel", "max_pending",
     "overlap", "retries", "arguments", "environments"}
)

# What one definition holds, each its own column of automations_definitions.
# `max_parallel`, `max_pending` and `overlap` are not among them: no project
# ever set them to anything but 1, 1 and skip, so those are fixed here, and
# one run of an automation at a time holds across every machine on the store.
DEFINITION_FIELDS = ("description", "script_path", "script_body", "arguments",
                     "schedule", "timezone", "node", "enabled", "timeout_seconds",
                     "retries")
DEFINITION_DEFAULTS = {"description": "", "script_path": None, "script_body": None,
                       "arguments": [], "schedule": None, "timezone": "UTC",
                       "node": None, "enabled": True, "timeout_seconds": 300,
                       "retries": 0}

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
    """The part of the declaration the project's file holds: `[engine]` and
    `[agents]`. The automations themselves are the store's, read by
    `apply_definitions`."""
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
    # The automations are the definition rows read from the store, so a change
    # to one, from any machine, moves the fingerprint of every daemon that
    # reads the project.
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
        "node": NODE_NAME(),
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


def read_daemon_node(state_dir: Path) -> str | None:
    """The node the running daemon schedules for, or None if it published none:
    the node a supervisor's daemon was handed is not visible from a terminal
    opened later."""
    value = read_daemon_record(state_dir).get("node")
    return value if isinstance(value, str) and value else None


def _project_identity(root: Path) -> dict:
    """What an older project.json still holds, or nothing: a project is bound
    by its id, which no file of the envelope carries."""
    try:
        identity = json.loads((root / "capabilities" / "project.json").read_text())
    except (OSError, ValueError):
        return {}
    return identity if isinstance(identity, dict) else {}


# The id a registration stamps is the one the launching CLI resolves for a
# write: in process the CLI installs its resolver here, called with `strict`
# False on a read path so a project whose id may not be stamped reads instead of
# refusing, and the daemon it launches is handed the answer as
# CAPABILITIES_PROJECT_ID. The project root's own binding - the id its
# .env.local, else its .env, sets - stands in only where no launcher resolved one.
PROJECT_ID_FOR_WRITE = None

# The node a run is recorded on is the one the contract names: in process the
# CLI installs its resolver here, and the daemon it launches is handed the
# answer as AUTOMATIONS_NODE_NAME. Where neither reached it, the contract is
# asked directly: the node id the machine resolves for itself.
def NODE_NAME() -> str:
    given = os.environ.get("AUTOMATIONS_NODE_NAME", "").strip()
    if given:
        return given
    from capabilities_contract.capability import store
    return store.node_id()


def _handed_project_id(root: Path) -> str:
    """The id handed down for this root, scoped by CAPABILITIES_PROJECT_ID_ROOT;
    one without that scope binds no folder."""
    handed = os.environ.get("CAPABILITIES_PROJECT_ID", "").strip()
    scope = os.environ.get("CAPABILITIES_PROJECT_ID_ROOT", "").strip()
    if not handed or not scope:
        return ""
    try:
        return handed if Path(scope).resolve() == Path(root).resolve() else ""
    except OSError:
        return ""


def _env_file_project_id(root: Path) -> str | None:
    """The CAPABILITIES_PROJECT_ID the project root's .env.local, else its
    .env, sets."""
    for name in (".env.local", ".env"):
        try:
            lines = (Path(root) / name).read_text().splitlines()
        except OSError:
            continue
        found = ""  # the last line naming the key wins, as in any env file
        for line in lines:
            key, eq, value = line.strip().partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if eq and key == "CAPABILITIES_PROJECT_ID":
                found = value.strip().strip('"').strip("'")
        if found:
            return found
    return None


def _registration_id(root: Path, identity: dict, strict: bool = True) -> str | None:
    if callable(PROJECT_ID_FOR_WRITE):
        return PROJECT_ID_FOR_WRITE(strict)
    return _handed_project_id(root) or _env_file_project_id(root)


class DefinitionsNotImported(ConfigError):
    """config.toml still declares `[[automations]]`, which nothing reads since
    the definitions moved into the store: every command refuses until
    `automations import --apply` has moved them, so an automation never stops
    running without anyone having been told."""


def load_config(root: Path, config_path: Path,
                unimported: bool = False) -> dict[str, Any]:
    """The config as the file declares it. `unimported` reads past
    [[automations]] still in it, for what reads `[agents]` alone: a running job
    asking for an agent turn mid-migration is not the one to stop."""
    try:
        raw = tomllib.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    return normalise_config(root, raw, unimported)


def normalise_config(root: Path, raw: dict[str, Any],
                     unimported: bool = False) -> dict[str, Any]:
    """Validation and defaulting of the file's part of the declaration. Its
    `automations` start empty: `apply_definitions` fills them from the store."""
    if raw.get("version") != 1:
        raise ConfigError("config version must be 1")
    if raw.get("automations") and not unimported:
        raise DefinitionsNotImported(
            "config.toml still declares [[automations]], and automations are read "
            "from the store only; `automations import` shows what moves, and "
            "`automations import --apply` moves them and removes them from the file")
    engine = raw.get("engine") or {}
    if not isinstance(engine, dict):
        raise ConfigError("engine must be a table")
    normalized_engine = {
        "tick_seconds": _number(engine.get("tick_seconds", 1.0), "engine.tick_seconds", 0.1),
        "max_parallel": _int(engine.get("max_parallel", 4), "engine.max_parallel", 1),
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
    return {"version": 1, "engine": normalized_engine,
            "automations": [], "agents": load_agents(raw)}


def _script_path(root: Path, script: Any, label: str) -> Path:
    """A script path as a definition declares it, resolved under the project
    root and refused when it leaves it."""
    if not isinstance(script, str) or not script.strip():
        raise ConfigError(f"{label}.script_path must be a non-empty relative path")
    path = Path(script)
    if path.is_absolute():
        raise ConfigError(f"{label}.script_path must be relative to the project root")
    if ".." in path.parts:
        raise ConfigError(f"{label}.script_path must not climb out with ..")
    allowed_root = root.resolve()
    resolved = (allowed_root / path).resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise ConfigError(f"{label}.script_path escapes the project root") from exc
    return resolved


def check_definition(root: Path, slug: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """One definition's fields, validated and defaulted exactly as the store
    and the daemon take them; raises ConfigError naming the first fault. The
    same check runs on every write and on every row read, so a row another
    writer put in the store is judged as one this CLI wrote."""
    if not isinstance(slug, str) or not ID_RE.fullmatch(slug):
        raise ConfigError(f"automation slug {slug!r} must match {ID_RE.pattern}")
    label = f"automation {slug!r}"
    unknown = sorted(set(fields) - set(DEFINITION_FIELDS))
    if unknown:
        raise ConfigError(f"{label} has unknown field(s): {', '.join(unknown)}")
    out = {**DEFINITION_DEFAULTS, **fields}
    if out["description"] is None:
        out["description"] = ""
    if not isinstance(out["description"], str):
        raise ConfigError(f"{label}.description must be text")
    if (out["script_path"] is None) == (out["script_body"] is None):
        raise ConfigError(f"{label} must have exactly one of script_path and script_body")
    if out["script_path"] is not None:
        _script_path(root, out["script_path"], label)
    elif not isinstance(out["script_body"], str) or not out["script_body"].strip():
        raise ConfigError(f"{label}.script_body must be a non-empty script")
    elif not out["script_body"].startswith("#!"):
        raise ConfigError(f"{label}.script_body must start with a #! line naming "
                          "what runs it")
    arguments = out["arguments"]
    if not isinstance(arguments, (list, tuple)) or not all(isinstance(v, str) for v in arguments):
        raise ConfigError(f"{label}.arguments must be an array of strings")
    out["arguments"] = list(arguments)
    if out["schedule"] is not None:
        if not isinstance(out["schedule"], str):
            raise ConfigError(f"{label}.schedule must be a five-field cron expression")
        parse_cron(out["schedule"])
    if not isinstance(out["timezone"], str):
        raise ConfigError(f"{label}.timezone must name a time zone")
    try:
        ZoneInfo(out["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{label}.timezone: unknown time zone {out['timezone']!r}") from exc
    if out["node"] is not None and (not isinstance(out["node"], str) or not out["node"].strip()
                                    or out["node"] != out["node"].strip()):
        raise ConfigError(f"{label}.node must name a node, or be null for none")
    if not isinstance(out["enabled"], bool):
        raise ConfigError(f"{label}.enabled must be true or false")
    out["timeout_seconds"] = _int(out["timeout_seconds"], f"{label}.timeout_seconds", 1)
    out["retries"] = _int(out["retries"], f"{label}.retries", 0)
    return out


def definition_item(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    """The automation one stored definition declares, as the scheduler acts on
    it. Raises ConfigError for a row that does not validate."""
    fields = check_definition(root, row["slug"],
                              {name: row[name] for name in DEFINITION_FIELDS})
    script = fields["script_path"]
    return {
        "id": row["slug"],
        "version": str(row["id"]),
        "description": fields["description"] or None,
        "script": script,
        "script_path": _script_path(root, script, row["slug"]) if script else None,
        "script_body": fields["script_body"],
        "arguments": fields["arguments"],
        "schedule": fields["schedule"],
        "timezone": fields["timezone"],
        "node": fields["node"],
        "enabled": fields["enabled"],
        "timeout_seconds": float(fields["timeout_seconds"]),
        "retries": fields["retries"],
        "created_at": iso(row["created_at"]) if isinstance(row["created_at"], datetime)
        else row["created_at"],
        "created_by": row["created_by"],
    }


def apply_definitions(root: Path, config: dict[str, Any],
                      rows: Sequence[dict[str, Any]],
                      placed: Sequence[str] | None = None) -> list[str]:
    """Fill `config["automations"]` from the project's active definitions and
    say which rows were left out. A row that does not validate is skipped and
    named, never refusing the rest: one bad definition must not stop every
    other automation of the project. `placed` is where the project's
    automations service is placed, kept as `config["placed_on"]` beside the
    declaration and outside its fingerprint: it is the manager's fact, not a
    person's declaration here."""
    config["placed_on"] = list(placed) if placed is not None else None
    items: list[dict[str, Any]] = []
    problems: list[str] = []
    for row in sorted(rows, key=lambda r: r["slug"]):
        try:
            items.append(definition_item(root, row))
        except ConfigError as exc:
            problems.append(f"definition {row['id']} of {row['slug']!r} is skipped: {exc}")
    config["automations"] = items
    config["definition_problems"] = problems
    return problems


def automation_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in config["automations"]}


def follows_service(item: dict[str, Any], placed: Sequence[str] | None) -> bool:
    """Whether the automation runs where the project's automations service is
    placed rather than on a node of its own: it names no node, or names one the
    service is not placed on, which no daemon of the project runs on once the
    service has left it. `placed` is the nodes the service is placed on, as the
    store records them (`RunLedger.placed_nodes`); while that is unknown or
    empty, a node the automation names is held to."""
    return item["node"] is None or (bool(placed) and item["node"] not in placed)


def scheduled_here(item: dict[str, Any], node: str,
                   placed: Sequence[str] | None = None) -> bool:
    """Whether the node schedules this automation: it is enabled, and names the
    node, or follows the service and the service is placed on the node - or on
    no node the store records, when every daemon of the project schedules it.
    Two daemons firing the same minute take one run between them, by its
    dedupe key."""
    if not item["enabled"]:
        return False
    if not follows_service(item, placed):
        return item["node"] == node
    return not placed or node in placed


def runs_here(item: dict[str, Any], node: str,
              placed: Sequence[str] | None = None) -> bool:
    """Whether a run queued for the node may start there: the automation is
    enabled and is the node's, or follows the service, whose runs go to the
    node they were queued for."""
    return item["enabled"] and (follows_service(item, placed) or item["node"] == node)


def manual_run_node(item: dict[str, Any], here: str,
                    placed: Sequence[str] | None = None) -> str:
    """The node a manual run of the automation is queued for: the node it names
    while that holds, else this machine where the service is placed here or
    nowhere the store records, else the first node the service is placed on."""
    if not follows_service(item, placed):
        return item["node"]
    if not placed or here in placed:
        return here
    return sorted(placed)[0]


def every_seconds_as_cron(seconds: int) -> str:
    """An `every_seconds` interval as the five-field cron that fires at the same
    rate, for `import`; raises ConfigError for one no cron expresses."""
    if seconds % 60 == 0 and seconds < 3600:
        minutes = seconds // 60
        return "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
    if seconds % 3600 == 0 and seconds < 86400:
        hours = seconds // 3600
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    if seconds == 86400:
        return "0 0 * * *"
    raise ConfigError(f"every_seconds = {seconds} has no five-field cron equivalent: "
                      "a whole number of minutes under an hour, of hours under a day, "
                      "or one day converts")


def legacy_definitions(root: Path, raw: dict[str, Any], here_environment: str,
                       here_node: str, nodes: dict[str, str | None]) -> tuple[list, list]:
    """The `[[automations]]` entries of a config.toml as store definitions, for
    `import`: (definitions, refusals). Each definition is
    {"slug", "fields", "from"}; a refusal names the entry and why it cannot be
    imported. `environments` becomes the one node: the environment in force here
    is this node, `nodes` maps the others, and an enabled automation whose
    environments reach no node, or reach two, is refused rather than imported
    to stop."""
    entries = raw.get("automations") or []
    if not isinstance(entries, list):
        return [], ["automations must be an array of tables"]
    timezone_name = str((raw.get("engine") or {}).get("timezone") or "UTC")
    mapping = {here_environment: here_node, **nodes}
    out: list[dict[str, Any]] = []
    refused: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        label = f"automations[{index}]"
        try:
            if not isinstance(item, dict):
                raise ConfigError("must be a table")
            label = f"automation {item.get('id')!r}"
            unknown = sorted(set(item) - LEGACY_AUTOMATION_KEYS)
            if unknown:
                raise ConfigError(f"has unknown key(s): {', '.join(unknown)}")
            slug = item.get("id")
            if slug in seen:
                raise ConfigError("is declared twice")
            seen.add(slug)
            schedule = item.get("schedule")
            every = item.get("every_seconds")
            if schedule is not None and every is not None:
                raise ConfigError("declares schedule and every_seconds")
            if every is not None:
                schedule = every_seconds_as_cron(_int(every, "every_seconds", 1))
            name = item.get("name")
            description = item.get("description")
            if name is not None and not isinstance(name, str):
                raise ConfigError("name must be a string")
            text = " - ".join(part for part in (name, description) if part)
            enabled = item.get("enabled", True)
            environments = item.get("environments") or []
            if not isinstance(environments, list) or not all(
                    isinstance(v, str) and v for v in environments):
                raise ConfigError("environments must be an array of strings")
            reached = ({here_node} if not environments else
                       {mapping[env] for env in environments if mapping.get(env)})
            if len(reached) > 1:
                raise ConfigError(f"its environments ({', '.join(environments)}) reach "
                                  f"the nodes {', '.join(sorted(reached))}, and an "
                                  "automation is scheduled on one node")
            if not reached and enabled:
                raise ConfigError(f"none of its environments ({', '.join(environments)}) "
                                  "names a node: the one in force here is "
                                  f"{here_environment!r}, node {here_node!r}; pass "
                                  "--node <environment>=<node> for the one it runs under")
            timeout = _number(item.get("timeout_seconds", 300), "timeout_seconds", 1)
            fields = {
                "description": text,
                "script_path": item.get("script"),
                "arguments": item.get("arguments") or [],
                "schedule": schedule,
                "timezone": timezone_name,
                "node": next(iter(reached)) if reached else None,
                "enabled": enabled,
                "timeout_seconds": int(math.ceil(timeout)),
                "retries": item.get("retries", 0),
            }
            out.append({"slug": slug, "fields": check_definition(root, slug, fields),
                        "from": {key: item[key] for key in item}})
        except ConfigError as exc:
            refused.append(str(exc) if str(exc).startswith(label) else f"{label}: {exc}")
    return out, refused


# --- the run ledger -------------------------------------------------------------

# The ledger is what every daemon of a project coordinates through, so it lives
# in the project's store, reached through the shared database library, in this
# capability's own table and migration ledger (DOCTRINE rule 22). The owner the
# library records the steps under is the capability's name, and every object a
# step creates is named after it.
#
# `dedupe_key` is automation and scheduled time. It is the whole of the mutual
# exclusion between machines running one project: whichever inserts a firing
# first owns it and the other is told. It is unique within its project rather
# than across the table, because one database serves every project on every
# machine, and two projects living in directories of the same name would
# otherwise take each other's firings. (Rows written before the definitions
# moved into the store carry namespace and environment in front of it.)
#
# `node` names the node a run belongs to: the one that schedules it, or for a
# manual run the definition's node, else the machine that asked. Recovery,
# claims and the node's running count read only the node's own runs, so two
# machines on one store never take each other's. `host` names the physical
# machine that started it, because a pid and the log a run points at mean
# something on that machine alone.
#
# The definitions are versions (`0003-definitions`): a body kept in the store
# has no git history, so a run names the exact version it ran as
# `definition_id`, and the version names who saved it.
#
# A saved or removed definition raises the project's version of the
# `automations_changed` channel in the store's change versions, in the same
# transaction (`0005-definitions-versions`), and a scheduler follows that
# channel by polling. The trigger still sends its `pg_notify` beside the raise,
# for a scheduler of an earlier release still listening on another machine.
#
# A run may record a result on its own row (`0006-runs-result`), and the next
# run of the automation is handed the last one recorded, whichever node ran
# it, so state between runs lives in the store rather than on one machine.
LEDGER_OWNER = "automations"
LEDGER_MAJOR = 1
LEDGER_MINOR = 3
# The most a run may record: it is handed to the next run as one environment
# variable, which the kernel caps at 128 KiB.
RESULT_MAX_BYTES = 65536
CHANGED_CHANNEL = "automations_changed"
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
    ("0003-definitions", r"""
    CREATE TABLE automations_definitions (
        id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id      text        NOT NULL CHECK (project_id <> ''),
        slug            text        NOT NULL CHECK (slug ~ '^[a-z][a-z0-9_-]*$'),
        description     text        NOT NULL DEFAULT '',
        script_path     text        CHECK (script_path <> '' AND script_path !~ '^/'
                                           AND script_path !~ '(^|/)\.\.(/|$)'),
        script_body     text        CHECK (script_body <> ''),
        arguments       text[]      NOT NULL DEFAULT '{}',
        schedule        text,
        timezone        text        NOT NULL DEFAULT 'UTC',
        node            text        CHECK (node <> ''),
        enabled         boolean     NOT NULL DEFAULT true,
        timeout_seconds integer     NOT NULL DEFAULT 300 CHECK (timeout_seconds > 0),
        retries         integer     NOT NULL DEFAULT 0 CHECK (retries >= 0),
        active          boolean     NOT NULL DEFAULT true,
        created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
        created_by      text,
        CONSTRAINT automations_definitions_one_script
            CHECK (num_nonnulls(script_path, script_body) = 1)
    );
    CREATE UNIQUE INDEX automations_definitions_one_active
        ON automations_definitions (project_id, slug) WHERE active;
    CREATE INDEX automations_definitions_history_idx
        ON automations_definitions (project_id, slug, created_at);

    COMMENT ON TABLE automations_definitions IS
        'One row is one version of an automation. A change never edits a row: it '
        'inserts a new version and clears active on the version the writer read, in '
        'one transaction; removing an automation clears active with no successor. '
        'History is every version of a slug by created_at.';
    COMMENT ON COLUMN automations_definitions.script_path IS
        'The script as a file in the project, relative to its root, on the node that '
        'runs it. Exactly one of script_path and script_body is set.';
    COMMENT ON COLUMN automations_definitions.script_body IS
        'The script itself, run from a temporary file through its #! line. A body '
        'reads no file beside itself.';
    COMMENT ON COLUMN automations_definitions.schedule IS
        'A five-field cron expression read in timezone; null runs only when asked.';
    COMMENT ON COLUMN automations_definitions.timezone IS
        'The zone the schedule is read in, kept on the row so its meaning does not '
        'depend on which machine reads it.';
    COMMENT ON COLUMN automations_definitions.node IS
        'The one node that schedules the automation, matched against the node name '
        'its service runs under; null schedules it nowhere.';
    COMMENT ON COLUMN automations_definitions.created_by IS
        'Who saved this version, as the CLI resolved it.';

    CREATE FUNCTION automations_definitions_keep_history() RETURNS trigger AS $$
    BEGIN
      IF tg_op = 'UPDATE' AND (to_jsonb(new) - 'active') = (to_jsonb(old) - 'active') THEN
        RETURN new;
      END IF;
      RAISE EXCEPTION 'an automation version is never edited or deleted: % on automations_definitions is refused, and only active may move; save a new version instead', lower(tg_op)
        USING errcode = 'restrict_violation', constraint = 'automations_definitions_history';
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER automations_definitions_keep_history_row
        BEFORE UPDATE OR DELETE ON automations_definitions
        FOR EACH ROW EXECUTE FUNCTION automations_definitions_keep_history();
    CREATE TRIGGER automations_definitions_keep_history_truncate
        BEFORE TRUNCATE ON automations_definitions
        FOR EACH STATEMENT EXECUTE FUNCTION automations_definitions_keep_history();

    CREATE FUNCTION automations_definitions_announce() RETURNS trigger AS $$
    BEGIN
      PERFORM pg_notify('automations_changed', json_build_object(
          'schema', tg_table_schema, 'project', new.project_id, 'slug', new.slug)::text);
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER automations_definitions_changed
        AFTER INSERT OR UPDATE ON automations_definitions
        FOR EACH ROW EXECUTE FUNCTION automations_definitions_announce();
    """),
    ("0004-runs-node", """
    ALTER TABLE automations_runs ADD COLUMN node text;
    UPDATE automations_runs SET node = host;
    ALTER TABLE automations_runs ALTER COLUMN node SET NOT NULL;
    -- A release from before node existed, still running on another machine
    -- of the store, inserts no node; its host was the node name it ran under,
    -- so its run is that node's rather than refused.
    CREATE FUNCTION automations_runs_node_from_host() RETURNS trigger AS $$
    BEGIN
      IF new.node IS NULL THEN
        new.node := new.host;
      END IF;
      RETURN new;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER automations_runs_node_from_host
        BEFORE INSERT ON automations_runs
        FOR EACH ROW EXECUTE FUNCTION automations_runs_node_from_host();
    ALTER TABLE automations_runs ADD CONSTRAINT automations_runs_node_named CHECK (node <> '');
    ALTER TABLE automations_runs ADD COLUMN definition_id uuid
        CONSTRAINT automations_runs_definition_id_fkey
        REFERENCES automations_definitions (id);
    CREATE INDEX automations_runs_node_idx
        ON automations_runs (project_id, node, status, queued_at);
    COMMENT ON COLUMN automations_runs.node IS
        'The node the run belongs to; recovery, claims and running counts read only '
        'their own node''s runs. Rows from before it existed carry their host.';
    COMMENT ON COLUMN automations_runs.host IS
        'The machine that recorded or started the run, where its pid and log mean '
        'something.';
    COMMENT ON COLUMN automations_runs.definition_id IS
        'The definition version the run was queued under, and once started the one '
        'it ran.';
    COMMENT ON COLUMN automations_runs.environment IS
        'Rows from before definitions moved into the store: the environment selector; '
        'since then the node.';
    """),
    ("0005-definitions-versions", r"""
    CREATE OR REPLACE FUNCTION automations_definitions_announce() RETURNS trigger AS $$
    BEGIN
      PERFORM pg_notify('automations_changed', json_build_object(
          'schema', tg_table_schema, 'project', new.project_id, 'slug', new.slug)::text);
      EXECUTE format('INSERT INTO %I.schema_channel AS c (channel, scope, version) '
                     'VALUES (%L, $1, 1) ON CONFLICT (channel, scope) '
                     'DO UPDATE SET version = c.version + 1',
                     tg_table_schema, 'automations_changed')
          USING new.project_id;
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    """),
    ("0006-runs-result", """
    ALTER TABLE automations_runs ADD COLUMN result text
        CONSTRAINT automations_runs_result_bounded
        CHECK (result <> '' AND octet_length(result) <= 65536);
    CREATE INDEX automations_runs_result_idx
        ON automations_runs (project_id, automation_slug, started_at DESC NULLS LAST)
        WHERE result IS NOT NULL;
    COMMENT ON COLUMN automations_runs.result IS
        'What the run recorded for the next run of its automation, through '
        '`automations result`; the next run is handed the result of the latest '
        'started run that recorded one, whichever node ran it. At most 64 KiB, '
        'since it is handed over as one environment variable.';
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


def resolve_store(root: Path | None):
    """The store setting the project at `root` uses, as the shared library
    resolves it: the project's .env.local / .env, then the process environment,
    then the machine's store setting file. Read and never written; raises
    StoreUnavailable when no level answers or the one that does cannot be used."""
    db = _database()
    try:
        return db.resolve_setting(root)
    except db.DbError as exc:
        raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc


def store_in_force(root: Path | None) -> dict[str, Any]:
    """Which store the project's ledger uses, named without reaching it: the
    level that answered as `store`, the files or variables it came from as
    `store_sources`, and the schema it binds. Raises StoreUnavailable when there
    is none to use."""
    setting = resolve_store(root)
    return {"store": setting.level, "store_sources": list(setting.sources),
            "schema": setting.schema}


def store_name(setting) -> str:
    """Where a store setting points, as host:port/database, with nothing secret."""
    if setting.url is not None:
        from urllib.parse import urlparse
        found = urlparse(setting.url)
        return f"{found.hostname}:{found.port or 5432}/{found.path.lstrip('/')}"
    return f"{setting.host}:{setting.port}/{setting.database}"


def store_key(setting) -> tuple:
    """What tells two stores apart: where the connection goes, as whom, and the
    schema it binds, whichever level named it."""
    return (tuple(sorted((key, str(value)) for key, value in
                         setting.connect_kwargs().items())), setting.schema)


def open_ledger(root: Path, config: dict[str, Any] | None = None,
                strict: bool = True, follow: bool = False) -> "RunLedger":
    """The ledger of this project's runs, open on the store. The caller closes it.
    `follow` is the scheduler's: its link follows changed definitions.

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
    return RunLedger(project_id, root=root, follow=follow).open()


class StoreLink:
    """One held connection to the store the ledger lives in, migrated on open.

    A project's daemon holds one for its own ledger. The machine service holds
    one for each distinct store the projects it serves resolve: each project's
    ledger is a view onto its store's link, so the projects on one store cost
    it one connection rather than one each. The scheduler is single-threaded,
    so a transaction on a link is the only one open on it, whichever project's
    ledger opened it.

    `setting` is the store, as `resolve_store` gives it; without one the link
    resolves the store for `root` when it first opens. `error` is why the link
    is down, while it is.

    Every link is a transaction connection, at the store's transaction address
    where one is named, and holds no session state. A scheduler's link
    (`follow`) follows the `automations_changed` versions, which the store
    raises for a project on every definition of it saved or removed: when a
    project asks whether it changed, it polls them, at most once every
    FOLLOW_INTERVAL_SECONDS of the library, in one query on the link's own
    connection, and collects the projects whose version moved in `changed`.
    Where the store names its two addresses apart, the follower also listens
    on a session connection of its own, so a saved definition cuts the
    scheduler's sleep short (`wait_for_changes`); a transaction-only store
    holds no second connection. Its first poll is
    taken when it opens, before any definition is read, and is only where it
    starts from. `generation` moves on every open, so every project on the
    link reads its definitions again after the link was down. A poll that
    does not reach the store is taken again at the next, which reports every
    project that moved meanwhile."""

    def __init__(self, setting=None, root: Path | None = None, follow: bool = False):
        self.setting = setting
        self.root = root
        self.follow = follow
        self.conn = None
        self.warnings: list[str] = []
        self.in_transaction = False
        self.error: str | None = None
        self.changed: set[str] = set()
        self.generation = 0
        self.follower = None
        self._polled_at: float | None = None

    @property
    def name(self) -> str:
        return store_name(self.setting) if self.setting is not None else "unresolved"

    def open(self) -> "StoreLink":
        db = _database()
        if self.setting is None:
            self.setting = resolve_store(self.root)
        try:
            conn = db.connect(application_name=LEDGER_OWNER, setting=self.setting,
                              transaction=True)
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
        if self.follow:
            try:
                if self.follower is None:
                    self.follower = db.Follower([CHANGED_CHANNEL],
                                                application_name=LEDGER_OWNER,
                                                setting=self.setting)
                self._poll(conn)
            except db.DbError as exc:
                conn.close()
                raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
            # Every project on the link reads its definitions after an open,
            # so this poll is only where the next one starts from.
            self.changed.clear()
        self.conn = conn
        self.warnings = list(result.warnings)
        self.generation += 1
        return self

    def _poll(self, conn=None) -> None:
        """Take the followed versions once, on the link's connection; one that
        does not reach the store is left for the next, as the library's own
        follower leaves it."""
        import psycopg
        db = _database()
        self._polled_at = time.monotonic()
        try:
            moved = self.follower.poll(conn if conn is not None else self.conn)
        except db.DbError as exc:
            if exc.slug != "store_unreachable":
                raise
            return
        except psycopg.OperationalError:
            return
        self.changed.update(scope for _channel, scope in moved)

    def behind(self, project_id: str) -> bool:
        """Whether a definition of the project changed since last asked, polled
        now rather than at the next interval, and left to be taken."""
        if self.follower is not None:
            self._poll()
        return project_id in self.changed

    def take_changed(self, project_id: str) -> bool:
        """Whether a definition of the project changed since last asked."""
        if self.follower is not None and (
                self._polled_at is None
                or time.monotonic() - self._polled_at >= _database().FOLLOW_INTERVAL_SECONDS):
            self._poll()
        if project_id in self.changed:
            self.changed.discard(project_id)
            return True
        return False

    def close(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.close()
            self.conn = None
        self.in_transaction = False
        if self.follower is not None:
            # Its listener, if it holds one; the versions it read stay.
            self.follower.close()

    def lost(self) -> bool:
        """Whether the connection is gone, so the next statement cannot succeed
        on it whatever it is."""
        return self.conn is None or self.conn.closed or self.conn.broken

    def reopen(self) -> None:
        self.close()
        self.open()


def wait_for_changes(links: Sequence["StoreLink"], seconds: float) -> bool:
    """Sleep `seconds` between passes, cut short as soon as a link whose store
    names its two addresses apart is notified of a changed definition; that
    link then polls at its next ask rather than at its next interval. A
    follower that cannot listen waits out the time, so a lost notification
    costs no more than the sleep."""
    listening = [link for link in links if link.follower is not None
                 and link.setting is not None and link.setting.has_transaction_address]
    if not listening:
        time.sleep(seconds)
        return False
    if len(listening) == 1:
        noticed = listening[0].follower.wait(seconds)
    else:
        import threading
        woke = threading.Event()
        found: list = []

        def one(link: "StoreLink") -> None:
            if link.follower.wait(seconds, woke):
                found.append(link)
                woke.set()

        threads = [threading.Thread(target=one, args=(link,), name="automations-wake",
                                    daemon=True) for link in listening]
        for thread in threads:
            thread.start()
        woke.wait(seconds)
        woke.set()
        for thread in threads:
            thread.join()
        noticed = bool(found)
    if noticed:
        for link in listening:
            link._polled_at = None
    return noticed


class RunLedger:
    """Every question and every claim about runs, in one place that knows whose.

    The table is shared: one database holds the runs of every project on every
    machine, so a query that forgot which project it is asking about would answer
    with another project's runs. Scoping cannot therefore be a `WHERE` clause
    fifteen callers are trusted to remember — it is the reason this boundary
    exists, and it is applied here once rather than at each call.

    A ledger opens a link of its own, to the store resolved for `root`, unless
    it is handed one; a handed link is its owner's to open, reopen and close.

    Rows come back as they always have: times as the ISO text they were written
    as, and the cancel flag as 0 or 1, so what `runs` and `show` print does not
    depend on how the store keeps them."""

    def __init__(self, project_id: str, link: StoreLink | None = None,
                 root: Path | None = None, follow: bool = False):
        self.project_id = project_id
        self.node = NODE_NAME()
        self.host = socket.gethostname()
        self._owns_link = link is None
        self.link = link if link is not None else StoreLink(root=root, follow=follow)

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
        if isinstance(value, uuid.UUID):
            return str(value)
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
        """This node's runs that were starting or running."""
        return self._rows("node = %s AND status IN ('starting', 'running')", (self.node,))

    def pending(self, *, lock: bool = False) -> list[dict[str, Any]]:
        """This node's queue in order. `lock` holds the rows for the transaction
        it is read in, so two dispatchers cannot take the same one."""
        return self._rows("node = %s AND status = 'pending'", (self.node,),
                          order="queued_at, id", lock=lock)

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
        """How many of this node's runs are starting or running."""
        clause, params = self._where("node = %s", "status IN ('starting', 'running')")
        return self._execute("SELECT COUNT(*) FROM automations_runs" + clause,
                             params + [self.node]).fetchone()[0]

    def expire_foreign(self, slug: str, older_than_seconds: float) -> int:
        """Mark interrupted the active runs of the automation that belong to
        another node and are older than `older_than_seconds`, counted from their
        start, or from their queueing while pending. One run at a time holds
        across every node, so a node that went away or was renamed would
        otherwise hold its automation forever."""
        clause, params = self._where(
            "automation_slug = %s", "node <> %s",
            "status IN ('pending', 'starting', 'running')",
            "COALESCE(started_at, queued_at) < clock_timestamp() - make_interval(secs => %s)")
        with self.transaction():
            return self._execute(
                "UPDATE automations_runs SET status = 'interrupted', pid = NULL, "
                "finished_at = %s, summary = 'its node ' || node || ' stopped answering "
                "for it within its timeout and shutdown grace'" + clause,
                [iso()] + params + [slug, self.node, older_than_seconds]).rowcount

    # -- writes ---------------------------------------------------------------

    def claim(self, slug: str, state_dir: Path, *,
              trigger: str, scheduled_for: str | None = None,
              dedupe_key: str | None = None, attempt: int = 1,
              parent_run_id: str | None = None, node: str | None = None,
              definition_id: str | None = None) -> dict[str, Any] | None:
        """Take one firing, or find it already taken, for `node`, this one's
        unless named. The log path is this machine's until the run starts,
        when the machine that starts it writes its own.

        The key is the only conflict the insert treats as someone else's win.
        Anything else is a schema the code no longer matches, and a run that
        vanishes quietly is worse than one that fails loudly."""
        run_id = uuid.uuid4().hex
        log_path = state_dir / "runs" / f"{run_id}.log"
        node = node or self.node
        with self.transaction():
            inserted = self._execute(
                "INSERT INTO automations_runs (id, project_id, automation_slug, "
                "environment, trigger, scheduled_for, dedupe_key, status, attempt, "
                "parent_run_id, queued_at, log_path, host, node, definition_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (project_id, dedupe_key) DO NOTHING",
                (run_id, self.project_id, slug, node, trigger,
                 scheduled_for, dedupe_key, attempt, parent_run_id, iso(),
                 str(log_path), self.host, node, definition_id)).rowcount
        if not inserted:
            return None
        return self.get(run_id)

    def update(self, run_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{name} = %s" for name in columns)
        clause, params = self._where("id = %s")
        with self.transaction():
            self._execute(f"UPDATE automations_runs SET {assignments}" + clause,
                          list(columns.values()) + params + [run_id])

    def record_result(self, run_id: str, result: str) -> dict[str, Any] | None:
        """Record `result` on a run that is starting or running, and give the
        row back; None when the run is not one of these."""
        clause, params = self._where("id = %s", "status IN ('starting', 'running')")
        with self.transaction():
            changed = self._execute("UPDATE automations_runs SET result = %s" + clause,
                                    [result] + params + [run_id]).rowcount
        return self.get(run_id) if changed else None

    def last_result(self, slug: str) -> str | None:
        """The result the latest started run of the automation recorded, on
        any node, or None when no run of it has recorded one."""
        clause, params = self._where("automation_slug = %s", "result IS NOT NULL")
        row = self._execute("SELECT result FROM automations_runs" + clause
                            + " ORDER BY started_at DESC NULLS LAST LIMIT 1",
                            params + [slug]).fetchone()
        return row[0] if row else None

    def take(self, run_id: str) -> bool:
        """Move one pending run to starting, unless another dispatcher did."""
        clause, params = self._where("id = %s", "status = 'pending'")
        with self.transaction():
            return self._execute("UPDATE automations_runs SET status = 'starting'" + clause,
                                 params + [run_id]).rowcount == 1

    # -- definitions ----------------------------------------------------------

    def _definition_rows(self, predicates: str, params: Sequence[Any],
                         order: str = "slug", lock: bool = False) -> list[dict[str, Any]]:
        sql = ("SELECT * FROM automations_definitions WHERE project_id = %s AND "
               + predicates + f" ORDER BY {order}" + (" FOR UPDATE" if lock else ""))
        cursor = self._execute(sql, [self.project_id, *params])
        names = [c[0] for c in cursor.description]
        return [{name: (str(value) if isinstance(value, uuid.UUID) else value)
                 for name, value in zip(names, row)} for row in cursor.fetchall()]

    def definitions(self) -> list[dict[str, Any]]:
        """The project's active definitions, one per slug."""
        return self._definition_rows("active", ())

    def placed_nodes(self) -> list[str] | None:
        """The nodes this project's automations service is placed on, as the
        manager's placement rows in the store say (`capabilities place`,
        `service start|stop`), sorted; None where the store keeps no placement
        to read, as one the manager never migrated."""
        import psycopg
        try:
            with self.conn.transaction():
                rows = self._execute(
                    "SELECT node FROM capabilities_policy WHERE agent_id = %s "
                    "AND capability = %s AND service AND node IS NOT NULL ORDER BY node",
                    (self.project_id, PLACEMENT_CAPABILITY)).fetchall()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return None
        return [row[0] for row in rows]

    def definition(self, slug: str, version: str | None = None) -> dict[str, Any] | None:
        """One definition: the active version, or the version named."""
        rows = (self._definition_rows("slug = %s AND id::text = %s", (slug, version))
                if version else self._definition_rows("slug = %s AND active", (slug,)))
        return rows[0] if rows else None

    def definition_history(self, slug: str) -> list[dict[str, Any]]:
        return self._definition_rows("slug = %s", (slug,), order="created_at, id")

    def save_definition(self, slug: str, fields: dict[str, Any], *, by: str | None,
                        base: str | None = None) -> tuple[dict[str, Any], bool]:
        """Save a version of the definition and say whether one was created: a
        save identical to the active version creates none. `base` is the
        version the writer read; without it, the version active now is the one
        replaced. Raises ConfigError on a conflict."""
        with self.transaction():
            active = self._definition_rows("slug = %s AND active", (slug,), lock=True)
            active = active[0] if active else None
            if base is not None and (active is None or active["id"] != base):
                raise ConfigError(
                    f"automation {slug!r} is no longer at version {base}: "
                    + (f"{active['id']} is active" if active else "it has no active version"))
            if active is not None and all(
                    (list(active[name]) if name == "arguments" else active[name]) == fields[name]
                    for name in DEFINITION_FIELDS):
                return active, False
            if active is not None:
                self._execute("UPDATE automations_definitions SET active = false "
                              "WHERE id = %s AND active", [active["id"]])
            columns = ["project_id", "slug", *DEFINITION_FIELDS, "created_by"]
            values = [self.project_id, slug, *(fields[name] for name in DEFINITION_FIELDS), by]
            try:
                cursor = self._execute(
                    f"INSERT INTO automations_definitions ({', '.join(columns)}) "
                    f"VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id", values)
            except Exception as exc:
                if type(exc).__name__ != "UniqueViolation":
                    raise
                raise ConfigError(f"automation {slug!r} was saved by someone else "
                                  "first; read it again and save over that") from None
            created = str(cursor.fetchone()[0])
        return self.definition(slug, created), True

    def remove_definition(self, slug: str, base: str | None = None) -> dict[str, Any] | None:
        """Clear `active` on the definition, leaving it no successor; None when
        it had no active version. Raises ConfigError when `base` is stale."""
        with self.transaction():
            rows = self._definition_rows("slug = %s AND active", (slug,), lock=True)
            if not rows:
                return None
            if base is not None and rows[0]["id"] != base:
                raise ConfigError(f"automation {slug!r} is no longer at version {base}: "
                                  f"{rows[0]['id']} is active")
            self._execute("UPDATE automations_definitions SET active = false "
                          "WHERE id = %s AND active", [rows[0]["id"]])
        return rows[0]


def enqueue_manual(
    root: Path, config: dict[str, Any], state_dir: Path, automation_id: str
) -> dict[str, Any]:
    """Queue one run of the automation for the node `manual_run_node` names."""
    item = automation_map(config).get(automation_id)
    if item is None:
        raise KeyError(automation_id)
    if not item["enabled"]:
        raise ConfigError(f"automation {automation_id!r} is disabled")
    with open_ledger(root, config) as ledger:
        node = manual_run_node(item, ledger.node, config.get("placed_on"))
        row = ledger.claim(automation_id, state_dir, trigger="manual", node=node,
                           definition_id=item["version"])
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


def record_result(root: Path, config: dict[str, Any], run_id: str,
                  result: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Record `result` on the run, as `(run as it was, run as recorded)`; the
    second is None when the run is not starting or running."""
    with open_ledger(root, config) as ledger:
        row = ledger.get(run_id)
        if row is None:
            return None, None
        return row, ledger.record_result(run_id, result)


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
        if item is None or not item["enabled"]:
            raise ConfigError(f"automation {slug!r} is missing or disabled")
        return ledger.claim(
            slug, state_dir, trigger="retry", attempt=int(row["attempt"]) + 1,
            parent_run_id=row["parent_run_id"] or row["id"], node=item["node"],
            definition_id=item["version"])


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
# How soon a store that went away, or did not answer when the scheduler
# started, is asked for again: the first interval, doubled after each attempt
# that fails, up to the longest.
RECONNECT_FIRST_SECONDS = 1.0
RECONNECT_LONGEST_SECONDS = 30.0


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
    """The tag of this project's log lines: the slug the CLI that launched the
    daemon resolved for it, else an older project.json's, else the folder's name."""
    handed = os.environ.get("AUTOMATIONS_PROJECT_SLUG", "").strip()
    try:
        if handed and Path(os.environ.get("AUTOMATIONS_PROJECT_ROOT") or "").resolve() \
                == Path(root).resolve():
            return handed
    except OSError:
        pass
    slug = _project_identity(root).get("slug")
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
    # The definition version the job runs, and the file a script body runs
    # from, named by the body's digest.
    definition_id: str | None = None
    body_file: Path | None = None


# Where a script kept in the store is written, once per body by its digest
# (capabilities_contract.bodies), under the project's state root.
BODIES_DIR = "bodies"


class Daemon:
    """One project's scheduler: its declaration, its ledger and its running jobs.

    In project mode a process runs one, through `run`. The machine service holds
    one for every project it serves and drives the same methods - `recover`,
    `reap`, `take_pause`, `schedule_due`, `dispatch`, `reload_declaration`,
    `shutdown` - so what is registered, how a job starts and how it ends are one
    code in both modes. What the machine service hands in is what is the
    machine's to hand: the project's ledger on its store's shared link, the
    loader that reads the project's declaration as the project itself resolves
    it, the launcher that starts each job inside the project's own environment,
    and the log, which names the project on every line."""

    def __init__(self, root: Path, config_path: Path | None, state_dir: Path, *,
                 slug: str | None = None, loader=None, runs: RunLedger | None = None,
                 launcher=None, log=None):
        self.root = root.resolve()
        # None for an agent whose settings are rows of the store: its
        # declaration comes through `loader` alone.
        self.config_path = config_path.resolve() if config_path is not None else None
        self.state_dir = state_dir.resolve()
        self.slug = slug or _project_slug(self.root)
        self._loader = loader
        self._launcher = launcher
        self._log = log
        self.children: dict[str, Child] = {}
        self.stop_requested = False
        self.reload_requested = False
        self.reload_error: str | None = None
        self.pause: dict | None = None
        self.definition_problems: list[str] = []
        self._generation = -1
        self._placement_read_at = 0.0
        # A lost store is asked for again on a doubling interval, not on every tick.
        self._reopen_at = 0.0
        self._reopen_delay = RECONNECT_FIRST_SECONDS
        # The file's part is read first, so a declaration that does not load is
        # refused before the store is opened for it.
        file_config = self._load_file()
        self.runs = runs if runs is not None else open_ledger(self.root, follow=True)
        self.config = self.load(file_config)
        self.by_id = automation_map(self.config)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "runs").mkdir(parents=True, exist_ok=True)
        for warning in self.runs.warnings:
            self.log(warning)

    @property
    def node(self) -> str:
        return self.runs.node

    def _load_file(self) -> dict[str, Any]:
        if self._loader is not None:
            return self._loader()
        return load_effective_config(self.root, self.config_path, self.state_dir)

    def load(self, file_config: dict[str, Any] | None = None) -> dict[str, Any]:
        """The declaration: the file's part, and the project's definitions as
        the store holds them now. A definition that does not validate is
        logged once and left out, and the others are loaded."""
        config = file_config if file_config is not None else self._load_file()
        generation = self.runs.link.generation
        self.runs.link.take_changed(self.runs.project_id)
        problems = apply_definitions(self.root, config, self.runs.definitions(),
                                     self.runs.placed_nodes())
        self._placement_read_at = time.monotonic()
        for problem in problems:
            if problem not in self.definition_problems:
                self.log(problem)
        self.definition_problems = problems
        self._generation = generation
        return config

    def follow_placement(self) -> None:
        """Read where the service is placed again, at most once every
        PLACEMENT_REFRESH_SECONDS: a move changes no definition, so the store
        announces nothing this daemon follows, and the automations that follow
        the service are taken up here within that interval."""
        if time.monotonic() - self._placement_read_at < PLACEMENT_REFRESH_SECONDS:
            return
        self._placement_read_at = time.monotonic()
        placed = self.runs.placed_nodes()
        if placed == self.config.get("placed_on"):
            return
        self.config["placed_on"] = placed
        here = sum(1 for item in self.config["automations"]
                   if scheduled_here(item, self.node, placed))
        self.log(f"the service is placed on {', '.join(placed) if placed else 'no node recorded'}"
                 f", {here} automations scheduled on node {self.node}")

    def follow_definitions(self) -> None:
        """Take up the definitions again when the store announced a change to
        one of this project's, or the link reconnected and may have missed one."""
        link = self.runs.link
        if link.generation != self._generation or link.take_changed(self.runs.project_id):
            self.reload_declaration(self.state_dir / DAEMON_FINGERPRINT_FILE)

    def log(self, message: str) -> None:
        if self._log is not None:
            self._log(message)
            return
        sys.stderr.write(log_line(self.slug, message))
        sys.stderr.flush()

    def _claim(self, slug: str, **kwargs) -> dict[str, Any] | None:
        return self.runs.claim(slug, self.state_dir, **kwargs)

    def recover(self) -> None:
        """Close what this node left starting or running. A pid is signalled
        only where this machine recorded it: a node name moves with a
        container that is redeployed, and its old pids were another machine's."""
        rows = self.runs.unfinished()
        live_pids = [int(row["pid"]) for row in rows
                     if row["pid"] and row["host"] == self.runs.host]
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
                and runs_here(item, self.node, self.config.get("placed_on"))
            ):
                self._claim(slug, trigger="recovery", attempt=int(row["attempt"]) + 1,
                            parent_run_id=row["parent_run_id"] or row["id"],
                            definition_id=item["version"])

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
        """Register the firings due now of the automations this node schedules.
        One run of an automation at a time holds across every node: a firing
        that finds one active anywhere is recorded skipped, once another
        node's run past its timeout and grace is closed for it."""
        grace = self.config["engine"]["shutdown_grace_seconds"]
        for item in self.config["automations"]:
            if not scheduled_here(item, self.node, self.config.get("placed_on")) \
                    or not item["schedule"]:
                continue
            local = now.astimezone(ZoneInfo(item["timezone"]))
            if not cron_matches(item["schedule"], local):
                continue
            scheduled_text = iso(local.replace(second=0, microsecond=0))
            dedupe = f"{item['id']}:{scheduled_text}"
            self.runs.expire_foreign(item["id"], item["timeout_seconds"] + grace)
            if self._has_active(item["id"]):
                self._insert_skipped(item, scheduled_text, dedupe, "overlap policy")
                continue
            self._claim(item["id"], trigger="schedule", scheduled_for=scheduled_text,
                        dedupe_key=dedupe, definition_id=item["version"])

    def _insert_skipped(self, item: dict[str, Any], scheduled: str, dedupe: str,
                        reason: str) -> None:
        row = self._claim(
            item["id"],
            trigger="schedule",
            scheduled_for=scheduled,
            dedupe_key=dedupe,
            definition_id=item["version"],
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
                if item is None or not runs_here(item, self.node,
                                                 self.config.get("placed_on")):
                    # A run queued just after its definition changed is not
                    # skipped on a declaration the last poll left behind: it
                    # waits for the next tick, which reads the definitions first.
                    if self.runs.link.behind(self.runs.project_id):
                        continue
                    self.runs.update(
                        row["id"], status="skipped", finished_at=iso(),
                        summary="automation missing, disabled, or scheduled on another node")
                    continue
                if self.runs.count_for(slug, "starting") + \
                        self.runs.count_for(slug, "running") >= 1:
                    continue
                if self.runs.take(row["id"]):
                    # Read with the claim, so starting the job asks the store
                    # nothing more.
                    row["last_result"] = self.runs.last_result(slug)
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

    def _body_file(self, run_id: str, body: str) -> Path:
        """The script body written where the run starts it from: the file the
        shared contract names by the body's digest in the project's state root,
        executable by this user alone, written once and kept for every run of
        that body."""
        from capabilities_contract.bodies import materialise
        try:
            return materialise(body, self.state_dir / BODIES_DIR)
        except ValueError as exc:
            raise OSError(str(exc)) from exc

    def _start(self, row: dict[str, Any]) -> None:
        item = self.by_id[row["automation_slug"]]
        body_file: Path | None = None
        if item["script_body"] is not None:
            try:
                body_file = self._body_file(row["id"], item["script_body"])
            except OSError as exc:
                self._finish_without_child(row, "failed", None,
                                           f"cannot write the script body to run: {exc}")
                return
            command = [str(body_file), *item["arguments"]]
        else:
            script = item["script_path"]
            if not script.is_file():
                self._finish_without_child(row, "failed", None, f"script not found: {script}")
                return
            command = [str(script), *item["arguments"]]
            if script.suffix == ".py" and not os.access(script, os.X_OK):
                command = [sys.executable, str(script), *item["arguments"]]
            elif not os.access(script, os.X_OK):
                self._finish_without_child(row, "failed", None,
                                           f"script is not executable: {script}")
                return
        # The log is a file on the machine that runs the job, whichever machine
        # queued it.
        log_path = self.state_dir / "runs" / f"{row['id']}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "AUTOMATION_ID": row["automation_slug"],
            "AUTOMATION_RUN_ID": row["id"],
            "AUTOMATION_ATTEMPT": str(row["attempt"]),
            "AUTOMATION_TRIGGER": row["trigger"],
            # What a script reads stays what it read before definitions moved
            # into the store, though the run row now records the node here.
            "AUTOMATION_ENVIRONMENT": self.config["engine"]["environment"],
            "AUTOMATION_NAMESPACE": self.config["engine"]["namespace"],
            "AUTOMATION_PROJECT_ROOT": str(self.root),
            "AUTOMATION_STATE_DIR": str(self.state_dir),
            "AUTOMATIONS_BIN": automations_bin(),
        })
        # Only the answer the claim read reaches the job, never one this
        # process inherited.
        env.pop("AUTOMATION_LAST_RESULT", None)
        if row.get("last_result") is not None:
            env["AUTOMATION_LAST_RESULT"] = row["last_result"]
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
            definition_id=item["version"],
            body_file=body_file,
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
                self.runs.update(child.run_id, **self._started_columns(child))
                child.started = True
            return
        columns = dict(child.outcome, pid=None)
        if not child.started:
            columns = {**self._started_columns(child), **columns}
        self.runs.update(child.run_id, **columns)
        del self.children[child.run_id]
        self._discard_body(child)
        if columns["status"] == "failed":
            self._maybe_retry(child.run_id)

    def _started_columns(self, child: Child) -> dict[str, Any]:
        """What the ledger records of a job as it starts: here, on this machine,
        with its pid, its log, and the definition version it runs."""
        return {"status": "running", "started_at": child.started_at,
                "pid": child.process.pid, "host": self.runs.host,
                "log_path": str(child.log_path), "definition_id": child.definition_id}

    @staticmethod
    def _discard_body(child: Child) -> None:
        """A body's file is named by its digest and shared by every run of
        that body, so it stays when one run ends."""

    def _maybe_retry(self, run_id: str) -> None:
        row = self.runs.get(run_id)
        if row is None:
            return
        slug = row["automation_slug"]
        item = self.by_id.get(slug)
        if item is None or int(row["attempt"]) > item["retries"]:
            return
        self._claim(slug, trigger="retry", attempt=int(row["attempt"]) + 1,
                    parent_run_id=row["parent_run_id"] or row["id"],
                    definition_id=item["version"])

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
                self._discard_body(child)
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
        self.reload_error = None
        fingerprint_path.write_text(daemon_record(config))
        here = sum(1 for item in config["automations"]
                   if scheduled_here(item, self.node, config.get("placed_on")))
        self.log(f"reloaded, {len(self.by_id)} automations declared, {here} scheduled "
                 f"on node {self.node}")
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
                wait_for_changes([self.runs.link], self.config["engine"]["tick_seconds"])
        finally:
            self.shutdown()
            pid_path.unlink(missing_ok=True)
            fingerprint_path.unlink(missing_ok=True)
            self.runs.close()
            lock.close()

    def prepare(self) -> None:
        """Reap what ended, take up changed definitions and the pause, and
        register the firings due unless paused: everything a tick does short of
        starting work. Reaping comes first because it supervises the running
        jobs, which a store that is gone must not hold up."""
        self.reap()
        self.follow_definitions()
        self.follow_placement()
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
            now = time.monotonic()
            if now < self._reopen_at:
                return
            self.log(f"lost the store connection, reconnecting: {exc}")
            try:
                self.runs.reopen()
            except StoreUnavailable as again:
                self.log(f"the store is still unavailable, asking again in "
                         f"{self._reopen_delay:g}s: {again.message}")
                self._reopen_at = now + self._reopen_delay
                self._reopen_delay = min(self._reopen_delay * 2, RECONNECT_LONGEST_SECONDS)
            else:
                self._reopen_at = 0.0
                self._reopen_delay = RECONNECT_FIRST_SECONDS


# --- machine mode ----------------------------------------------------------------

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
        # The link to the store the project resolves, shared with every
        # project on that store.
        self.link = machine.link_for(host.setting)
        self.state_dir = Path(host.state_dir)
        self.daemon: Daemon | None = None
        self.recovered = False
        self.load_error: str | None = None
        self.tick_error: str | None = None
        self.refusal: tuple[str, str] | None = None
        self.next_tick = 0.0
        self.leaving: str | None = None
        # A declaration that failed to load because its store was away is
        # loaded again once the store answers, without waiting for a reload.
        self.load_waits_on_store = False

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
                            runs=RunLedger(self.project_id, link=self.link),
                            launcher=self.host.launcher, log=self.log)
        except Exception as exc:
            self.load_waits_on_store = self.link.lost() or isinstance(exc, StoreUnavailable)
            reason = _why(exc)
            if reason != self.load_error:
                self.log(f"error: its declaration does not load, so nothing is registered "
                         f"or started for it until a reload takes one: {reason}")
            self.load_error = reason
            with contextlib.suppress(OSError):
                (self.state_dir / DAEMON_FINGERPRINT_FILE).unlink()
            return False
        self.daemon, self.load_error, self.recovered = daemon, None, False
        self.load_waits_on_store = False
        write_atomic(self.state_dir / DAEMON_FINGERPRINT_FILE, daemon_record(daemon.config))
        here = sum(1 for item in daemon.config["automations"]
                   if scheduled_here(item, daemon.node, daemon.config.get("placed_on")))
        self.log(f"served: {len(daemon.by_id)} automations declared, {here} scheduled on "
                 f"node {daemon.node}, environment {daemon.config['engine']['environment']}")
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
        if self.daemon is None and self.load_waits_on_store:
            self.load()
        daemon = self.daemon
        if daemon is None:
            return
        if not self.recovered:
            daemon.recover()
            self.recovered = True
        daemon.reap()
        daemon.follow_definitions()
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
                "node": self.daemon.node,
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
    what is the machine's: one store link for each distinct store its projects
    resolve, which the ledger of every project on that store is a view onto,
    an optional cap on jobs running at once across every project, and
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
        # One link per distinct store, by `store_key`.
        self.links: dict[tuple, StoreLink] = {}
        # Every project on the list by its id: what the list says of it, why it
        # is not served while it is not, and its slot once it is.
        self.entries: dict[str, dict] = {}
        self.draining: list[ProjectSlot] = []
        self.list_witness: Any = ()
        self.list_error: str | None = None
        self.settings: dict = {}
        self.settings_error: str | None = None
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
            "stores": [{"store": link.name, "schema": link.setting.schema,
                        "connected": not link.lost(), "error": link.error}
                       for link in self.links.values()],
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
        except BaseException:
            self._lock.close()
            raise
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
                wait_for_changes(list(self.links.values()), self.sleep_seconds())
        finally:
            self.shutdown()
            for link in self.links.values():
                link.close()
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
        for link in self.links.values():
            if link.lost():
                self._reconnect(link, "the store connection is closed")
        due = []
        for slot in (*self.draining, *self.slots()):
            if slot.link.lost():
                # Without its store nothing is registered, started or recorded
                # for the project, but every running job is still timed out
                # and stopped.
                if slot.daemon is not None:
                    slot.daemon.supervise()
                continue
            if now < slot.next_tick:
                continue
            slot.next_tick = now + slot.tick_seconds()
            if self._guard(slot, slot.prepare) is not False:
                due.append(slot)
        self.deal([slot for slot in due if slot.can_start()])
        self._release_drained()
        self._drop_unused_links()
        self.publish()

    def _guard(self, slot: ProjectSlot, action):
        """Run one project's action; a fault of the project's stays the
        project's, and a lost store link is its store's, reconnected at once and
        otherwise every pass. A project whose link is down is asked nothing."""
        if slot.link.lost():
            return False
        try:
            return action()
        except Exception as exc:
            if slot.link.lost():
                self._reconnect(slot.link, exc)
            else:
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

    # --- the stores -----------------------------------------------------------

    def link_for(self, setting) -> StoreLink:
        """The link to `setting`'s store, opened the first time a project on it
        is served. A store that does not answer then is asked again every pass."""
        key = store_key(setting)
        link = self.links.get(key)
        if link is None:
            link = self.links[key] = StoreLink(setting, follow=True)
            try:
                link.open()
            except StoreUnavailable as exc:
                link.error = exc.message
                self.log(f"the store {link.name} is unavailable, asking again every "
                         f"pass: {exc.slug}: {exc.message}")
            else:
                for warning in link.warnings:
                    self.log(warning)
        return link

    def _reconnect(self, link: StoreLink, why) -> None:
        if link.error is None:
            self.log(f"lost the store connection to {link.name}, reconnecting every "
                     f"pass: {why}")
        try:
            link.reopen()
        except StoreUnavailable as again:
            if again.message != link.error:
                self.log(f"the store {link.name} is unavailable: {again.slug}: "
                         f"{again.message}")
            link.error = again.message
            return
        self.log(f"the store {link.name} answers again")
        link.error = None

    def _drop_unused_links(self) -> None:
        """Close the link of a store no project served or letting go is on."""
        used = {id(slot.link) for slot in (*self.slots(), *self.draining)}
        for key, link in list(self.links.items()):
            if id(link) not in used:
                link.close()
                del self.links[key]

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


# How the CLI tells the daemon it starts that the project's declaration is not
# a file: the agent's settings are rows of the store, read by the CLI alone.
DECLARATION_FROM_CLI = "cli"


def cli_declaration(root: Path) -> dict[str, Any]:
    """The declaration of a project whose settings are rows of the store, as
    the CLI reads it (`automations service declaration`), validated here as a
    file's would be. Raises ConfigError naming what the CLI said."""
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    try:
        proc = subprocess.run([automations_bin(), "service", "declaration"],
                              cwd=str(root), env=env, capture_output=True, text=True,
                              timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"cannot read the declaration through the CLI: {exc}") from exc
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = said[-1] if said else f"exit {proc.returncode}"
        with contextlib.suppress(ValueError, AttributeError, TypeError, KeyError):
            error = json.loads(detail)["error"]
            detail = f"{error['code']}: {error['message']}"
        raise ConfigError(f"the declaration does not load: {detail}")
    try:
        raw = json.loads(proc.stdout)
    except ValueError as exc:
        raise ConfigError(f"the declaration the CLI printed is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("the declaration the CLI printed is not an object")
    return normalise_config(root, raw)


def run_from_env() -> None:
    root_raw = os.environ.get("AUTOMATIONS_PROJECT_ROOT")
    config_raw = os.environ.get("AUTOMATIONS_CONFIG")
    state_raw = os.environ.get("AUTOMATIONS_STATE_DIR")
    from_cli = os.environ.get("AUTOMATIONS_DECLARATION") == DECLARATION_FROM_CLI
    if not root_raw or not state_raw or not (config_raw or from_cli):
        raise RuntimeError(
            "AUTOMATIONS_PROJECT_ROOT, AUTOMATIONS_STATE_DIR, and AUTOMATIONS_CONFIG "
            f"(or AUTOMATIONS_DECLARATION={DECLARATION_FROM_CLI}) are required"
        )
    root = Path(root_raw)
    if from_cli:
        _daemon_when_the_store_answers(
            lambda: Daemon(root, None, Path(state_raw),
                           loader=lambda: cli_declaration(root))).run()
        return
    _daemon_when_the_store_answers(
        lambda: Daemon(root, Path(config_raw), Path(state_raw))).run()


def _daemon_when_the_store_answers(make) -> "Daemon":
    """The scheduler, made once its store answers. A store that does not
    answer when it starts is asked for again on a doubling interval, said once,
    rather than ending the process for its supervisor to start again; a store
    setting that cannot be used still ends it."""
    delay, said = RECONNECT_FIRST_SECONDS, None
    tag = _project_slug(Path(os.environ.get("AUTOMATIONS_PROJECT_ROOT") or "."))
    while True:
        try:
            return make()
        except StoreUnavailable as exc:
            if exc.slug not in ("store_unreachable", "store_busy"):
                raise
            if exc.message != said:
                said = exc.message
                sys.stderr.write(log_line(tag, f"the store is unavailable, asking again: "
                                               f"{exc.slug}: {exc.message}"))
                sys.stderr.flush()
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_LONGEST_SECONDS)


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
