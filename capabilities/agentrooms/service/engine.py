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
room, the lease that keeps a second process off an agent, and a project's pause.
No message text is kept: a turn reads the room live.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from string import Template
from typing import Any, Callable

LEDGER_OWNER = "agentrooms"
LEDGER_MAJOR = 1
LEDGER_MINOR = 0
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
# The room account data an agent marks a room with, to be woken by every message.
MARK_TYPE = "agentrooms.marked"
# Where custom data rides in a message's content.
DATA_KEY = "agentrooms.data"

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

    def restore(self, project: str, agent: str, room_id: str, turn: str) -> None:
        self.execute("UPDATE agentrooms_wakes SET claimed_by = NULL WHERE project_id = %s "
                     "AND agent = %s AND room_id = %s AND claimed_by = %s",
                     (project, agent, room_id, turn))

    # -- sessions -------------------------------------------------------------

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


def read_sync(response: dict, user_id: str, rooms: dict[str, RoomView], *,
              initial: bool, member_count: Callable[[str], int | None]) -> tuple[list[dict], list[str]]:
    """The wakes one sync response brings for `user_id`, and notes about what it
    passed over. `rooms` is updated in place. An initial sync wakes only for the
    invites waiting; history before the agent was first served wakes nothing."""
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
        for event in timeline:
            if event.get("type") != "m.room.message" or event.get("sender") == user_id:
                continue
            if "state_key" in event:
                continue
            if view.encrypted:
                notes.append(f"{room_id}: a message in an encrypted room is passed over")
                continue
            content = event.get("content") or {}
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


def room_rules(template: str, *, agent: dict, room_id: str, room_name: str | None) -> str:
    """The room prompt filled for this agent and room: the turn's standing rules,
    handed to the harness as its system-prompt append rather than as a message."""
    return Template(template).safe_substitute(
        user_id=agent["user_id"], connection=agent["connection"], room_id=room_id,
        room_name=room_name or room_id, silent=SILENT).strip()


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
                "last_sync_at": self.last_sync, "profile": self.plan["profile_name"]}

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

        wakes, notes = read_sync(response, self.plan["user_id"], self.rooms, initial=initial,
                                 member_count=member_count)
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
            self.run_turn(turn, agent_plan, wakes)
        except Exception as exc:
            slot.log(f"{cid}: turn in {room_id} failed: {type(exc).__name__}: {exc}")
        finally:
            if not finished:
                with contextlib.suppress(Exception):
                    store.finish(slot.project_id, cid, room_id, turn.id)
            self.poke()

    def run_turn(self, turn: Turn, agent: dict, wakes: list[dict]) -> None:
        slot, cid, room_id = turn.slot, turn.cid, turn.room_id
        store = slot.store
        matrix = self.host.matrix(agent["homeserver"], agent["token"], timeout=60.0)
        try:
            invite_only = all(w["kind"] == "invite" for w in wakes)
            room_name = None
            events: list[dict] = []
            if not invite_only:
                if self.room_encrypted(matrix, room_id):
                    slot.log(f"{cid}: {room_id} is encrypted; the wake is dropped")
                    return
                room_name = self.room_name(matrix, room_id)
                events = self.history(matrix, room_id, agent["history"])
            row = store.session(slot.project_id, cid, room_id)
            harness = agent["harness"]
            resumable = bool(row and row.get("session_id") and row.get("node") == self.node
                             and row.get("harness") == harness)
            shown = events
            if resumable and row.get("seen_ts"):
                shown = [e for e in events if (e.get("origin_server_ts") or 0) > row["seen_ts"]]
            profile = with_rules(agent["profile"], room_rules(
                self.host.template(slot.entry), agent=agent, room_id=room_id,
                room_name=room_name))
            prompt = build_prompt(room_id=room_id, room_name=room_name, wakes=wakes,
                                  events=shown, primed=not resumable)
            runner = self.host.runner()
            session = (runner.Session.resume(row["session_id"]) if resumable
                       else runner.Session.fresh())
            how = (f"resuming session {row['session_id']}" if resumable
                   else f"fresh session primed with {len(shown)} message(s)")
            slot.log(f"{cid}: turn in {room_id} ({', '.join(sorted({w['kind'] for w in wakes}))})"
                     f" started on {agent['profile_name']} ({harness}), {how}, cwd {slot.entry['root']}")
            result = self.call_runner(turn, agent, profile, prompt, session, runner)
            if resumable and missing_session(result):
                slot.log(f"{cid}: session {row['session_id']} is not on this node; "
                         "starting a fresh one primed from the room")
                prompt = build_prompt(room_id=room_id, room_name=room_name, wakes=wakes,
                                      events=events, primed=True)
                result = self.call_runner(turn, agent, profile, prompt, runner.Session.fresh(),
                                          runner)
            session_id = getattr(result, "session_id", None)
            seen = max([e.get("origin_server_ts") or 0 for e in events] or [0]) or None
            if session_id and harness_ran(result):
                store.save_session(slot.project_id, cid, room_id, harness=harness,
                                   session_id=session_id, node=self.node, seen_ts=seen)
            slot.turns += 1
            if not getattr(result, "ok", False):
                failure = getattr(result, "failure", None)
                kind = getattr(getattr(failure, "kind", None), "value", None) or str(
                    getattr(failure, "kind", "error"))
                slot.log(f"{cid}: turn in {room_id} ended without an answer ({kind}): "
                         f"{str(getattr(failure, 'message', ''))[:400]}")
                return
            answer = cut_answer(getattr(result, "answer", "") or "")
            if not answer:
                slot.log(f"{cid}: turn in {room_id} stayed silent (session {session_id})")
                return
            refusal = self.host.check(slot.entry, cid)
            if refusal:
                slot.log(f"{cid}: answer in {room_id} not posted: {refusal[0]}: {refusal[1]}")
                return
            thread = self.answer_thread(events, wakes)
            try:
                event_id = self.post(matrix, room_id, answer, thread)
            except Exception as exc:
                slot.log(f"{cid}: answer in {room_id} not posted: "
                         f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
                return
            slot.log(f"{cid}: answered in {room_id} as {event_id} (session {session_id})")
        finally:
            with contextlib.suppress(Exception):
                matrix.close()

    def call_runner(self, turn: Turn, agent: dict, profile, prompt: str, session, runner):
        timer = threading.Timer(float(agent["turn_timeout"]), turn.cancel.set)
        timer.daemon = True
        timer.start()
        try:
            return runner.run(prompt, profile, turn.slot.entry["root"], session=session,
                              environ=self.host.turn_env(turn.slot.entry),
                              extra_env={"CLAUDE_PROJECT_DIR": turn.slot.entry["root"]},
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
    def post(matrix, room_id: str, text: str, thread: str | None) -> str | None:
        content: dict = {"msgtype": "m.text", "body": text}
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
