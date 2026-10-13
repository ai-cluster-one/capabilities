"""The agentrooms service engine: one process serving the agents of a set of projects.

The CLI owns everything a project is - its identity, its policy, its connections,
its settings - and hands this module a host that answers those questions inside
the project's own resolution. This module owns what the process does with the
answers: one `/sync` long-poll per agent, the wakes it records, the turns it runs
and the state it keeps in the project's store.

Project mode is the case of a set of one: `agentrooms service run` serves the
project it is started in, `agentrooms service run --machine` every project on the
machine's opt-in list, through the same code.

Runtime state lives in the PostgreSQL the project resolves to, under the
`agentrooms_` prefix and the `agentrooms` migration ledger: the sync position of
each agent, the wakes not yet answered, the harness session of each agent in each
room, the lease that keeps a second process off an agent, a project's pause, the
project's workers as versions, and the record of the turns run, which the limits
count and which names the message a turn posted and the harness session that
wrote it. No message text is kept: a turn reads the room live.

A worker admits and limits an agent's turns. Which worker answers an agent in a
room is read from the agent's connection at every turn, and the worker and its
profile from the store, so a saved worker or a new binding holds from the next
turn on.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from string import Template
from typing import Any, Callable

LEDGER_OWNER = "agentrooms"
LEDGER_MAJOR = 1
LEDGER_MINOR = 2
LEDGER_STEPS = [
    ("0001-sync", """
    CREATE TABLE agentrooms_sync (
        project_id  text NOT NULL,
        agent       text NOT NULL,
        user_id     text NOT NULL,
        next_batch  text,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, agent)
    )
    """),
    ("0002-wakes", """
    CREATE TABLE agentrooms_wakes (
        project_id   text NOT NULL,
        agent        text NOT NULL,
        room_id      text NOT NULL,
        wake_key     text NOT NULL,
        kind         text NOT NULL,
        event_id     text,
        sender       text,
        received_at  timestamptz NOT NULL DEFAULT now(),
        claimed_by   text,
        PRIMARY KEY (project_id, agent, room_id, wake_key)
    )
    """),
    ("0003-sessions", """
    CREATE TABLE agentrooms_sessions (
        project_id  text NOT NULL,
        agent       text NOT NULL,
        room_id     text NOT NULL,
        harness     text,
        session_id  text,
        node        text,
        seen_ts     bigint,
        turns       integer NOT NULL DEFAULT 0,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, agent, room_id)
    )
    """),
    ("0004-leases", """
    CREATE TABLE agentrooms_leases (
        project_id   text NOT NULL,
        agent        text NOT NULL,
        holder       text NOT NULL,
        node         text NOT NULL,
        pid          integer,
        acquired_at  timestamptz NOT NULL,
        expires_at   timestamptz NOT NULL,
        PRIMARY KEY (project_id, agent)
    )
    """),
    ("0005-pauses", """
    CREATE TABLE agentrooms_pauses (
        project_id  text PRIMARY KEY,
        paused_at   timestamptz NOT NULL,
        paused_by   text NOT NULL,
        reason      text
    )
    """),
    ("0006-workers", """
    CREATE TABLE agentrooms_workers (
        id          uuid        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
        project_id  text        NOT NULL,
        name        text        NOT NULL,
        spec        jsonb       NOT NULL,
        body        text        NOT NULL,
        active      boolean     NOT NULL DEFAULT true,
        created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
        created_by  text
    );
    CREATE UNIQUE INDEX agentrooms_workers_one_active_idx
        ON agentrooms_workers (project_id, name) WHERE active;
    CREATE INDEX agentrooms_workers_name_idx
        ON agentrooms_workers (project_id, name, created_at);
    CREATE FUNCTION agentrooms_workers_keep_history() RETURNS trigger AS $$
    BEGIN
      IF tg_op = 'UPDATE' AND (to_jsonb(new) - 'active') = (to_jsonb(old) - 'active') THEN
        RETURN new;
      END IF;
      RAISE EXCEPTION 'a worker version is never edited or deleted: % on agentrooms_workers is refused, and only active may move; save a new version instead', lower(tg_op)
        USING errcode = 'restrict_violation', constraint = 'agentrooms_workers_history';
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER agentrooms_workers_keep_history_row BEFORE UPDATE OR DELETE
        ON agentrooms_workers FOR EACH ROW EXECUTE FUNCTION agentrooms_workers_keep_history();
    CREATE TRIGGER agentrooms_workers_keep_history_truncate BEFORE TRUNCATE
        ON agentrooms_workers FOR EACH STATEMENT EXECUTE FUNCTION agentrooms_workers_keep_history();
    """),
    ("0007-turns", """
    CREATE TABLE agentrooms_turns (
        id          text        PRIMARY KEY,
        project_id  text        NOT NULL,
        agent       text        NOT NULL,
        room_id     text        NOT NULL,
        worker      text        NOT NULL,
        worker_id   uuid,
        senders     text[]      NOT NULL DEFAULT '{}',
        kinds       text[]      NOT NULL DEFAULT '{}',
        started_at  timestamptz NOT NULL DEFAULT now(),
        outcome     text
    );
    CREATE INDEX agentrooms_turns_agent_idx ON agentrooms_turns (project_id, agent, started_at);
    CREATE INDEX agentrooms_turns_room_idx
        ON agentrooms_turns (project_id, agent, room_id, started_at);
    """),
    ("0008-turn-sessions", """
    ALTER TABLE agentrooms_turns
        ADD COLUMN event_id    text,
        ADD COLUMN harness     text,
        ADD COLUMN session_id  text,
        ADD COLUMN node        text;
    CREATE INDEX agentrooms_turns_event_idx ON agentrooms_turns (project_id, event_id)
        WHERE event_id IS NOT NULL;
    """),
]

# How long a lease outlives its last renewal, and how often it is renewed. A
# process that dies leaves its agents to another process once this runs out.
LEASE_SECONDS = 90
RENEW_SECONDS = 30
# The `/sync` long-poll, in milliseconds, and the HTTP read bound around it.
SYNC_TIMEOUT_MS = 30000
# How long the process waits before asking again about a project it could not
# serve, or an agent whose lease another process holds.
ADMIT_SECONDS = 5.0
LEASE_RETRY_SECONDS = 10.0
# How often an agent's grant is asked again between turns.
CHECK_SECONDS = 30.0
# The longest the process goes without publishing its status.
PUBLISH_SECONDS = 15.0

# A turn's final answer that says the agent stays silent.
SILENT = "[silent]"
# How far a sync that brings an agent's join reads back for what arrived while
# the agent was only invited: pages of room messages, each of this many events.
INVITE_GAP_PAGES = 4
INVITE_GAP_EVENTS = 50
# The room account data an agent marks a room with, to be woken by every message.
MARK_TYPE = "agentrooms.marked"
# Where custom data rides in a message's content.
DATA_KEY = "agentrooms.data"
# Set in the environment of every turn the service starts, so what the agent
# runs knows it runs under a worker, and a worker's fence cannot be rewritten
# from inside the turn it fences.
TURN_ENV = "AGENTROOMS_TURN"

LOG_FILE = "daemon.log"
PID_FILE = "daemon.pid"
LOCK_FILE = "daemon.lock"
STATUS_FILE = "daemon.json"
MACHINE_TAG = "machine"

SYNC_FILTER = {
    "presence": {"not_types": ["*"]},
    "account_data": {"not_types": ["*"]},
    "room": {
        "timeline": {"limit": 50},
        "ephemeral": {"not_types": ["*"]},
        "account_data": {"types": [MARK_TYPE]},
    },
}

# ── workers ──────────────────────────────────────────────────────────────────
#
# A worker is a Markdown document: YAML front matter is its settings, the body
# its role. The shape is this capability's contract (`agentrooms help` WORKERS)
# and the content is the project's; a key the contract does not name is refused.

WORKER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
DEFAULT_WORKER = "default"
DEFAULT_WORKER_PATH = Path(__file__).resolve().parent / "workers" / "default.md"
WAKE_KINDS = ("mention", "direct", "invite", "marked")
# The kinds the default worker can answer: a signal addressed to the agent. A
# message in a room the agent marked is addressed to nobody.
ADDRESSED_KINDS = ("mention", "direct", "invite")
WORKER_KEYS = ("enabled", "description", "profile", "history", "accepts_from",
               "invites_from", "wakes_on", "limits", "hooks")
DEFAULT_KEYS = ("description", "limits")
DEFAULT_PROFILE = "agentrooms-claude"
DEFAULT_HISTORY = 30
# Each limit: its default, its least and its greatest value.
LIMITS = {
    "debounce_seconds": (0, 0, 3600),
    "turn_timeout": (900, 30, 86400),
    "window_seconds": (3600, 60, 7 * 86400),
    "turns_per_agent": (120, 1, 100000),
    "turns_per_sender": (30, 1, 100000),
    "turns_per_room": (30, 1, 100000),
    "quiet_seconds": (900, 1, 7 * 86400),
}
DEFAULT_LIMITS = {"window_seconds": (86400, 60, 30 * 86400)}
# A worker's hooks, the seconds each may take, and what each answers by its exit
# - the tasks hook contract: 0 go; 75 hold, until the moment on its last stdout
# line; 76 escalate, its last line the reason. Any other ending is a hold with
# no moment, which lasts HOLD_SECONDS.
HOOK_KEYS = ("before", "after")
HOOK_TIMEOUTS = {"before": 30, "after": 60}
HOOK_HOLD = 75
HOOK_ESCALATE = 76
HOLD_SECONDS = 60
# What the default worker's answer carries in `agentrooms.data`, and what the
# service never wakes on, so two agents' default workers never answer each other.
DEFAULT_MARK = {"agentrooms.worker": DEFAULT_WORKER}


class WorkerInvalid(Exception):
    """A worker document the contract refuses; the message names every problem."""


def split_document(text: str, where: str) -> tuple[dict, str]:
    """A worker document's front matter and body. Raises WorkerInvalid."""
    import yaml
    if not text.startswith("---"):
        raise WorkerInvalid(f"{where}: a worker opens with YAML front matter between `---` lines")
    parts = text.split("\n")
    end = next((i for i, line in enumerate(parts[1:], 1) if line.strip() == "---"), None)
    if parts[0].strip() != "---" or end is None:
        raise WorkerInvalid(f"{where}: its front matter is not closed by a `---` line")
    try:
        spec = yaml.safe_load("\n".join(parts[1:end])) or {}
    except yaml.YAMLError as exc:
        raise WorkerInvalid(f"{where}: its front matter is not YAML: {exc}") from None
    if not isinstance(spec, dict):
        raise WorkerInvalid(f"{where}: its front matter is a mapping of settings")
    return json.loads(json.dumps(spec, default=str)), "\n".join(parts[end + 1:]).strip()


def worker_text(spec: dict, body: str) -> str:
    """A stored version as the document it was saved from."""
    import yaml
    head = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).strip() if spec else ""
    return f"---\n{head}\n---\n" + (f"\n{body.strip()}\n" if body.strip() else "")


