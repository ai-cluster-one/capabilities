"""inbox's service: the loop that starts `inbox deliver` in a served agent's
folder when the store announces an answer for it and when a failed delivery
falls due.

The executable loads this module and hands it a host that answers every
question about a project inside that project's own resolution: whether the
process may serve it, which store it resolves, the command and environment a
delivery starts with. The loop decides only when a delivery starts; what a
delivery does is `inbox deliver`, run as the agent would run it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PID_FILE = "daemon.pid"
LOCK_FILE = "daemon.lock"
STATUS_FILE = "daemon.json"
LOG_FILE = "daemon.log"
DELIVER_LOG = "deliver.log"
MACHINE_TAG = "machine"
ADMIT_SECONDS = 5.0
CHECK_SECONDS = 10.0
FOLLOW_SECONDS = 1.0
RECHECK_SECONDS = 300.0
PUBLISH_SECONDS = 2.0
RECONNECT_FIRST = 1.0
RECONNECT_LONGEST = 30.0
START_FAILED_SECONDS = 30.0

# One agent's undelivered answers: how many, how many are due now (their
# moment come and no claim holding them), and the seconds until the first
# one that is not.
DUE_SQL = (
    "SELECT count(*) AS undelivered, "
    "count(*) FILTER (WHERE coalesce(delivery_due_at, answered_at) <= now() "
    "AND (delivery_lease_until IS NULL OR delivery_lease_until <= now())) AS due, "
    "extract(epoch FROM min(greatest(coalesce(delivery_due_at, answered_at), "
    "coalesce(delivery_lease_until, '-infinity'))) - now()) AS next_in "
    "FROM inbox_items WHERE agent_id = %s AND answered_at IS NOT NULL "
    "AND on_answer IS NOT NULL AND delivered_at IS NULL")


def iso(moment) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso() -> str:
    return iso(dt.datetime.now(dt.timezone.utc))


def write_atomic(path: Path, text: str) -> None:
    spare = path.with_name(f".{path.name}.{os.getpid()}")
    spare.write_text(text)
    os.replace(spare, path)


class Busy(Exception):
    """Another process holds the state root."""


class Link:
    """The service's way to one store, shared by every agent on it: a working
    connection lent for each step, and a follower of the change channel that
    says which agents' items moved."""

    def __init__(self, host, setting):
        self.host = host
        self.setting = setting
        self.follower = None
        self.migrated = False
        self.error: str | None = None
        self.retry_at = 0.0
        self.delay = RECONNECT_FIRST

    def row(self) -> dict:
        return {"schema": self.setting.schema, "connected": self.migrated and not self.error,
                "following": self.follower is not None, "error": self.error}

    @contextlib.contextmanager
    def conn(self):
        db = self.host.db()
        conn = db.connect(application_name=f"{self.host.name}-service", setting=self.setting,
                          transaction=True)
        try:
            if not self.migrated:
                steps, major, minor = self.host.ledger()
                db.migrate(conn, self.host.name, steps, major=major, minor=minor)
                self.migrated = True
            conn.autocommit = True
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def changed(self, conn) -> set[str]:
        """The agents whose version of the channel moved since the last ask;
        on the first ask, every agent the store holds a version for."""
        if self.follower is None:
            self.follower = self.host.db().Follower(
                [self.host.channel], application_name=f"{self.host.name}-service",
                setting=self.setting)
        return {scope for (_, scope) in self.follower.poll(conn)}

    def failed(self, exc: BaseException, log) -> None:
        why = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        if why != self.error:
            log(f"store {self.setting.schema}: not reachable, asking again: {why}")
        self.error = why
        self.retry_at = time.monotonic() + self.delay
        self.delay = min(self.delay * 2, RECONNECT_LONGEST)

    def reached(self, log) -> None:
        if self.error is not None:
            log(f"store {self.setting.schema}: reachable again")
        self.error = None
        self.delay = RECONNECT_FIRST


