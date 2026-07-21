from __future__ import annotations

import contextlib
import os
import re
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
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ACTIVE_STATUSES = ("pending", "starting", "running")
FINAL_STATUSES = ("succeeded", "failed", "canceled", "interrupted", "skipped")
ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ConfigError(ValueError):
    pass


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


def load_config(root: Path, config_path: Path) -> dict[str, Any]:
    try:
        raw = tomllib.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
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
            raise ConfigError(f"{label}.script must be relative to the project root")
        resolved_script = (root / script).resolve()
        try:
            resolved_script.relative_to(root.resolve())
        except ValueError as exc:
            raise ConfigError(f"{label}.script escapes the project root") from exc
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
    return {"version": 1, "engine": normalized_engine, "automations": automations}


def automation_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in config["automations"]}


def applies(item: dict[str, Any], environment: str) -> bool:
    return item["enabled"] and (
        not item["environments"] or environment in item["environments"]
    )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            environment TEXT NOT NULL,
            trigger TEXT NOT NULL,
            scheduled_for TEXT,
            dedupe_key TEXT UNIQUE,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            parent_run_id TEXT,
            queued_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            pid INTEGER,
            exit_code INTEGER,
            summary TEXT,
            log_path TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(status, queued_at);
        CREATE INDEX IF NOT EXISTS runs_automation_idx ON runs(automation_id, queued_at);
        """
    )
    return db


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def enqueue(
    db: sqlite3.Connection,
    config: dict[str, Any],
    state_dir: Path,
    automation_id: str,
    *,
    trigger: str,
    scheduled_for: str | None = None,
    dedupe_key: str | None = None,
    attempt: int = 1,
    parent_run_id: str | None = None,
) -> dict[str, Any] | None:
    run_id = uuid.uuid4().hex
    log_path = state_dir / "runs" / f"{run_id}.log"
    engine = config["engine"]
    try:
        db.execute(
            """
            INSERT INTO runs (
                id, automation_id, namespace, environment, trigger, scheduled_for,
                dedupe_key, status, attempt, parent_run_id, queued_at, log_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                run_id,
                automation_id,
                engine["namespace"],
                engine["environment"],
                trigger,
                scheduled_for,
                dedupe_key,
                attempt,
                parent_run_id,
                iso(),
                str(log_path),
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return None
    return row_dict(db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def enqueue_manual(
    db_path: Path, config: dict[str, Any], state_dir: Path, automation_id: str
) -> dict[str, Any]:
    item = automation_map(config).get(automation_id)
    if item is None:
        raise KeyError(automation_id)
    if not applies(item, config["engine"]["environment"]):
        raise ConfigError(
            f"automation {automation_id!r} is disabled or not enabled for environment "
            f"{config['engine']['environment']!r}"
        )
    with contextlib.closing(connect(db_path)) as db:
        row = enqueue(db, config, state_dir, automation_id, trigger="manual")
    assert row is not None
    return row


def list_runs(
    db_path: Path, *, limit: int = 50, status: str | None = None
) -> list[dict[str, Any]]:
    with contextlib.closing(connect(db_path)) as db:
        if status:
            rows = db.execute(
                "SELECT * FROM runs WHERE status = ? ORDER BY queued_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM runs ORDER BY queued_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def get_run(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with contextlib.closing(connect(db_path)) as db:
        return row_dict(db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def counts(db_path: Path) -> dict[str, int]:
    with contextlib.closing(connect(db_path)) as db:
        rows = db.execute("SELECT status, COUNT(*) AS count FROM runs GROUP BY status").fetchall()
    return {row["status"]: row["count"] for row in rows}


def request_cancel(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with contextlib.closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        if row["status"] == "pending":
            db.execute(
                "UPDATE runs SET status = 'canceled', cancel_requested = 1, "
                "finished_at = ? WHERE id = ?",
                (iso(), run_id),
            )
        elif row["status"] in {"starting", "running"}:
            db.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
        db.commit()
        return row_dict(db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def retry_run(
    db_path: Path, config: dict[str, Any], state_dir: Path, run_id: str
) -> dict[str, Any] | None:
    with contextlib.closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        if row["status"] not in FINAL_STATUSES:
            raise ConfigError(f"run {run_id} is still {row['status']}")
        item = automation_map(config).get(row["automation_id"])
        if item is None or not applies(item, config["engine"]["environment"]):
            raise ConfigError(
                f"automation {row['automation_id']!r} is missing, disabled, or outside "
                f"environment {config['engine']['environment']!r}"
            )
        return enqueue(
            db,
            config,
            state_dir,
            row["automation_id"],
            trigger="retry",
            attempt=int(row["attempt"]) + 1,
            parent_run_id=row["parent_run_id"] or row["id"],
        )


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
        self.db_path = self.state_dir / "automations.db"
        self.config = load_config(self.root, self.config_path)
        self.by_id = automation_map(self.config)
        self.children: dict[str, Child] = {}
        self.stop_requested = False
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "runs").mkdir(parents=True, exist_ok=True)
        self.db = connect(self.db_path)

    def recover(self) -> None:
        rows = self.db.execute(
            "SELECT * FROM runs WHERE status IN ('starting', 'running')"
        ).fetchall()
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
            self.db.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, pid = NULL, "
                "summary = 'daemon restarted while run was active' WHERE id = ?",
                (iso(), row["id"]),
            )
            self.db.commit()
            item = self.by_id.get(row["automation_id"])
            if (
                self.config["engine"]["recovery"] == "retry"
                and item is not None
                and applies(item, self.config["engine"]["environment"])
            ):
                enqueue(
                    self.db,
                    self.config,
                    self.state_dir,
                    row["automation_id"],
                    trigger="recovery",
                    attempt=int(row["attempt"]) + 1,
                    parent_run_id=row["parent_run_id"] or row["id"],
                )

    def _has_active(self, automation_id: str) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.execute(
            f"SELECT 1 FROM runs WHERE automation_id = ? AND status IN ({placeholders}) LIMIT 1",
            (automation_id, *ACTIVE_STATUSES),
        ).fetchone()
        return row is not None

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
            pending = self.db.execute(
                "SELECT COUNT(*) FROM runs WHERE automation_id = ? AND status = 'pending'",
                (item["id"],),
            ).fetchone()[0]
            if pending >= item["max_pending"]:
                self._insert_skipped(item["id"], scheduled_text, dedupe, "pending limit")
                continue
            enqueue(
                self.db,
                self.config,
                self.state_dir,
                item["id"],
                trigger="schedule",
                scheduled_for=scheduled_text,
                dedupe_key=dedupe,
            )

    def _insert_skipped(self, automation_id: str, scheduled: str, dedupe: str, reason: str) -> None:
        row = enqueue(
            self.db,
            self.config,
            self.state_dir,
            automation_id,
            trigger="schedule",
            scheduled_for=scheduled,
            dedupe_key=dedupe,
        )
        if row:
            self.db.execute(
                "UPDATE runs SET status = 'skipped', finished_at = ?, summary = ? WHERE id = ?",
                (iso(), reason, row["id"]),
            )
            self.db.commit()

    def _claim_one(self) -> dict[str, Any] | None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            active = self.db.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('starting', 'running')"
            ).fetchone()[0]
            if active >= self.config["engine"]["max_parallel"]:
                self.db.commit()
                return None
            rows = self.db.execute(
                "SELECT * FROM runs WHERE status = 'pending' ORDER BY queued_at, id"
            ).fetchall()
            for row in rows:
                item = self.by_id.get(row["automation_id"])
                if item is None or not applies(item, self.config["engine"]["environment"]):
                    self.db.execute(
                        "UPDATE runs SET status = 'skipped', finished_at = ?, summary = ? WHERE id = ?",
                        (iso(), "automation missing, disabled, or outside environment", row["id"]),
                    )
                    continue
                same = self.db.execute(
                    "SELECT COUNT(*) FROM runs WHERE automation_id = ? "
                    "AND status IN ('starting', 'running')",
                    (row["automation_id"],),
                ).fetchone()[0]
                if same >= item["max_parallel"]:
                    continue
                updated = self.db.execute(
                    "UPDATE runs SET status = 'starting' WHERE id = ? AND status = 'pending'",
                    (row["id"],),
                ).rowcount
                if updated:
                    self.db.commit()
                    return dict(row)
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
        item = self.by_id[row["automation_id"]]
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
            "AUTOMATION_ID": row["automation_id"],
            "AUTOMATION_RUN_ID": row["id"],
            "AUTOMATION_ATTEMPT": str(row["attempt"]),
            "AUTOMATION_TRIGGER": row["trigger"],
            "AUTOMATION_ENVIRONMENT": row["environment"],
            "AUTOMATION_NAMESPACE": row["namespace"],
            "AUTOMATION_PROJECT_ROOT": str(self.root),
            "AUTOMATION_STATE_DIR": str(self.state_dir),
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
        self.db.execute(
            "UPDATE runs SET status = 'running', started_at = ?, pid = ? WHERE id = ?",
            (iso(), proc.pid, row["id"]),
        )
        self.db.commit()
        self.children[row["id"]] = Child(
            run_id=row["id"],
            automation_id=row["automation_id"],
            process=proc,
            log_handle=log_handle,
            log_path=log_path,
            timeout_seconds=item["timeout_seconds"],
            started_monotonic=time.monotonic(),
        )

    def _finish_without_child(
        self, row: dict[str, Any], status: str, exit_code: int | None, summary: str
    ) -> None:
        self.db.execute(
            "UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, summary = ?, "
            "pid = NULL WHERE id = ?",
            (status, iso(), exit_code, summary, row["id"]),
        )
        self.db.commit()
        self._maybe_retry(row["id"])

    def reap(self) -> None:
        now_mono = time.monotonic()
        for run_id, child in list(self.children.items()):
            row = self.db.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
            cancel = bool(row and row[0])
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
            self.db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, summary = ?, "
                "pid = NULL WHERE id = ?",
                (status, iso(), code, summary, run_id),
            )
            self.db.commit()
            del self.children[run_id]
            if status == "failed":
                self._maybe_retry(run_id)

    def _maybe_retry(self, run_id: str) -> None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return
        item = self.by_id.get(row["automation_id"])
        if item is None or int(row["attempt"]) > item["retries"]:
            return
        enqueue(
            self.db,
            self.config,
            self.state_dir,
            row["automation_id"],
            trigger="retry",
            attempt=int(row["attempt"]) + 1,
            parent_run_id=row["parent_run_id"] or row["id"],
        )

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
                self.db.execute(
                    "UPDATE runs SET status = 'interrupted', finished_at = ?, exit_code = ?, "
                    "summary = 'daemon stopped', pid = NULL WHERE id = ?",
                    (iso(), code, run_id),
                )
                self.db.commit()
                del self.children[run_id]
            time.sleep(0.05)
        for run_id, child in list(self.children.items()):
            _signal_group(child.process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                child.process.wait(timeout=2)
            child.log_handle.close()
            self.db.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, "
                "summary = 'daemon stopped', pid = NULL WHERE id = ?",
                (iso(), run_id),
            )
            self.db.commit()
            del self.children[run_id]

    def run(self) -> None:
        import fcntl

        lock_path = self.state_dir / "daemon.lock"
        pid_path = self.state_dir / "daemon.pid"
        lock = lock_path.open("a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another automations daemon holds {lock_path}") from exc
        pid_path.write_text(f"{os.getpid()}\n")
        self.recover()

        def stop(_signum: int, _frame: Any) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not self.stop_requested:
                self.reap()
                self.schedule_due(utc_now())
                self.dispatch()
                time.sleep(self.config["engine"]["tick_seconds"])
        finally:
            self.shutdown()
            pid_path.unlink(missing_ok=True)
            self.db.close()
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