def _patterns(value, key: str, wrong: Callable[[str], None]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        wrong(f"`{key}` is a list of Matrix user ids or patterns, such as `@*:example.org` or `*`")
        return []
    return [v.strip() for v in value]


def _limits(raw, table: dict, wrong: Callable[[str], None]) -> dict:
    out = {key: spec[0] for key, spec in table.items()}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        wrong("`limits` is a mapping")
        return out
    for key in sorted(set(map(str, raw)) - set(table)):
        wrong(f"`limits` names {key!r}, which nothing reads: the limits are {', '.join(table)}")
    for key, (_default, low, high) in table.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            wrong(f"`limits.{key}` is whole seconds or a whole number, {low}-{high}")
            continue
        out[key] = value
    return out


def read_worker(name: str, spec: dict, body: str, where: str) -> dict:
    """A worker's settings, whole and checked, with every default filled in.
    Raises WorkerInvalid naming every problem at once."""
    problems: list[str] = []
    wrong = problems.append
    if not isinstance(spec, dict):
        raise WorkerInvalid(f"{where}: its front matter is a mapping of settings")
    for key in sorted(set(map(str, spec)) - set(WORKER_KEYS)):
        wrong(f"it names {key!r}, which nothing reads: the keys are {', '.join(WORKER_KEYS)}")
    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        wrong("`enabled` is true or false")
    description = spec.get("description")
    if description is not None and not isinstance(description, str):
        wrong("`description` is one sentence")
    profile = spec.get("profile", DEFAULT_PROFILE)
    if not isinstance(profile, str) or not profile.strip():
        wrong("`profile` names a harness profile")
    history = spec.get("history", DEFAULT_HISTORY)
    if isinstance(history, bool) or not isinstance(history, int) or not 1 <= history <= 100:
        wrong("`history` is a whole number of messages, 1-100")
    accepts = _patterns(spec.get("accepts_from", []), "accepts_from", wrong)
    invites = (_patterns(spec["invites_from"], "invites_from", wrong)
               if "invites_from" in spec else list(accepts))
    wakes_on = spec.get("wakes_on", list(WAKE_KINDS))
    if (not isinstance(wakes_on, list) or not wakes_on
            or not all(isinstance(k, str) and k in WAKE_KINDS for k in wakes_on)):
        wrong(f"`wakes_on` lists one or more of {', '.join(WAKE_KINDS)}")
        wakes_on = []
    limits = _limits(spec.get("limits"), LIMITS, wrong)
    hooks: dict = {}
    raw_hooks = spec.get("hooks")
    if raw_hooks is not None:
        if not isinstance(raw_hooks, dict):
            wrong("`hooks` is a mapping of before and after, each one command line")
        else:
            for key in sorted(set(map(str, raw_hooks)) - set(HOOK_KEYS)):
                wrong(f"`hooks` names {key!r}, which nothing reads: the hooks are "
                      f"{', '.join(HOOK_KEYS)}")
            for which in HOOK_KEYS:
                command = raw_hooks.get(which)
                if command is None:
                    continue
                try:
                    argv = shlex.split(command) if isinstance(command, str) else []
                except ValueError as exc:
                    wrong(f"`hooks.{which}` does not split into a command: {exc}")
                    continue
                if not argv:
                    wrong(f"`hooks.{which}` is one command line, run without a shell from "
                          "the project root")
                    continue
                hooks[which] = command.strip()
    if enabled is True and not accepts and not problems:
        wrong("it is enabled and admits nobody: `accepts_from` names who may wake the agent")
    if problems:
        raise WorkerInvalid(f"{where}: " + "; ".join(problems))
    return {"name": name, "enabled": enabled, "description": (description or "").strip(),
            "profile": profile.strip(), "history": history, "accepts_from": accepts,
            "invites_from": invites, "wakes_on": wakes_on, "limits": limits, "hooks": hooks,
            "body": (body or "").strip()}


def read_default(spec: dict, body: str, where: str) -> dict:
    """The shipped default worker: its window and its answer. It runs no turn, so
    it carries no profile, no role and no hooks."""
    problems: list[str] = []
    wrong = problems.append
    for key in sorted(set(map(str, spec)) - set(DEFAULT_KEYS)):
        wrong(f"it names {key!r}, which the default worker does not read")
    limits = _limits(spec.get("limits"), DEFAULT_LIMITS, wrong)
    if not (body or "").strip():
        wrong("its body is the answer it posts, and it is empty")
    if problems:
        raise WorkerInvalid(f"{where}: " + "; ".join(problems))
    return {"name": DEFAULT_WORKER, "description": str(spec.get("description") or "").strip(),
            "limits": limits, "body": body.strip(), "wakes_on": list(ADDRESSED_KINDS)}


def load_default() -> dict:
    spec, body = split_document(DEFAULT_WORKER_PATH.read_text(), str(DEFAULT_WORKER_PATH))
    return read_default(spec, body, str(DEFAULT_WORKER_PATH))


def admits(patterns: list[str], sender: str | None) -> bool:
    return bool(sender) and any(fnmatch.fnmatchcase(sender, p) for p in patterns)


def is_default_answer(content: dict) -> bool:
    data = content.get(DATA_KEY)
    return isinstance(data, dict) and all(data.get(k) == v for k, v in DEFAULT_MARK.items())


def default_answer(template: str, *, user_id: str, sender: str | None,
                   supervisor: str | None) -> str:
    return Template(template).safe_substitute(
        user_id=user_id, sender=sender or "someone",
        supervisor=f"its supervisor, {supervisor}," if supervisor
        else "the person who runs it").strip()


# ── hooks ────────────────────────────────────────────────────────────────────


def _last_line(text: str | None) -> str | None:
    lines = [one.strip() for one in (text or "").splitlines() if one.strip()]
    return lines[-1][:500] if lines else None


def run_hook(command: str, *, cwd: str, env: dict, payload: dict, seconds: int) -> dict:
    """Run one hook command line without a shell, from `cwd`, with `payload` as
    JSON on stdin: its exit, or why it has none, and the last line it printed. A
    hook that outlives its seconds is ended with everything it started."""
    try:
        child = subprocess.Popen(shlex.split(command), cwd=cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
    except (OSError, ValueError) as exc:
        return {"exit": None, "why": f"could not start: {exc}", "said": None}
    try:
        out, _err = child.communicate(json.dumps(payload, ensure_ascii=False) + "\n",
                                      timeout=seconds)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(child.pid, signal.SIGKILL)
        try:
            out, _err = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = None
        return {"exit": None, "why": f"timed out after {seconds}s", "said": _last_line(out)}
    code = child.returncode
    return {"exit": code, "why": None if code == 0 else f"exited {code}", "said": _last_line(out)}


def hook_moment(text: str | None) -> _dt.datetime | None:
    """The moment a holding hook named on its last line: ISO 8601 with its
    offset, or a bare date, read as midnight UTC."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            day = _dt.date.fromisoformat(text)
            return _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
        stamp = _dt.datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def verdict(ran: dict) -> dict:
    """What a hook's ending says: `go`; `hold`, with `until` when its last line
    names a moment; or `escalate`, its last line the reason."""
    said = ran["said"]
    if ran["exit"] == 0:
        return {"verdict": "go", "said": said}
    if ran["exit"] == HOOK_ESCALATE:
        return {"verdict": "escalate", "said": said,
                "why": said or f"its hook exited {HOOK_ESCALATE} and said nothing"}
    if ran["exit"] == HOOK_HOLD:
        moment = hook_moment(said)
        if moment:
            return {"verdict": "hold", "until": moment, "said": said}
        return {"verdict": "hold", "said": said,
                "why": f"exited {HOOK_HOLD} without a moment on its last line"}
    return {"verdict": "hold", "said": said, "why": ran["why"]}


class StoreUnavailable(Exception):
    """The store cannot be used: none configured, unreadable, unreachable, or it
    refuses the migration. `slug` is the shared library's word for the cause."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug, self.message, self.hint = slug, message, hint


def database():
    try:
        from capabilities_contract import db
    except ImportError as exc:  # pragma: no cover - the header pins it
        raise StoreUnavailable("driver_missing", "the shared database library is not installed",
                               "run agentrooms through its own script header, which pins it") from exc
    return db


def resolve_store(root: Path | None):
    """The store setting a process standing in `root` uses, through the shared
    library's cascade. Read only; raises StoreUnavailable when none answers."""
    db = database()
    try:
        return db.resolve_setting(root)
    except db.DbError as exc:
        raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def ts_iso(ms: int | None) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).isoformat(timespec="seconds")


# ── the store ────────────────────────────────────────────────────────────────


class Store:
    """One held connection to a project's store, migrated on open. Every query
    names the project it is about; the connection is shared by the process's
    threads, one statement or transaction at a time."""

    def __init__(self, setting):
        self.setting = setting
        self.conn = None
        self.lock = threading.RLock()

    def open(self) -> "Store":
        db = database()
        try:
            conn = db.connect(application_name=LEDGER_OWNER, setting=self.setting)
        except db.DbError as exc:
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
        try:
            db.migrate(conn, LEDGER_OWNER, LEDGER_STEPS, major=LEDGER_MAJOR, minor=LEDGER_MINOR)
            conn.autocommit = True
        except db.DbError as exc:
            conn.close()
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
        self.conn = conn
        return self

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                with contextlib.suppress(Exception):
                    self.conn.close()
                self.conn = None

    def _reopen_if_lost(self) -> None:
        if self.conn is None or self.conn.closed or self.conn.broken:
            self.close()
            self.open()

    def execute(self, sql: str, params=(), *, fetch: str | None = None):
        with self.lock:
            self._reopen_if_lost()
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return cur.rowcount

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self._reopen_if_lost()
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    yield cur

    # -- leases ---------------------------------------------------------------

    def acquire(self, project: str, agent: str, holder: str, node: str, pid: int) -> dict | None:
        """Take or keep the agent's lease. None when it is ours now; else who
        holds it and until when."""
        row = self.execute(
            """
            INSERT INTO agentrooms_leases (project_id, agent, holder, node, pid, acquired_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, now(), now() + make_interval(secs => %s))
            ON CONFLICT (project_id, agent) DO UPDATE
               SET holder = EXCLUDED.holder, node = EXCLUDED.node, pid = EXCLUDED.pid,
                   acquired_at = CASE WHEN agentrooms_leases.holder = EXCLUDED.holder
                                      THEN agentrooms_leases.acquired_at ELSE now() END,
                   expires_at = EXCLUDED.expires_at
             WHERE agentrooms_leases.expires_at < now() OR agentrooms_leases.holder = EXCLUDED.holder
            RETURNING holder
            """, (project, agent, holder, node, pid, LEASE_SECONDS), fetch="one")
        if row is not None:
            return None
        found = self.execute(
            "SELECT holder, node, pid, expires_at FROM agentrooms_leases "
            "WHERE project_id = %s AND agent = %s", (project, agent), fetch="one")
        if found is None:
            return {"holder": "unknown"}
        return {"holder": found[0], "node": found[1], "pid": found[2],
                "expires_at": found[3].isoformat() if found[3] else None}

    def release(self, project: str, agent: str, holder: str) -> None:
        self.execute("DELETE FROM agentrooms_leases WHERE project_id = %s AND agent = %s "
                     "AND holder = %s", (project, agent, holder))

    def holds(self, cur, project: str, agent: str, holder: str) -> bool:
        cur.execute("SELECT 1 FROM agentrooms_leases WHERE project_id = %s AND agent = %s "
                    "AND holder = %s AND expires_at > now() FOR UPDATE",
                    (project, agent, holder))
        return cur.fetchone() is not None

    # -- sync position and wakes ----------------------------------------------

    def next_batch(self, project: str, agent: str, user_id: str) -> str | None:
        """The sync position, or None for an agent seen first or now under
        another identity."""
        row = self.execute("SELECT user_id, next_batch FROM agentrooms_sync "
                           "WHERE project_id = %s AND agent = %s", (project, agent), fetch="one")
        if row is None or row[0] != user_id:
            return None
        return row[1]

    def save_sync(self, project: str, agent: str, user_id: str, holder: str,
                  next_batch: str, wakes: list[dict]) -> bool:
        """Record the wakes a sync brought and the position after it, in one
        transaction, only while this process still holds the agent."""
        with self.transaction() as cur:
            if not self.holds(cur, project, agent, holder):
                return False
            for wake in wakes:
                cur.execute(
                    "INSERT INTO agentrooms_wakes (project_id, agent, room_id, wake_key, kind, "
                    "event_id, sender) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (project, agent, wake["room_id"], wake["key"], wake["kind"],
                     wake.get("event_id"), wake.get("sender")))
            cur.execute(
                "INSERT INTO agentrooms_sync (project_id, agent, user_id, next_batch, updated_at) "
                "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (project_id, agent) DO UPDATE "
                "SET user_id = EXCLUDED.user_id, next_batch = EXCLUDED.next_batch, "
                "updated_at = now()", (project, agent, user_id, next_batch))
        return True

    def unclaim(self, project: str, agent: str) -> int:
        """Hand back what a process that held the agent before claimed and never
        finished; called by the new holder, which alone may claim."""
        return self.execute("UPDATE agentrooms_wakes SET claimed_by = NULL WHERE project_id = %s "
                            "AND agent = %s AND claimed_by IS NOT NULL", (project, agent))

    def ready(self, project: str) -> list[tuple[str, str, Any]]:
        return [tuple(r) for r in self.execute(
            "SELECT agent, room_id, min(received_at) FROM agentrooms_wakes "
            "WHERE project_id = %s AND claimed_by IS NULL GROUP BY agent, room_id "
            "ORDER BY 3", (project,), fetch="all")]

    def claim(self, project: str, agent: str, room_id: str, turn: str) -> list[dict]:
        rows = self.execute(
            "UPDATE agentrooms_wakes SET claimed_by = %s WHERE project_id = %s AND agent = %s "
            "AND room_id = %s AND claimed_by IS NULL "
            "RETURNING wake_key, kind, event_id, sender, received_at",
            (turn, project, agent, room_id), fetch="all")
        out = [{"key": r[0], "kind": r[1], "event_id": r[2], "sender": r[3],
                "received_at": r[4]} for r in rows]
        return sorted(out, key=lambda w: w["received_at"])

    def finish(self, project: str, agent: str, room_id: str, turn: str) -> None:
        self.execute("DELETE FROM agentrooms_wakes WHERE project_id = %s AND agent = %s "
                     "AND room_id = %s AND claimed_by = %s", (project, agent, room_id, turn))

    def restore(self, project: str, agent: str, room_id: str, turn: str,
                keys: list[str] | None = None) -> None:
        """Hand claimed wakes back for a later turn: all the turn claimed, or the
        ones `keys` names."""
        if keys is None:
            self.execute("UPDATE agentrooms_wakes SET claimed_by = NULL WHERE project_id = %s "
                         "AND agent = %s AND room_id = %s AND claimed_by = %s",
                         (project, agent, room_id, turn))
            return
        self.execute("UPDATE agentrooms_wakes SET claimed_by = NULL WHERE project_id = %s "
                     "AND agent = %s AND room_id = %s AND claimed_by = %s "
                     "AND wake_key = ANY(%s)", (project, agent, room_id, turn, list(keys)))

    # -- sessions -------------------------------------------------------------

    def sessions(self, project: str, agent: str, room_id: str | None = None) -> list[dict]:
        """The room-to-session map of `agent`, every room or the one named."""
        rows = self.execute(
            "SELECT room_id, harness, session_id, node, seen_ts, turns, updated_at "
            "FROM agentrooms_sessions WHERE project_id = %s AND agent = %s "
            "AND (%s::text IS NULL OR room_id = %s) ORDER BY room_id",
            (project, agent, room_id, room_id), fetch="all")
        return [{"room_id": r[0], "harness": r[1], "session_id": r[2], "node": r[3],
                 "seen_ts": r[4], "turns": r[5],
                 "updated_at": r[6].isoformat() if r[6] else None} for r in rows]

    def session(self, project: str, agent: str, room_id: str) -> dict | None:
        row = self.execute(
            "SELECT harness, session_id, node, seen_ts, turns FROM agentrooms_sessions "
            "WHERE project_id = %s AND agent = %s AND room_id = %s",
            (project, agent, room_id), fetch="one")
        if row is None:
            return None
        return {"harness": row[0], "session_id": row[1], "node": row[2],
                "seen_ts": row[3], "turns": row[4]}

    def save_session(self, project: str, agent: str, room_id: str, *, harness: str | None,
                     session_id: str | None, node: str, seen_ts: int | None) -> None:
        self.execute(
            "INSERT INTO agentrooms_sessions (project_id, agent, room_id, harness, session_id, "
            "node, seen_ts, turns, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, now()) "
            "ON CONFLICT (project_id, agent, room_id) DO UPDATE SET "
            "harness = EXCLUDED.harness, session_id = EXCLUDED.session_id, node = EXCLUDED.node, "
            "seen_ts = GREATEST(COALESCE(agentrooms_sessions.seen_ts, 0), "
            "COALESCE(EXCLUDED.seen_ts, 0)), turns = agentrooms_sessions.turns + 1, "
            "updated_at = now()",
            (project, agent, room_id, harness, session_id, node, seen_ts))

    def counts(self, project: str) -> dict:
        rows = self.execute(
            "SELECT agent, count(*), sum(turns) FROM agentrooms_sessions WHERE project_id = %s "
            "GROUP BY agent", (project,), fetch="all")
        return {r[0]: {"rooms": int(r[1]), "turns": int(r[2] or 0)} for r in rows}

    # -- pause ----------------------------------------------------------------

    def pause_of(self, project: str) -> dict | None:
        row = self.execute("SELECT paused_at, paused_by, reason FROM agentrooms_pauses "
                           "WHERE project_id = %s", (project,), fetch="one")
        if row is None:
            return None
        return {"at": row[0].isoformat() if row[0] else None, "by": row[1], "reason": row[2]}

    def pause(self, project: str, by: str, reason: str | None) -> None:
        self.execute("INSERT INTO agentrooms_pauses (project_id, paused_at, paused_by, reason) "
                     "VALUES (%s, now(), %s, %s) ON CONFLICT (project_id) DO UPDATE SET "
                     "paused_at = now(), paused_by = EXCLUDED.paused_by, reason = EXCLUDED.reason",
                     (project, by, reason))

    def resume(self, project: str) -> bool:
        return bool(self.execute("DELETE FROM agentrooms_pauses WHERE project_id = %s", (project,)))

    # -- workers --------------------------------------------------------------
    #
    # One row is one version of a worker. A save never edits a row: it inserts
    # a new version and clears `active` on the version the writer read, in one
    # transaction, so two writers never both replace the same version.

    WORKER_COLUMNS = "id, name, spec, body, active, created_at, created_by"

    @staticmethod
    def _worker_row(row) -> dict:
        spec = row[2] if isinstance(row[2], dict) else json.loads(row[2])
        return {"id": str(row[0]), "name": row[1], "spec": spec, "body": row[3] or "",
                "active": row[4], "created_at": row[5].isoformat() if row[5] else None,
                "created_by": row[6]}

    def worker(self, project: str, name: str, version: str | None = None) -> dict | None:
        """The worker's active version, or the version named."""
        if version is None:
            row = self.execute(f"SELECT {self.WORKER_COLUMNS} FROM agentrooms_workers "
                               "WHERE project_id = %s AND name = %s AND active",
                               (project, name), fetch="one")
        else:
            row = self.execute(f"SELECT {self.WORKER_COLUMNS} FROM agentrooms_workers "
                               "WHERE project_id = %s AND name = %s AND id::text = %s",
                               (project, name, version), fetch="one")
        return self._worker_row(row) if row else None

    def workers(self, project: str) -> list[dict]:
        rows = self.execute(f"SELECT {self.WORKER_COLUMNS} FROM agentrooms_workers "
                            "WHERE project_id = %s AND active ORDER BY name", (project,),
                            fetch="all")
        return [self._worker_row(r) for r in rows]

    def worker_history(self, project: str, name: str) -> list[dict]:
        rows = self.execute(f"SELECT {self.WORKER_COLUMNS} FROM agentrooms_workers "
                            "WHERE project_id = %s AND name = %s ORDER BY created_at, id",
                            (project, name), fetch="all")
        return [self._worker_row(r) for r in rows]

    def save_worker(self, project: str, name: str, spec: dict, body: str, *,
                    base: str | None, by: str) -> tuple[dict, bool]:
        """Save a version and say whether one was created: a save identical to
        the active version creates none. `base` is the version the writer read;
        a save over any other is refused with WorkerConflict."""
        with self.transaction() as cur:
            cur.execute(f"SELECT {self.WORKER_COLUMNS} FROM agentrooms_workers "
                        "WHERE project_id = %s AND name = %s AND active FOR UPDATE",
                        (project, name))
            found = cur.fetchone()
            active = self._worker_row(found) if found else None
            if base is not None and (active is None or active["id"] != base):
                raise WorkerConflict(
                    f"worker {name!r} is no longer at version {base}: "
                    + (f"{active['id']} is active" if active else "it has no active version"))
            if active and active["spec"] == spec and active["body"] == body:
                return active, False
            if active:
                cur.execute("UPDATE agentrooms_workers SET active = false WHERE id = %s AND active",
                            (active["id"],))
                if cur.rowcount != 1:
                    raise WorkerConflict(f"worker {name!r} was saved by someone else first")
            cur.execute(f"INSERT INTO agentrooms_workers (project_id, name, spec, body, active, "
                        f"created_by) VALUES (%s, %s, %s::jsonb, %s, true, %s) "
                        f"RETURNING {self.WORKER_COLUMNS}",
                        (project, name, json.dumps(spec), body, by))
            return self._worker_row(cur.fetchone()), True

    def remove_worker(self, project: str, name: str, *, base: str | None) -> str | None:
        """Clear the active version, leaving every version as history. None when
        the worker has no active version."""
        with self.transaction() as cur:
            cur.execute("SELECT id FROM agentrooms_workers WHERE project_id = %s AND name = %s "
                        "AND active FOR UPDATE", (project, name))
            found = cur.fetchone()
            if found is None:
                return None
            if base is not None and str(found[0]) != base:
                raise WorkerConflict(f"worker {name!r} is no longer at version {base}")
            cur.execute("UPDATE agentrooms_workers SET active = false WHERE id = %s", (found[0],))
            return str(found[0])

    # -- turns ----------------------------------------------------------------

    def start_turn(self, turn: str, project: str, agent: str, room_id: str, worker: str,
                   worker_id: str | None, senders: list[str], kinds: list[str]) -> None:
        self.execute("INSERT INTO agentrooms_turns (id, project_id, agent, room_id, worker, "
                     "worker_id, senders, kinds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                     "ON CONFLICT (id) DO NOTHING",
                     (turn, project, agent, room_id, worker, worker_id, senders, kinds))

    def end_turn(self, turn: str, outcome: str, *, event_id: str | None = None,
                 harness: str | None = None, session_id: str | None = None,
                 node: str | None = None) -> None:
        """How the turn ended, and the message it posted and the harness session
        that wrote it, where there are."""
        self.execute("UPDATE agentrooms_turns SET outcome = %s, event_id = %s, harness = %s, "
                     "session_id = %s, node = %s WHERE id = %s",
                     (outcome, event_id, harness, session_id, node, turn))

    def answered_by(self, project: str, agent: str, event_id: str) -> dict | None:
        """The turn of `agent` that posted `event_id`, with the session it ran in."""
        row = self.execute(
            "SELECT id, room_id, worker, harness, session_id, node, started_at, outcome "
            "FROM agentrooms_turns WHERE project_id = %s AND agent = %s AND event_id = %s "
            "ORDER BY started_at DESC LIMIT 1", (project, agent, event_id), fetch="one")
        if row is None:
            return None
        return {"turn": row[0], "room_id": row[1], "worker": row[2], "harness": row[3],
                "session_id": row[4], "node": row[5],
                "started_at": row[6].isoformat() if row[6] else None, "outcome": row[7]}

    def turn_counts(self, project: str, agent: str, room_id: str, senders: list[str], *,
                    window: int, quiet: int, room_cap: int) -> dict:
        """What the limits count, over the turns a worker ran (never the default
        worker's answers): the agent's turns and each sender's in the window, and
        the room's since it was last quiet for `quiet` seconds - at most
        `room_cap`, since that is all the room limit asks."""
        agent_n = self.execute(
            "SELECT count(*) FROM agentrooms_turns WHERE project_id = %s AND agent = %s "
            "AND worker <> %s AND started_at > now() - make_interval(secs => %s)",
            (project, agent, DEFAULT_WORKER, window), fetch="one")[0]
        per_sender = {}
        for sender in senders:
            per_sender[sender] = self.execute(
                "SELECT count(*) FROM agentrooms_turns WHERE project_id = %s AND agent = %s "
                "AND worker <> %s AND %s = ANY(senders) "
                "AND started_at > now() - make_interval(secs => %s)",
                (project, agent, DEFAULT_WORKER, sender, window), fetch="one")[0]
        rows = self.execute(
            "SELECT extract(epoch FROM now() - started_at) FROM agentrooms_turns "
            "WHERE project_id = %s AND agent = %s AND room_id = %s AND worker <> %s "
            "ORDER BY started_at DESC LIMIT %s",
            (project, agent, room_id, DEFAULT_WORKER, room_cap), fetch="all")
        room_n, since = 0, 0.0
        for (age,) in rows:
            if float(age) - since >= quiet:
                break
            room_n, since = room_n + 1, float(age)
        return {"agent": int(agent_n), "room": room_n,
                "senders": {s: int(n) for s, n in per_sender.items()}}

    def default_answered(self, project: str, agent: str, room_id: str, sender: str,
                         window: int) -> bool:
        return self.execute(
            "SELECT 1 FROM agentrooms_turns WHERE project_id = %s AND agent = %s "
            "AND room_id = %s AND worker = %s AND %s = ANY(senders) "
            "AND started_at > now() - make_interval(secs => %s) LIMIT 1",
            (project, agent, room_id, DEFAULT_WORKER, sender, window), fetch="one") is not None

    def stored_profiles(self, library, project: str) -> list:
        """Every active harness profile a lookup from the project reaches, read
        from the table the harness library owns. A store whose tables hold no
        profiles yet holds none."""
        with self.lock:
            self._reopen_if_lost()
            try:
                return library.stored_profiles(self.conn, project=project,
                                               schema=self.setting.schema)
            except Exception as exc:
                if type(exc).__name__ == "UndefinedTable":
                    return []
                raise