class Served:
    """One agent as the process sees it."""

    def __init__(self, service: "Service", agent_id: str, entry: dict):
        self.service = service
        self.agent_id = agent_id
        self.entry = entry
        self.slug = entry.get("slug") or agent_id
        self.root = Path(entry["root"])
        self.state, self.reason = "pending", None
        self.link: Link | None = None
        self.lock_handle = None
        self.next_admit = 0.0
        self.next_check = 0.0
        self.dirty = True
        self.due_at: float | None = None
        self.recheck_at: float | None = None
        self.child: subprocess.Popen | None = None
        self.child_started: str | None = None
        self.last: dict | None = None
        self.undelivered: int | None = None

    def log(self, message: str) -> None:
        self.service.log(self.slug, message)

    def row(self) -> dict:
        return {"project": self.slug, "project_id": self.agent_id, "root": str(self.root),
                "state": self.state, "reason": self.reason, "undelivered": self.undelivered,
                "delivering": ({"pid": self.child.pid, "started_at": self.child_started}
                               if self.child is not None else None),
                "next_due_in": (round(max(0.0, self.due_at - time.monotonic()), 1)
                                if self.due_at is not None else None),
                "last_deliver": self.last}

    def admit(self) -> None:
        host = self.service.host
        machine = self.service.mode == "machine"
        if not self.root.is_dir():
            refusal, setting = ("error", f"the project folder {self.root} is gone"), None
        else:
            refusal, setting = host.admit(self.agent_id, self.root, machine)
        if refusal is None and machine and not self._take_lock():
            refusal = ("refused", f"another {host.name} service process serves this project; "
                       "stop it there")
        if refusal:
            if (self.state, self.reason) != refusal:
                self.log(f"not served: {refusal[0]}: {refusal[1]}")
            self.state, self.reason = refusal
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        self.link = self.service.link_for(setting)
        self.state, self.reason = "served", None
        self.dirty = True
        self.next_check = time.monotonic() + CHECK_SECONDS
        self.log(f"served: {self.root}")

    def _take_lock(self) -> bool:
        """The lock that keeps a second process off this project, the same
        file in both modes; a project-mode process holds it as its own."""
        if self.lock_handle is not None:
            return True
        import fcntl
        path = self.service.host.lock_path(self.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.lock_handle = handle
        return True

    def check(self) -> None:
        self.next_check = time.monotonic() + CHECK_SECONDS
        if not self.root.is_dir():
            refusal = ("error", f"the project folder {self.root} is gone")
        else:
            refusal, _setting = self.service.host.admit(self.agent_id, self.root,
                                                        self.service.mode == "machine")
        if refusal:
            self.state, self.reason = refusal
            self.release(f"no longer served: {refusal[0]}: {refusal[1]}")
            self.next_admit = time.monotonic() + ADMIT_SECONDS

    def release(self, said: str) -> None:
        self.stop_child()
        self.link = None
        if self.lock_handle is not None:
            with contextlib.suppress(Exception):
                self.lock_handle.close()
            self.lock_handle = None
        self.log(said)

    def read_due(self, conn) -> None:
        """Read whether a delivery of this agent is due now, and when the next
        one falls due."""
        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(DUE_SQL, (self.agent_id,))
            found = cur.fetchone()
        now = time.monotonic()
        self.dirty = False
        self.undelivered = found["undelivered"]
        if found["due"]:
            self.due_at = now
        elif found["next_in"] is not None:
            self.due_at = now + max(0.0, float(found["next_in"]))
        else:
            self.due_at = None
        # A change no version showed is still found within this interval.
        self.recheck_at = (now + RECHECK_SECONDS
                           if self.due_at is None or self.due_at > now + RECHECK_SECONDS
                           else None)

    def start_child(self) -> None:
        host = self.service.host
        argv, env = host.deliver_command(self.root)
        log_path = self.service.state_dir / DELIVER_LOG
        try:
            with open(log_path, "ab") as out:
                out.write(f"{now_iso()} [{self.slug}] {host.name} deliver\n".encode())
                out.flush()
                self.child = subprocess.Popen(argv, cwd=str(self.root), env=env, stdout=out,
                                              stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL,
                                              start_new_session=True)
        except OSError as exc:
            self.log(f"cannot start `{host.name} deliver`: {exc}")
            self.due_at = time.monotonic() + START_FAILED_SECONDS
            return
        self.child_started = now_iso()
        self.due_at = None
        self.log(f"started `{host.name} deliver`, pid {self.child.pid}: a delivery is due")

    def reap(self) -> None:
        if self.child is None:
            return
        code = self.child.poll()
        if code is None:
            return
        self.child = None
        self.last = {"exit": code, "at": now_iso()}
        outcome = {0: "every due delivery ran to success",
                   7: "a delivery failed and falls due again"}.get(
            code, f"see {self.service.state_dir / DELIVER_LOG}")
        self.log(f"`{self.service.host.name} deliver` ended with exit {code}: {outcome}")
        self.dirty = True

    def stop_child(self) -> None:
        if self.child is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.child.pid, signal.SIGTERM)
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.child.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                self.child.wait(timeout=5)
        self.child = None