class WorkerConflict(Exception):
    """Another save came first: the version the writer read is no longer active."""


# ── reading a sync ───────────────────────────────────────────────────────────


class RoomView:
    """What one agent knows of one room from its syncs: enough to tell a direct
    message, an encrypted room and a marked room apart. Nothing a member said."""

    __slots__ = ("members", "encrypted", "marked", "name", "joined")

    def __init__(self):
        self.members: int | None = None
        self.encrypted = False
        self.marked = False
        self.name: str | None = None
        self.joined = True


def mentions(content: dict, user_id: str) -> bool:
    """Whether a message addresses `user_id`: by the mentions block a client
    sends, or by the user id written into the text."""
    block = content.get("m.mentions")
    if isinstance(block, dict) and user_id in (block.get("user_ids") or []):
        return True
    for field in ("body", "formatted_body"):
        text = content.get(field)
        if isinstance(text, str) and user_id in text:
            return True
    return False


def thread_root(content: dict) -> str | None:
    rel = content.get("m.relates_to")
    if isinstance(rel, dict) and rel.get("rel_type") == "m.thread":
        return rel.get("event_id")
    return None


def reply_to(content: dict) -> str | None:
    rel = content.get("m.relates_to")
    if isinstance(rel, dict):
        inner = rel.get("m.in_reply_to")
        if isinstance(inner, dict) and not rel.get("is_falling_back"):
            return inner.get("event_id")
    return None


def own_membership(event: dict, user_id: str) -> str | None:
    if event.get("type") == "m.room.member" and event.get("state_key") == user_id:
        return (event.get("content") or {}).get("membership")
    return None


def joined_now(timeline: list[dict], user_id: str) -> bool:
    """Whether a timeline holds the agent's own join, as opposed to a profile
    change of an agent that was already a member."""
    for event in timeline:
        if own_membership(event, user_id) == "join":
            prev = ((event.get("unsigned") or {}).get("prev_content") or {}).get("membership")
            if prev != "join":
                return True
    return False


def since_invite(page: Callable[[str], dict], token: str, user_id: str, *,
                 pages: int = INVITE_GAP_PAGES) -> list[dict] | None:
    """Reading back from `token` (a joined timeline's start), the events that
    arrived after the agent's invite, oldest first. None when the invite is not
    reached: another membership of the agent's own comes first, the room's
    start is reached, or the pages run out."""
    collected: list[dict] = []
    for _ in range(pages):
        found = page(token) or {}
        chunk = found.get("chunk") or []
        for event in chunk:  # newest first
            membership = own_membership(event, user_id)
            if membership == "invite":
                return list(reversed(collected))
            if membership is not None:
                return None
            collected.append(event)
        token = found.get("end")
        if not chunk or not token:
            return None
    return None


def joined_events(room_id: str, block: dict, user_id: str,
                  before_join: Callable[[str, str], list[dict] | None] | None,
                  notes: list[str]) -> list[dict]:
    """What a sync that brings the agent's join may wake on: the events after its
    invite. A homeserver may start the timeline at the join and leave what
    arrived while the agent was invited behind it; that is read back to the
    invite. Nothing older than the invite wakes."""
    timeline = block.get("events") or []
    cut = [i for i, e in enumerate(timeline) if own_membership(e, user_id) == "invite"]
    if cut:
        return timeline[cut[-1] + 1:]
    prev_batch = block.get("prev_batch")
    if not prev_batch or before_join is None:
        return timeline
    earlier = before_join(room_id, prev_batch)
    if earlier is None:
        notes.append(f"{room_id}: what arrived before the join wakes nothing; it could "
                     "not be read back to the invite")
        return timeline
    seen = {e.get("event_id") for e in timeline}
    return [e for e in earlier if e.get("event_id") not in seen] + timeline


def read_sync(response: dict, user_id: str, rooms: dict[str, RoomView], *,
              initial: bool, member_count: Callable[[str], int | None],
              before_join: Callable[[str, str], list[dict] | None] | None = None,
              ) -> tuple[list[dict], list[str]]:
    """The wakes one sync response brings for `user_id`, and notes about what it
    passed over. `rooms` is updated in place. An initial sync wakes only for the
    invites waiting; history before the agent was first served wakes nothing. A
    sync that brings the agent's join also wakes on what arrived between its
    invite and its join, read back through `before_join(room_id, prev_batch)`.
    A default worker's answer, by its mark in `agentrooms.data`, wakes nothing."""
    wakes: list[dict] = []
    notes: list[str] = []
    groups = response.get("rooms") or {}
    for room_id, room in (groups.get("join") or {}).items():
        view = rooms.setdefault(room_id, RoomView())
        view.joined = True
        summary = room.get("summary") or {}
        if isinstance(summary.get("m.joined_member_count"), int):
            view.members = summary["m.joined_member_count"]
        timeline = (room.get("timeline") or {}).get("events") or []
        for event in [*((room.get("state") or {}).get("events") or []), *timeline]:
            if "state_key" not in event:
                continue
            if event.get("type") == "m.room.encryption":
                view.encrypted = True
            elif event.get("type") == "m.room.name":
                view.name = (event.get("content") or {}).get("name") or view.name
        for event in (room.get("account_data") or {}).get("events") or []:
            if event.get("type") == MARK_TYPE:
                view.marked = (event.get("content") or {}).get("marked") is True
        if initial:
            continue
        events = timeline
        if not view.encrypted and joined_now(timeline, user_id):
            events = joined_events(room_id, room.get("timeline") or {}, user_id, before_join,
                                   notes)
        woken: set = set()
        for event in events:
            if event.get("type") != "m.room.message" or event.get("sender") == user_id:
                continue
            if event.get("event_id") in woken:
                continue
            if "state_key" in event:
                continue
            if view.encrypted:
                notes.append(f"{room_id}: a message in an encrypted room is passed over")
                continue
            content = event.get("content") or {}
            if is_default_answer(content):
                continue
            kind = None
            if mentions(content, user_id):
                kind = "mention"
            else:
                if view.members is None:
                    view.members = member_count(room_id)
                if view.members == 2:
                    kind = "direct"
                elif view.marked:
                    kind = "marked"
            if kind:
                woken.add(event.get("event_id"))
                wakes.append({"room_id": room_id, "key": event.get("event_id"), "kind": kind,
                              "event_id": event.get("event_id"), "sender": event.get("sender")})
    for room_id, room in (groups.get("invite") or {}).items():
        stripped = (room.get("invite_state") or {}).get("events") or []
        inviter = None
        encrypted = False
        for event in stripped:
            if event.get("type") == "m.room.member" and event.get("state_key") == user_id:
                inviter = event.get("sender")
            elif event.get("type") == "m.room.encryption":
                encrypted = True
        if encrypted:
            notes.append(f"{room_id}: an invite to an encrypted room is passed over")
            continue
        view = rooms.setdefault(room_id, RoomView())
        view.joined = False
        wakes.append({"room_id": room_id, "key": "invite", "kind": "invite",
                      "event_id": None, "sender": inviter})
    for room_id in (groups.get("leave") or {}):
        rooms.pop(room_id, None)
    return wakes, notes