class Service:
    """The process: the agents it serves and the stores they resolve."""

    def __init__(self, host, *, mode: str, state_dir: Path, tag: str = MACHINE_TAG):
        self.host = host
        self.mode = mode
        self.state_dir = Path(state_dir)
        self.tag = tag
        self.node = host.node()
        self.served: dict[str, Served] = {}
        self.links: dict[tuple, Link] = {}
        self.stopping = threading.Event()
        self.started_at = now_iso()
        self.list_witness = None
        self.last_publish = 0.0
        self.changed = False

    def log(self, project: str, message: str) -> None:
        self.changed = True
        text = " ".join(part.strip() for part in str(message).splitlines() if part.strip())
        line = f"{now_iso()} {self.host.name} service [{project}]: {text}\n"
        with contextlib.suppress(OSError):
            with (self.state_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
                handle.write(line)
        with contextlib.suppress(Exception):
            sys.stderr.write(line)
            sys.stderr.flush()

    def log_machine(self, message: str) -> None:
        self.log(self.tag, message)

    def link_for(self, setting) -> Link:
        key = (tuple(sorted((k, str(v)) for k, v in setting.connect_kwargs().items())),
               setting.schema)
        if key not in self.links:
            self.links[key] = Link(self.host, setting)
        return self.links[key]

    def status(self) -> dict:
        return {"mode": self.mode, "pid": os.getpid(), "node": self.node,
                "started_at": self.started_at, "published_at": now_iso(),
                "stores": [link.row() for link in self.links.values()],
                "projects": [s.row() for s in sorted(self.served.values(),
                                                     key=lambda s: s.slug)]}

    def publish(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_publish < PUBLISH_SECONDS:
            return
        self.last_publish = time.monotonic()
        self.changed = False
        with contextlib.suppress(OSError):
            write_atomic(self.state_dir / STATUS_FILE,
                         json.dumps(self.status(), indent=2, default=str))

    def take_list(self) -> None:
        witness = self.host.list_witness()
        if witness == self.list_witness and self.served:
            return
        self.list_witness = witness
        try:
            entries = self.host.entries()
        except Exception as exc:
            self.log_machine("the opt-in list does not load: "
                             f"{getattr(exc, 'message', None) or exc}")
            return
        for agent_id in list(self.served):
            if agent_id not in entries:
                self.served.pop(agent_id).release("left the service")
        for agent_id, entry in entries.items():
            if agent_id not in self.served:
                self.served[agent_id] = Served(self, agent_id, entry)
        self.publish(force=True)

    def run(self) -> None:
        import fcntl
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.state_dir / LOCK_FILE).open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Busy(str(self.state_dir))
        try:
            (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
            signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
            signal.signal(signal.SIGINT, lambda *_: self.stopping.set())
            self.log_machine(f"started ({self.mode} mode, pid {os.getpid()}, node {self.node})")
            while not self.stopping.is_set():
                self.step()
                self.wait(self.until_next())
        finally:
            for served in list(self.served.values()):
                served.release("released: the service stops")
            for link in self.links.values():
                if link.follower is not None:
                    link.follower.close()
            self.log_machine("stopped")
            with contextlib.suppress(OSError):
                (self.state_dir / STATUS_FILE).unlink()
            with contextlib.suppress(OSError):
                (self.state_dir / PID_FILE).unlink()
            handle.close()

    def until_next(self) -> float:
        seconds = FOLLOW_SECONDS
        now = time.monotonic()
        for served in self.served.values():
            if served.link is not None and served.child is None and served.due_at is not None:
                seconds = min(seconds, served.due_at - now)
        return max(0.05, seconds)

    def wait(self, seconds: float) -> None:
        """Until `seconds` pass, the process is asked to stop, or a store whose
        two addresses are named apart notifies a change; with more than one
        such store it waits out the time, which is the poll interval."""
        listening = [link.follower for link in self.links.values()
                     if link.follower is not None and link.setting.has_transaction_address]
        if len(listening) == 1:
            listening[0].wait(seconds, self.stopping)
        else:
            self.stopping.wait(seconds)

    def step(self) -> None:
        self.take_list()
        now = time.monotonic()
        for served in list(self.served.values()):
            served.reap()
            if served.link is None and now >= served.next_admit:
                served.admit()
            elif served.link is not None and now >= served.next_check:
                served.check()
        for link in list(self.links.values()):
            mine = [s for s in self.served.values() if s.link is link]
            if not mine or time.monotonic() < link.retry_at:
                continue
            try:
                with link.conn() as conn:
                    moved = link.changed(conn)
                    for served in mine:
                        if (served.agent_id in moved or served.dirty
                                or (served.recheck_at is not None
                                    and time.monotonic() >= served.recheck_at)):
                            served.read_due(conn)
                link.reached(self.log_machine)
            except Exception as exc:
                link.failed(exc, self.log_machine)
        for served in self.served.values():
            if (served.link is not None and served.child is None
                    and served.due_at is not None and time.monotonic() >= served.due_at):
                served.start_child()
        self.publish(force=self.changed)