# ── building a turn ──────────────────────────────────────────────────────────


def message_line(event: dict, wake_ids: set[str]) -> str:
    content = event.get("content") or {}
    when = ts_iso(event.get("origin_server_ts")) or "?"
    parts = [f"[{when}] {event.get('sender')}"]
    meta = [f"event {event.get('event_id')}"]
    root = thread_root(content)
    if root:
        meta.append(f"in thread {root}")
    replied = reply_to(content)
    if replied:
        meta.append(f"reply to {replied}")
    msgtype = content.get("msgtype")
    if msgtype and msgtype != "m.text":
        meta.append(msgtype)
    if content.get("url"):
        meta.append(f"file {content.get('url')}")
    if DATA_KEY in content:
        meta.append("data " + json.dumps(content[DATA_KEY], ensure_ascii=False)[:500])
    if event.get("event_id") in wake_ids:
        meta.append("WOKE YOU")
    body = str(content.get("body") or "")
    return f"{parts[0]} ({', '.join(meta)}):\n{body}"


def room_rules(template: str, *, agent: dict, room_id: str, room_name: str | None,
               role: str = "") -> str:
    """The room frame filled for this agent and room, with the worker's role
    after it: the turn's standing rules, handed to the harness as its
    system-prompt append rather than as a message."""
    frame = Template(template).safe_substitute(
        user_id=agent["user_id"], connection=agent["connection"], room_id=room_id,
        room_name=room_name or room_id, silent=SILENT).strip()
    role = (role or "").strip()
    return f"{frame}\n\n## Your role\n\n{role}" if role else frame


def with_rules(profile, rules: str):
    """The agent's profile with the room rules appended to its system prompt. A
    profile's own append_system_prompt comes first and is kept."""
    own = (getattr(profile, "append_system_prompt", None) or "").strip()
    return dataclasses.replace(profile, append_system_prompt="\n\n".join(
        part for part in (own, rules) if part))


def build_prompt(*, room_id: str, room_name: str | None, wakes: list[dict],
                 events: list[dict], primed: bool) -> str:
    """The turn's user prompt: what woke the agent and the room's messages. The
    room's rules travel in the system prompt (`room_rules`), not here."""
    kinds = sorted({w["kind"] for w in wakes})
    lines = ["=== This turn ===",
             f"Room: {room_id}" + (f" ({room_name})" if room_name else ""),
             f"Woken by: {', '.join(kinds)}"]
    for wake in wakes:
        if wake["kind"] == "invite":
            lines.append(f"You are invited to this room by {wake.get('sender') or 'someone'}. "
                         "You are not a member yet.")
    wake_ids = {w["event_id"] for w in wakes if w.get("event_id")}
    if events:
        lines += ["", "=== Room history ===" if primed
                  else "=== New in the room since your last turn ==="]
        for event in events:
            lines += [message_line(event, wake_ids), ""]
    elif primed and not any(w["kind"] == "invite" for w in wakes):
        lines += ["", "(No readable messages in the room.)"]
    return "\n".join(lines).rstrip() + "\n"


def missing_session(result) -> bool:
    """Whether a resumed turn failed because its session is not on this node."""
    if getattr(result, "ok", False):
        return False
    text = " ".join(str(x) for x in (
        getattr(getattr(result, "failure", None), "message", ""),
        getattr(result, "stderr_tail", ""))).lower()
    return any(word in text for word in ("no conversation found", "session not found",
                                         "no such session", "not found"))


# Endings after which the harness held a conversation worth resuming; any
# other failure (not authenticated, binary missing, a crash at start) never
# reached one, so its session id is not recorded.
RAN_FAILURES = ("timeout", "cancelled", "max_turns", "budget")


def harness_ran(result) -> bool:
    if getattr(result, "ok", False):
        return True
    kind = getattr(getattr(result, "failure", None), "kind", None)
    return str(getattr(kind, "value", kind)) in RAN_FAILURES


def cut_answer(answer: str) -> str:
    text = (answer or "").strip()
    if text == SILENT or text.endswith("\n" + SILENT) or not text:
        return ""
    return text


# ── the process ──────────────────────────────────────────────────────────────


class Agent:
    """One agent of one project: its identity, its sync loop and its lease."""

    def __init__(self, slot: "Slot", plan: dict):
        self.slot = slot
        self.plan = plan
        self.cid = plan["connection"]
        self.rooms: dict[str, RoomView] = {}
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "starting"
        self.reason: str | None = None
        self.lease = False
        self.last_sync: str | None = None
        self.last_check = 0.0
        self.last_renew = 0.0

    @property
    def engine(self) -> "Engine":
        return self.slot.engine

    def identity(self) -> tuple:
        return (self.plan["homeserver"], self.plan["user_id"], self.plan["token_fingerprint"])

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, name=f"sync-{self.slot.slug}-{self.cid}",
                                       daemon=True)
        self.thread.start()

    def halt(self) -> None:
        self.stop.set()

    def log(self, message: str) -> None:
        self.slot.log(f"{self.cid}: {message}")

    def row(self) -> dict:
        return {"connection": self.cid, "user_id": self.plan["user_id"], "state": self.state,
                "reason": self.reason, "lease": self.lease, "rooms_seen": len(self.rooms),
                "last_sync_at": self.last_sync}

    def take_lease(self) -> bool:
        store = self.slot.store
        held = store.acquire(self.slot.project_id, self.cid, self.engine.holder,
                             self.engine.node, os.getpid())
        if held is None:
            if not self.lease:
                handed = store.unclaim(self.slot.project_id, self.cid)
                self.log("lease taken" + (f", {handed} unfinished wake(s) handed back"
                                          if handed else ""))
                self.engine.poke()
            self.lease = True
            self.last_renew = time.monotonic()
            return True
        self.lease = False
        self.state = "waiting"
        self.reason = (f"another process holds this agent ({held.get('holder')}, until "
                       f"{held.get('expires_at')})")
        return False

    def loop(self) -> None:
        matrix = None
        backoff = 1.0
        while not self.stop.is_set():
            try:
                if self.slot.paused:
                    self.state, self.reason = "paused", "the project is paused"
                    self.stop.wait(2.0)
                    continue
                if not self.lease or time.monotonic() - self.last_renew > RENEW_SECONDS:
                    if not self.take_lease():
                        self.stop.wait(LEASE_RETRY_SECONDS)
                        continue
                if time.monotonic() - self.last_check > CHECK_SECONDS:
                    refusal = self.engine.host.check(self.slot.entry, self.cid)
                    self.last_check = time.monotonic()
                    if refusal:
                        self.state, self.reason = refusal
                        self.stop.wait(CHECK_SECONDS)
                        self.last_check = 0.0
                        continue
                if matrix is None:
                    matrix = self.engine.host.matrix(self.plan["homeserver"], self.plan["token"],
                                                     timeout=SYNC_TIMEOUT_MS / 1000 + 30)
                self.sync_once(matrix)
                self.state, self.reason = "served", None
                backoff = 1.0
            except StoreUnavailable as exc:
                self.state, self.reason = "error", f"{exc.slug}: {exc.message}"
                self.log(f"store: {exc.slug}: {exc.message}")
                self.stop.wait(min(backoff, 60.0))
                backoff *= 2
            except Exception as exc:  # one agent's failure is that agent's alone
                code = getattr(exc, "code", type(exc).__name__)
                self.state, self.reason = "error", f"{code}: {exc}"
                self.log(f"sync failed: {code}: {exc}")
                if getattr(exc, "exit_code", None) == 2:
                    self.stop.wait(60.0)
                else:
                    self.stop.wait(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
        if matrix is not None:
            with contextlib.suppress(Exception):
                matrix.close()
        if self.lease:
            with contextlib.suppress(Exception):
                self.slot.store.release(self.slot.project_id, self.cid, self.engine.holder)
            self.lease = False
        self.state = "stopped"

    def sync_once(self, matrix) -> None:
        store = self.slot.store
        since = store.next_batch(self.slot.project_id, self.cid, self.plan["user_id"])
        initial = since is None
        params = {"filter": json.dumps(SYNC_FILTER, separators=(",", ":")),
                  "timeout": "0" if initial else str(SYNC_TIMEOUT_MS)}
        if since:
            params["since"] = since
        response = matrix.call("GET", "/_matrix/client/v3/sync", params=params)

        def member_count(room_id: str) -> int | None:
            with contextlib.suppress(Exception):
                found = matrix.call("GET", f"/_matrix/client/v3/rooms/{quote(room_id)}/joined_members")
                return len((found or {}).get("joined") or {})
            return None

        def before_join(room_id: str, prev_batch: str) -> list[dict] | None:
            def page(token: str) -> dict:
                return matrix.call("GET", f"/_matrix/client/v3/rooms/{quote(room_id)}/messages",
                                   params={"dir": "b", "from": token,
                                           "limit": str(INVITE_GAP_EVENTS),
                                           "filter": json.dumps({"types": ["m.room.message",
                                                                           "m.room.member"]})})
            try:
                return since_invite(page, prev_batch, self.plan["user_id"])
            except Exception as exc:
                self.log(f"{room_id}: what arrived before the join is not read: "
                         f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
                return None

        wakes, notes = read_sync(response, self.plan["user_id"], self.rooms, initial=initial,
                                 member_count=member_count, before_join=before_join)
        for note in notes:
            self.log(note)
        next_batch = response.get("next_batch")
        if not next_batch:
            raise RuntimeError("the homeserver answered a sync without next_batch")
        if not store.save_sync(self.slot.project_id, self.cid, self.plan["user_id"],
                               self.engine.holder, next_batch, wakes):
            self.lease = False
            raise RuntimeError("the lease on this agent was lost before the sync was recorded")
        self.last_sync = now_iso()
        self.last_renew = time.monotonic()  # the transaction proved the lease is held
        store.execute("UPDATE agentrooms_leases SET expires_at = now() + make_interval(secs => %s) "
                      "WHERE project_id = %s AND agent = %s AND holder = %s",
                      (LEASE_SECONDS, self.slot.project_id, self.cid, self.engine.holder))
        if initial:
            self.log("first sync recorded; history before now wakes nothing")
        for wake in wakes:
            self.log(f"wake {wake['kind']} in {wake['room_id']} from {wake.get('sender')}")
        if wakes:
            self.engine.poke()


def quote(value: str) -> str:
    from urllib.parse import quote as _quote
    return _quote(value, safe="")


class Slot:
    """One project as the process sees it."""

    def __init__(self, engine: "Engine", project_id: str, entry: dict):
        self.engine = engine
        self.project_id = project_id
        self.entry = entry
        self.slug = entry.get("slug") or project_id
        self.plan: dict | None = None
        self.store: Store | None = None
        self.agents: dict[str, Agent] = {}
        self.state = "pending"
        self.reason: str | None = None
        self.reload_error: str | None = None
        self.paused = False
        self.pause: dict | None = None
        self.next_admit = 0.0
        self.last_pause_check = 0.0
        self.turns = 0

    def log(self, message: str) -> None:
        self.engine.log(self.slug, message)

    def row(self) -> dict:
        return {"project": self.slug, "project_id": self.project_id,
                "root": self.entry.get("root"), "state": self.state, "reason": self.reason,
                "fingerprint": (self.plan or {}).get("fingerprint"),
                "reload_error": self.reload_error, "paused": self.pause,
                "turns": self.turns, "running": self.engine.running_for(self.project_id),
                "agents": [a.row() for a in self.agents.values()]}

    def admit(self) -> None:
        plan, refusal = self.engine.host.admit(self.project_id, self.entry)
        if refusal:
            if (self.state, self.reason) != refusal:
                self.log(f"not served: {refusal[0]}: {refusal[1]}")
            self.state, self.reason = refusal
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        try:
            store = self.engine.store_for(plan["setting"])
        except StoreUnavailable as exc:
            self.state, self.reason = "refused", f"{exc.slug}: {exc.message}"
            self.log(f"not served: {exc.slug}: {exc.message}")
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        self.store = store
        self.adopt(plan)
        self.state, self.reason = "served", None
        self.log(f"served: {len(self.agents)} agent(s) on {plan['store_name']}")

    def adopt(self, plan: dict) -> None:
        """Take a plan: start agents it adds, stop those it drops, restart one
        whose identity changed. Turns already running finish as they started."""
        self.plan = plan
        wanted = plan["agents"]
        for cid in list(self.agents):
            agent = self.agents[cid]
            if cid not in wanted or wanted[cid]["identity"] != agent.identity():
                agent.halt()
                self.agents.pop(cid)
                self.log(f"{cid}: stopped ({'dropped' if cid not in wanted else 'identity changed'})")
        for cid, agent_plan in wanted.items():
            if cid in self.agents:
                self.agents[cid].plan = agent_plan
                continue
            agent = Agent(self, agent_plan)
            self.agents[cid] = agent
            agent.start()
            self.log(f"{cid}: serving {agent_plan['user_id']} on {agent_plan['homeserver']}")

    def reload(self) -> None:
        if self.plan is None:
            self.next_admit = 0.0
            return
        plan, refusal = self.engine.host.admit(self.project_id, self.entry)
        if refusal:
            self.reload_error = f"{refusal[0]}: {refusal[1]}"
            self.log(f"reload rejected, the previous declaration stays: {self.reload_error}")
            return
        if self.engine.store_key(plan["setting"]) != self.engine.store_key(self.plan["setting"]):
            self.reload_error = "the project now resolves another store; that takes a restart"
            self.log(f"reload rejected: {self.reload_error}")
            return
        self.reload_error = None
        self.adopt(plan)
        self.log(f"reloaded: fingerprint {plan['fingerprint']}")

    def check_pause(self) -> None:
        if self.store is None or time.monotonic() - self.last_pause_check < 2.0:
            return
        self.last_pause_check = time.monotonic()
        try:
            pause = self.store.pause_of(self.project_id)
        except Exception as exc:
            self.log(f"store: {exc}")
            return
        if bool(pause) != self.paused:
            self.log("paused" if pause else "resumed")
            if not pause:
                self.engine.poke()
        self.paused, self.pause = bool(pause), pause
        if self.state in ("served", "paused"):
            self.state = "paused" if pause else "served"
            self.reason = (pause or {}).get("reason") if pause else None

    def release(self, said: str) -> None:
        for agent in self.agents.values():
            agent.halt()
        for agent in self.agents.values():
            if agent.thread is not None:
                agent.thread.join(timeout=5.0)
            # A long-poll still in flight outlives the join; the lease goes now,
            # and that sync's record is refused because the lease is gone.
            if self.store is not None:
                with contextlib.suppress(Exception):
                    self.store.release(self.project_id, agent.cid, self.engine.holder)
                agent.lease = False
        self.agents.clear()
        self.log(said)


class Turn:
    def __init__(self, slot: Slot, cid: str, room_id: str):
        self.slot, self.cid, self.room_id = slot, cid, room_id
        self.id = uuid.uuid4().hex
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.started = time.monotonic()

    @property
    def key(self) -> tuple:
        return (self.slot.project_id, self.cid, self.room_id)


class Engine:
    """The process: its projects, its cap on concurrent turns, its status."""

    def __init__(self, host, *, mode: str, state_dir: Path, echo: bool = True):
        self.host = host
        self.mode = mode
        self.state_dir = Path(state_dir)
        self.echo = echo
        self.node = host.node()
        self.holder = f"{self.node}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.slots: dict[str, Slot] = {}
        self.turns: dict[tuple, Turn] = {}
        self.turns_lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.reload_requested = threading.Event()
        self.stores: dict[tuple, Store] = {}
        self.settings: dict = {}
        self.list_witness = None
        self.started_at = now_iso()
        self.last_publish = 0.0
        self.reload_error: str | None = None
        self.fingerprint: str | None = None
        self.log_lock = threading.Lock()
        # When a room's next turn may start, keyed (project, agent, room), and an
        # agent's in every room, keyed (project, agent, None): a debounce still
        # waiting, or a hook's hold.
        self.holds: dict[tuple, float] = {}
        self._default: dict | None = None

    # -- small parts ----------------------------------------------------------

    def log(self, project: str, message: str) -> None:
        text = " ".join(part.strip() for part in str(message).splitlines() if part.strip())
        line = f"{now_iso()} agentrooms service [{project}]: {text}\n"
        with self.log_lock:
            with contextlib.suppress(OSError):
                with (self.state_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
                    handle.write(line)
            if self.echo:
                with contextlib.suppress(Exception):
                    sys.stderr.write(line)
                    sys.stderr.flush()

    def poke(self) -> None:
        self.wake.set()

    def running_for(self, project_id: str) -> int:
        with self.turns_lock:
            return sum(1 for key in self.turns if key[0] == project_id)

    def store_key(self, setting) -> tuple:
        kwargs = setting.connect_kwargs()
        return (tuple(sorted((k, str(v)) for k, v in kwargs.items())), setting.schema)

    def store_for(self, setting) -> Store:
        key = self.store_key(setting)
        store = self.stores.get(key)
        if store is None:
            store = Store(setting).open()
            self.stores[key] = store
        return store

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        with self.turns_lock:
            running = len(self.turns)
        return {"mode": self.mode, "pid": os.getpid(), "node": self.node, "holder": self.holder,
                "started_at": self.started_at, "published_at": now_iso(),
                "fingerprint": self.fingerprint, "reload_error": self.reload_error,
                "cap": {"max_turns": self.settings.get("max_turns"), "running": running},
                "projects": [slot.row() for slot in sorted(self.slots.values(),
                                                            key=lambda s: s.slug)]}

    def publish(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_publish < PUBLISH_SECONDS:
            return
        self.last_publish = time.monotonic()
        path = self.state_dir / STATUS_FILE
        spare = path.with_name(f".{path.name}.{os.getpid()}")
        with contextlib.suppress(OSError):
            spare.write_text(json.dumps(self.status(), indent=2, default=str))
            os.replace(spare, path)

    # -- the loop -------------------------------------------------------------

    def run(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        import fcntl
        handle = (self.state_dir / LOCK_FILE).open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("busy")
        try:
            (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
            self.settings = self.host.machine_settings()
            self.fingerprint = self.host.fingerprint()
            signal.signal(signal.SIGHUP, lambda *_: (self.reload_requested.set(), self.poke()))
            signal.signal(signal.SIGTERM, lambda *_: (self.stopping.set(), self.poke()))
            signal.signal(signal.SIGINT, lambda *_: (self.stopping.set(), self.poke()))
            self.log(MACHINE_TAG if self.mode == "machine" else self.host.tag(),
                     f"started ({self.mode} mode, pid {os.getpid()}, node {self.node}, "
                     f"max_turns {self.settings.get('max_turns')})")
            while not self.stopping.is_set():
                self.step()
                self.wake.wait(1.0)
                self.wake.clear()
        finally:
            self.shutdown()
            with contextlib.suppress(OSError):
                (self.state_dir / PID_FILE).unlink()
            handle.close()

    def step(self) -> None:
        if self.reload_requested.is_set():
            self.reload_requested.clear()
            self.reload()
        self.take_list()
        for slot in list(self.slots.values()):
            if slot.plan is None and time.monotonic() >= slot.next_admit:
                try:
                    slot.admit()
                except Exception as exc:
                    slot.state, slot.reason = "error", f"{type(exc).__name__}: {exc}"
                    slot.log(f"not served: {slot.reason}")
                    slot.next_admit = time.monotonic() + ADMIT_SECONDS
            slot.check_pause()
        self.reap()
        self.schedule()
        self.publish()

    def take_list(self) -> None:
        witness = self.host.list_witness()
        if witness == self.list_witness and self.slots:
            return
        self.list_witness = witness
        try:
            entries = self.host.entries()
        except Exception as exc:
            self.log(MACHINE_TAG, f"the opt-in list does not load: {exc}")
            return
        for project_id in list(self.slots):
            if project_id not in entries:
                slot = self.slots.pop(project_id)
                slot.release("left the service")
        for project_id, entry in entries.items():
            if project_id not in self.slots:
                self.slots[project_id] = Slot(self, project_id, entry)
        self.publish(force=True)

    def reload(self) -> None:
        try:
            settings = self.host.machine_settings()
        except Exception as exc:
            self.reload_error = str(exc)
            self.log(MACHINE_TAG if self.mode == "machine" else self.host.tag(),
                     f"reload rejected, the previous settings stay: {exc}")
        else:
            self.settings = settings
            self.reload_error = None
        for slot in self.slots.values():
            try:
                slot.reload()
            except Exception as exc:
                slot.reload_error = f"{type(exc).__name__}: {exc}"
                slot.log(f"reload failed: {slot.reload_error}")
        self.list_witness = None
        if self.reload_error is None:
            self.fingerprint = self.host.fingerprint()
        self.publish(force=True)

    def reap(self) -> None:
        with self.turns_lock:
            done = [key for key, turn in self.turns.items()
                    if turn.thread is not None and not turn.thread.is_alive()]
            for key in done:
                self.turns.pop(key)
        if done:
            self.poke()

    def schedule(self) -> None:
        cap = int(self.settings.get("max_turns") or 1)
        candidates: list[tuple] = []
        for slot in self.slots.values():
            if slot.store is None or slot.paused or slot.state not in ("served",):
                continue
            try:
                ready = slot.store.ready(slot.project_id)
            except Exception as exc:
                slot.log(f"store: {exc}")
                continue
            for cid, room_id, since in ready:
                agent = slot.agents.get(cid)
                if agent is None or not agent.lease:
                    continue
                if self.held(slot.project_id, cid, room_id):
                    continue
                candidates.append((slot, cid, room_id, since))
        if not candidates:
            return
        # Fairness: the project running fewest turns goes first, then the
        # oldest wake.
        candidates.sort(key=lambda c: (self.running_for(c[0].project_id), c[3]))
        for slot, cid, room_id, _since in candidates:
            with self.turns_lock:
                if len(self.turns) >= cap:
                    return
                key = (slot.project_id, cid, room_id)
                if key in self.turns:
                    continue
                turn = Turn(slot, cid, room_id)
                self.turns[key] = turn
            turn.thread = threading.Thread(target=self.turn, args=(turn,),
                                           name=f"turn-{slot.slug}-{cid}", daemon=True)
            turn.thread.start()

    # -- holds ----------------------------------------------------------------

    def held(self, project_id: str, cid: str, room_id: str) -> bool:
        now = time.monotonic()
        for key in ((project_id, cid, room_id), (project_id, cid, None)):
            until = self.holds.get(key)
            if until is not None:
                if until > now:
                    return True
                self.holds.pop(key, None)
        return False

    def hold(self, key: tuple, seconds: float) -> None:
        self.holds[key] = max(self.holds.get(key, 0.0), time.monotonic() + max(seconds, 0.0))

    def default_worker(self) -> dict:
        if self._default is None:
            self._default = load_default()
        return self._default

    # -- one turn -------------------------------------------------------------

    def turn(self, turn: Turn) -> None:
        slot, cid, room_id = turn.slot, turn.cid, turn.room_id
        store = slot.store
        agent_plan = (slot.plan or {}).get("agents", {}).get(cid)
        if store is None or agent_plan is None:
            return
        try:
            wakes = store.claim(slot.project_id, cid, room_id, turn.id)
        except Exception as exc:
            slot.log(f"{cid}: claim failed: {exc}")
            return
        if not wakes:
            return
        finished = False
        try:
            refusal = self.host.check(slot.entry, cid)
            if refusal:
                slot.log(f"{cid}: turn in {room_id} not started: {refusal[0]}: {refusal[1]}")
                store.restore(slot.project_id, cid, room_id, turn.id)
                finished = True
                return
            self.serve(turn, agent_plan, wakes)
        except Exception as exc:
            slot.log(f"{cid}: turn in {room_id} failed: {type(exc).__name__}: {exc}")
        finally:
            if not finished:
                with contextlib.suppress(Exception):
                    store.finish(slot.project_id, cid, room_id, turn.id)
            self.poke()

    def bound_worker(self, slot: Slot, cid: str, room_id: str,
                     binding: dict) -> tuple[dict | None, str]:
        """The worker that answers this agent in this room - the room's binding,
        else the agent's - or None and why there is none."""
        rooms = binding.get("rooms") if isinstance(binding.get("rooms"), dict) else {}
        name = rooms.get(room_id) or binding.get("worker")
        if not name:
            return None, "no worker is bound to this agent"
        row = slot.store.worker(slot.project_id, name)
        if row is None:
            return None, f"its bound worker {name!r} has no active version"
        try:
            worker = read_worker(name, row["spec"], row["body"],
                                 f"worker {name!r} version {row['id']}")
        except WorkerInvalid as exc:
            return None, f"its bound worker is refused: {exc}"
        if not worker["enabled"]:
            return None, f"its bound worker {name!r} is switched off"
        worker["version"] = row["id"]
        return worker, ""

    def serve(self, turn: Turn, agent: dict, wakes: list[dict]) -> None:
        """Admit the claimed wakes and answer them, in order: the bound worker and
        whether it is on, each sender against who it admits, the kinds it wakes
        on; what it does not admit goes to the default worker. Then, for what it
        admits, the debounce, the limits and the `before` hook; then the turn and
        the `after` hook. Every refusal is logged with its reason. Wakes a hold or
        the debounce puts off are handed back; every other claimed wake is done."""
        slot, cid, room_id = turn.slot, turn.cid, turn.room_id
        binding = self.host.binding(slot.entry, cid) or {}
        worker, why_none = self.bound_worker(slot, cid, room_id, binding)
        default = self.default_worker()
        admitted: list[dict] = []
        defaulted: dict[str, tuple[dict, str]] = {}
        for wake in wakes:
            kind, sender = wake["kind"], wake.get("sender")
            if worker is not None and kind not in worker["wakes_on"]:
                slot.log(f"{cid}: {kind} in {room_id} from {sender} refused: worker "
                         f"{worker['name']!r} does not wake on {kind}")
                continue
            if worker is not None:
                patterns = worker["invites_from"] if kind == "invite" else worker["accepts_from"]
                if admits(patterns, sender):
                    admitted.append(wake)
                    continue
                reason = (f"worker {worker['name']!r} does not admit "
                          f"{'invites from ' if kind == 'invite' else ''}{sender}")
            else:
                reason = why_none
            if kind not in default["wakes_on"]:
                slot.log(f"{cid}: {kind} in {room_id} from {sender} refused: {reason}; the "
                         "default worker answers only what is addressed to the agent")
                continue
            if sender and sender not in defaulted:
                defaulted[sender] = (wake, reason)
        for sender, (wake, reason) in defaulted.items():
            slot.log(f"{cid}: {wake['kind']} in {room_id} from {sender} refused: {reason}; "
                     "the default worker answers")
            self.answer_default(turn, agent, binding, wake, default)
        if not admitted or worker is None:
            return
        self.admit_and_run(turn, agent, worker, admitted)

    def answer_default(self, turn: Turn, agent: dict, binding: dict, wake: dict,
                       default: dict) -> None:
        """The default worker's answer to one sender: a fixed text and no turn, at
        most once per sender per room per window. An invite is declined with the
        text as its reason, since the agent cannot post where it has not joined."""
        slot, cid, room_id = turn.slot, turn.cid, turn.room_id
        store = slot.store
        sender = wake.get("sender")
        window = default["limits"]["window_seconds"]
        if store.default_answered(slot.project_id, cid, room_id, sender, window):
            slot.log(f"{cid}: default worker already answered {sender} in {room_id} within "
                     f"{window}s; nothing sent")
            return
        text = default_answer(default["body"], user_id=agent["user_id"], sender=sender,
                              supervisor=binding.get("supervisor"))
        mark = f"default:{turn.id}:{sender}"
        store.start_turn(mark, slot.project_id, cid, room_id, DEFAULT_WORKER, None,
                         [sender], [wake["kind"]])
        matrix = self.host.matrix(agent["homeserver"], agent["token"], timeout=60.0)
        try:
            if wake["kind"] == "invite":
                matrix.call("POST", f"/_matrix/client/v3/rooms/{quote(room_id)}/leave",
                            body={"reason": text})
                store.end_turn(mark, "declined")
                slot.log(f"{cid}: default worker declined the invite to {room_id} from {sender}")
                return
            thread = thread_root(self.wake_content(matrix, room_id, wake.get("event_id")))
            event_id = self.post(matrix, room_id, text, thread, data=dict(DEFAULT_MARK))
            store.end_turn(mark, "answered")
            slot.log(f"{cid}: default worker answered {sender} in {room_id} as {event_id}")
        except Exception as exc:
            store.end_turn(mark, "failed")
            slot.log(f"{cid}: default worker's answer to {sender} in {room_id} not sent: "
                     f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
        finally:
            with contextlib.suppress(Exception):
                matrix.close()

    @staticmethod
    def wake_content(matrix, room_id: str, event_id: str | None) -> dict:
        if not event_id:
            return {}
        with contextlib.suppress(Exception):
            found = matrix.call("GET", f"/_matrix/client/v3/rooms/{quote(room_id)}/event/"
                                       f"{quote(event_id)}")
            return (found or {}).get("content") or {}
        return {}

    def admit_and_run(self, turn: Turn, agent: dict, worker: dict, wakes: list[dict]) -> None:
        slot, cid, room_id = turn.slot, turn.cid, turn.room_id
        store = slot.store
        limits = worker["limits"]
        name = worker["name"]
        keys = [w["key"] for w in wakes]
        debounce = limits["debounce_seconds"]
        if debounce:
            newest = max(w["received_at"] for w in wakes)
            if isinstance(newest, _dt.datetime):
                waited = (_dt.datetime.now(_dt.timezone.utc) - newest).total_seconds()
            else:
                waited = time.time() - float(newest)
            if waited < debounce:
                store.restore(slot.project_id, cid, room_id, turn.id, keys)
                self.hold((slot.project_id, cid, room_id), debounce - waited)
                return
        senders = sorted({w.get("sender") for w in wakes if w.get("sender")})
        counts = store.turn_counts(slot.project_id, cid, room_id, senders,
                                   window=limits["window_seconds"],
                                   quiet=limits["quiet_seconds"],
                                   room_cap=limits["turns_per_room"])
        refused = None
        if counts["agent"] >= limits["turns_per_agent"]:
            refused = (f"the agent ran {counts['agent']} turns in {limits['window_seconds']}s, "
                       f"its turns_per_agent")
        elif counts["room"] >= limits["turns_per_room"]:
            refused = (f"the room had {counts['room']} turns without {limits['quiet_seconds']}s "
                       "of quiet, its turns_per_room")
        if refused:
            slot.log(f"{cid}: turn in {room_id} for {', '.join(senders)} refused by worker "
                     f"{name!r}: {refused}")
            return
        over = {s for s, n in counts["senders"].items() if n >= limits["turns_per_sender"]}
        for sender in sorted(over):
            slot.log(f"{cid}: {sender} in {room_id} refused by worker {name!r}: "
                     f"{counts['senders'][sender]} turns in {limits['window_seconds']}s, "
                     "its turns_per_sender")
        wakes = [w for w in wakes if w.get("sender") not in over]
        if not wakes:
            return
        senders = sorted({w.get("sender") for w in wakes if w.get("sender")})
        payload = {"agent": cid, "user_id": agent["user_id"], "room_id": room_id,
                   "worker": name, "version": worker.get("version"),
                   "wakes": [{"kind": w["kind"], "sender": w.get("sender"),
                              "event_id": w.get("event_id")} for w in wakes]}
        env = {**self.host.turn_env(slot.entry), "CLAUDE_PROJECT_DIR": slot.entry["root"],
               "AGENTROOMS_PROJECT_ROOT": slot.entry["root"], "AGENTROOMS_AGENT": cid,
               "AGENTROOMS_USER_ID": agent["user_id"], "AGENTROOMS_ROOM_ID": room_id,
               "AGENTROOMS_WORKER": name, "AGENTROOMS_SENDERS": " ".join(senders),
               "AGENTROOMS_KINDS": " ".join(sorted({w["kind"] for w in wakes}))}
        before = worker["hooks"].get("before")
        if before:
            said = verdict(run_hook(before, cwd=slot.entry["root"], env=env, payload=payload,
                                    seconds=HOOK_TIMEOUTS["before"]))
            if said["verdict"] == "hold":
                seconds = ((said["until"] - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                           if said.get("until") else HOLD_SECONDS)
                store.restore(slot.project_id, cid, room_id, turn.id, [w["key"] for w in wakes])
                self.hold((slot.project_id, cid, room_id), seconds)
                slot.log(f"{cid}: turn in {room_id} held by worker {name!r}'s before hook "
                         + (f"until {said['until'].isoformat()}" if said.get("until")
                            else f"for {HOLD_SECONDS}s ({said.get('why')})")
                         + (f": {said['said']}" if said.get("said") else ""))
                return
            if said["verdict"] == "escalate":
                slot.log(f"{cid}: turn in {room_id} escalated by worker {name!r}'s before hook: "
                         f"{said['why']}")
                return
        store.start_turn(turn.id, slot.project_id, cid, room_id, name, worker.get("version"),
                         senders, sorted({w["kind"] for w in wakes}))
        ended = {"outcome": "failed"}
        try:
            ended = self.run_turn(turn, agent, worker, wakes)
        finally:
            with contextlib.suppress(Exception):
                store.end_turn(turn.id, ended["outcome"], event_id=ended.get("event_id"),
                               harness=ended.get("harness"),
                               session_id=ended.get("session_id"),
                               node=self.node if ended.get("session_id") else None)
        after = worker["hooks"].get("after")
        if after:
            said = verdict(run_hook(after, cwd=slot.entry["root"], env={
                **env, "AGENTROOMS_OUTCOME": ended["outcome"],
                "AGENTROOMS_EVENT_ID": ended.get("event_id") or "",
                "AGENTROOMS_SESSION": ended.get("session_id") or "",
                "AGENTROOMS_HARNESS_FAILURE": ended.get("failure") or "",
                "AGENTROOMS_HARNESS_SAID": (ended.get("said") or "")[:4000]},
                payload={**payload, "outcome": ended["outcome"],
                         "event_id": ended.get("event_id")},
                seconds=HOOK_TIMEOUTS["after"]))
            if said["verdict"] == "hold":
                seconds = ((said["until"] - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                           if said.get("until") else HOLD_SECONDS)
                self.hold((slot.project_id, cid, None), seconds)
                slot.log(f"{cid}: held in every room by worker {name!r}'s after hook "
                         + (f"until {said['until'].isoformat()}" if said.get("until")
                            else f"for {HOLD_SECONDS}s ({said.get('why')})")
                         + (f": {said['said']}" if said.get("said") else ""))
            elif said["verdict"] == "escalate":
                slot.log(f"{cid}: turn in {room_id} escalated by worker {name!r}'s after hook: "
                         f"{said['why']}")

    def run_turn(self, turn: Turn, agent: dict, worker: dict, wakes: list[dict]) -> dict:
        """One harness turn for the admitted wakes, on the worker's profile with
        its role in the system prompt. Answers how it ended: `outcome` (answered,
        silent, failed, not_posted, refused), and the posted event, the session
        and the harness's failure where there are."""
        slot, cid, room_id = turn.slot, turn.cid, turn.room_id
        store = slot.store
        try:
            profile, harness, origin = self.host.profile(slot.entry, store, worker["profile"])
        except Exception as exc:
            slot.log(f"{cid}: turn in {room_id} refused: worker {worker['name']!r} names "
                     f"profile {worker['profile']!r}: {getattr(exc, 'message', exc)}")
            return {"outcome": "refused"}
        matrix = self.host.matrix(agent["homeserver"], agent["token"], timeout=60.0)
        try:
            invite_only = all(w["kind"] == "invite" for w in wakes)
            room_name = None
            events: list[dict] = []
            if not invite_only:
                if self.room_encrypted(matrix, room_id):
                    slot.log(f"{cid}: {room_id} is encrypted; the wake is dropped")
                    return {"outcome": "refused"}
                room_name = self.room_name(matrix, room_id)
                events = self.history(matrix, room_id, worker["history"])
            row = store.session(slot.project_id, cid, room_id)
            resumable = bool(row and row.get("session_id") and row.get("node") == self.node
                             and row.get("harness") == harness)
            shown = events
            if resumable and row.get("seen_ts"):
                shown = [e for e in events if (e.get("origin_server_ts") or 0) > row["seen_ts"]]
            profile = with_rules(profile, room_rules(
                self.host.template(slot.entry), agent=agent, room_id=room_id,
                room_name=room_name, role=worker["body"]))
            prompt = build_prompt(room_id=room_id, room_name=room_name, wakes=wakes,
                                  events=shown, primed=not resumable)
            runner = self.host.runner()
            session = (runner.Session.resume(row["session_id"]) if resumable
                       else runner.Session.fresh())
            how = (f"resuming session {row['session_id']}" if resumable
                   else f"fresh session primed with {len(shown)} message(s)")
            timeout = worker["limits"]["turn_timeout"]
            slot.log(f"{cid}: turn in {room_id} ({', '.join(sorted({w['kind'] for w in wakes}))})"
                     f" started by worker {worker['name']!r} on {origin.get('name')} ({harness}), "
                     f"{how}, cwd {slot.entry['root']}")
            result = self.call_runner(turn, timeout, profile, prompt, session, runner)
            if resumable and missing_session(result):
                slot.log(f"{cid}: session {row['session_id']} is not on this node; "
                         "starting a fresh one primed from the room")
                prompt = build_prompt(room_id=room_id, room_name=room_name, wakes=wakes,
                                      events=events, primed=True)
                result = self.call_runner(turn, timeout, profile, prompt, runner.Session.fresh(),
                                          runner)
            session_id = getattr(result, "session_id", None)
            seen = max([e.get("origin_server_ts") or 0 for e in events] or [0]) or None
            if session_id and harness_ran(result):
                store.save_session(slot.project_id, cid, room_id, harness=harness,
                                   session_id=session_id, node=self.node, seen_ts=seen)
            slot.turns += 1
            ended = {"session_id": session_id, "harness": harness}
            if not getattr(result, "ok", False):
                failure = getattr(result, "failure", None)
                kind = getattr(getattr(failure, "kind", None), "value", None) or str(
                    getattr(failure, "kind", "error"))
                message = str(getattr(failure, "message", ""))
                slot.log(f"{cid}: turn in {room_id} ended without an answer ({kind}): "
                         f"{message[:400]}")
                return {**ended, "outcome": "failed", "failure": kind,
                        "said": " ".join(part for part in (
                            message, str(getattr(result, "stderr_tail", "") or "")) if part)}
            answer = cut_answer(getattr(result, "answer", "") or "")
            if not answer:
                slot.log(f"{cid}: turn in {room_id} stayed silent (session {session_id})")
                return {**ended, "outcome": "silent", "said": getattr(result, "answer", "")}
            refusal = self.host.check(slot.entry, cid)
            if refusal:
                slot.log(f"{cid}: answer in {room_id} not posted: {refusal[0]}: {refusal[1]}")
                return {**ended, "outcome": "not_posted", "said": answer}
            thread = self.answer_thread(events, wakes)
            try:
                event_id = self.post(matrix, room_id, answer, thread)
            except Exception as exc:
                slot.log(f"{cid}: answer in {room_id} not posted: "
                         f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
                return {**ended, "outcome": "not_posted", "said": answer}
            slot.log(f"{cid}: answered in {room_id} as {event_id} (session {session_id})")
            return {**ended, "outcome": "answered", "event_id": event_id, "said": answer}
        finally:
            with contextlib.suppress(Exception):
                matrix.close()

    def call_runner(self, turn: Turn, timeout: int, profile, prompt: str, session, runner):
        timer = threading.Timer(float(timeout), turn.cancel.set)
        timer.daemon = True
        timer.start()
        try:
            return runner.run(prompt, profile, turn.slot.entry["root"], session=session,
                              environ=self.host.turn_env(turn.slot.entry),
                              extra_env={"CLAUDE_PROJECT_DIR": turn.slot.entry["root"],
                                         TURN_ENV: turn.id},
                              cancel=turn.cancel)
        finally:
            timer.cancel()

    # -- reading and answering the room --------------------------------------

    @staticmethod
    def room_encrypted(matrix, room_id: str) -> bool:
        try:
            matrix.call("GET", f"/_matrix/client/v3/rooms/{quote(room_id)}/state/m.room.encryption/")
        except Exception as exc:
            if getattr(exc, "exit_code", None) == 3:
                return False
            raise
        return True

    @staticmethod
    def room_name(matrix, room_id: str) -> str | None:
        with contextlib.suppress(Exception):
            found = matrix.call("GET", f"/_matrix/client/v3/rooms/{quote(room_id)}/state/m.room.name/")
            return (found or {}).get("name")
        return None

    @staticmethod
    def history(matrix, room_id: str, limit: int) -> list[dict]:
        found = matrix.call("GET", f"/_matrix/client/v3/rooms/{quote(room_id)}/messages",
                            params={"dir": "b", "limit": str(limit),
                                    "filter": json.dumps({"types": ["m.room.message"]})})
        chunk = [e for e in (found or {}).get("chunk") or [] if e.get("type") == "m.room.message"]
        return list(reversed(chunk))

    @staticmethod
    def answer_thread(events: list[dict], wakes: list[dict]) -> str | None:
        by_id = {e.get("event_id"): e for e in events}
        for wake in reversed(wakes):
            event = by_id.get(wake.get("event_id"))
            if event is not None:
                return thread_root(event.get("content") or {})
        return None

    @staticmethod
    def post(matrix, room_id: str, text: str, thread: str | None,
             data: dict | None = None) -> str | None:
        content: dict = {"msgtype": "m.text", "body": text}
        if data is not None:
            content[DATA_KEY] = data
        if thread:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread,
                                       "is_falling_back": True,
                                       "m.in_reply_to": {"event_id": thread}}
        txn = uuid.uuid4().hex
        found = matrix.call("PUT", f"/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{txn}",
                            body=content)
        return (found or {}).get("event_id")

    # -- stopping -------------------------------------------------------------

    def shutdown(self) -> None:
        with self.turns_lock:
            running = list(self.turns.values())
        for turn in running:
            turn.cancel.set()
        for turn in running:
            if turn.thread is not None:
                turn.thread.join(timeout=20.0)
        for slot in list(self.slots.values()):
            slot.release("stopped")
        for store in self.stores.values():
            store.close()
        self.log(MACHINE_TAG if self.mode == "machine" else self.host.tag(), "stopped")
        with contextlib.suppress(OSError):
            (self.state_dir / STATUS_FILE).unlink()


def fingerprint_of(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(hashlib.sha256(part).digest())
    return digest.hexdigest()[:12]


def default_node() -> str:
    return socket.gethostname()
