"""memory's service: the loop that starts `memory groom --if-due` in a served
agent's folder - soon after the agent is admitted, again after its memory
changes in the store, and on a recurring timer - so every agent is groomed
with no schedule set up by anyone.

The executable loads this module and hands it a host that answers every
question about a project inside that project's own resolution: whether the
process may serve it, which store it resolves, the command and environment a
pass starts with. The loop decides only when to ask; whether a pass is due,
and what it does, is `memory groom --if-due`, run as the agent would run it.
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
GROOM_LOG = "groom.log"
MACHINE_TAG = "machine"
# How often a refused agent is asked again, and a served one checked again.
ADMIT_SECONDS = 5.0
CHECK_SECONDS = 30.0
# How soon after admission the first ask starts, and how long a change in the
# store settles before the ask it causes, so a burst of writes is one ask.
FIRST_ASK_SECONDS = 5.0
SETTLE_SECONDS = 10.0
# The recurring ask, so a pass that falls due by time alone - the quiet period
# or the interval running out - is still run.
RECHECK_SECONDS = 1800.0
# Passes running at once on the machine, every agent counted.
MAX_PASSES = 2
# The longest a pass may run before it is stopped: past the longest model
# deadline a pass may be given (groom_timeout_seconds at most 7200), with room.
CHILD_LONGEST_SECONDS = 7200.0 + 900.0
START_FAILED_SECONDS = 60.0
FOLLOW_SECONDS = 1.0
PUBLISH_SECONDS = 2.0
RECONNECT_FIRST = 1.0
RECONNECT_LONGEST = 30.0
LINE_LONGEST = 2000
LOG_LONGEST_BYTES = 10 * 1024 * 1024


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


def rotate(path: Path) -> None:
    """Keep a log bounded: past LOG_LONGEST_BYTES it moves to `<name>.1`."""
    with contextlib.suppress(OSError):
        if path.stat().st_size > LOG_LONGEST_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))


class Busy(Exception):
    """Another process holds the state root."""


class Link:
    """The service's way to one store, shared by every agent on it: a working
    connection lent for each step, and a follower of the change channel that
    says which agents' memory moved."""

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
        # When the next `memory groom --if-due` starts, and why; None while
        # nothing asks for one.
        self.ask_at: float | None = None
        self.ask_why: str | None = None
        self.timer_at: float | None = None
        # A change seen while a pass runs, asked about once it ends.
        self.again = False
        self.child: subprocess.Popen | None = None
        self.child_started: str | None = None
        self.child_deadline = 0.0
        self.last: dict | None = None

    def log(self, message: str) -> None:
        self.service.log(self.slug, message)

    def row(self) -> dict:
        now = time.monotonic()
        return {"project": self.slug, "project_id": self.agent_id, "root": str(self.root),
                "state": self.state, "reason": self.reason,
                "grooming": ({"pid": self.child.pid, "started_at": self.child_started}
                             if self.child is not None else None),
                "next_ask_in": (round(max(0.0, self.ask_at - now), 1)
                                if self.ask_at is not None else None),
                "next_ask_why": self.ask_why if self.ask_at is not None else None,
                "timer_in": (round(max(0.0, self.timer_at - now), 1)
                             if self.timer_at is not None and self.link is not None else None),
                "last_groom": self.last}

    def ask(self, after: float, why: str) -> None:
        """Ask for a pass `after` seconds from now, unless one is asked sooner."""
        at = time.monotonic() + after
        if self.ask_at is None or at < self.ask_at:
            self.ask_at, self.ask_why = at, why

    def _refusal(self):
        host = self.service.host
        if not self.root.is_dir():
            return ("error", f"the project folder {self.root} is gone"), None
        return host.admit(self.agent_id, self.root, self.service.mode == "machine")

    def admit(self) -> None:
        host = self.service.host
        refusal, setting = self._refusal()
        if refusal is None and self.service.mode == "machine" and not self._take_lock():
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
        self.next_check = time.monotonic() + CHECK_SECONDS
        self.timer_at = time.monotonic() + RECHECK_SECONDS
        self.ask(FIRST_ASK_SECONDS, "admitted")
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

    def check(self) -> bool:
        """Whether the agent is still admitted; one that is not is released."""
        self.next_check = time.monotonic() + CHECK_SECONDS
        refusal, _setting = self._refusal()
        if refusal:
            self.state, self.reason = refusal
            self.release(f"no longer served: {refusal[0]}: {refusal[1]}")
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return False
        return True

    def release(self, said: str) -> None:
        self.stop_child()
        self.link = None
        self.ask_at = self.ask_why = self.timer_at = None
        self.again = False
        if self.lock_handle is not None:
            with contextlib.suppress(Exception):
                self.lock_handle.close()
            self.lock_handle = None
        self.log(said)

    def moved(self) -> None:
        """The store says this agent's memory changed."""
        if self.child is not None:
            self.again = True
        else:
            self.ask(SETTLE_SECONDS, "its memory changed")

    def tick(self) -> None:
        """The recurring ask."""
        if self.timer_at is not None and time.monotonic() >= self.timer_at:
            self.timer_at = time.monotonic() + RECHECK_SECONDS
            if self.child is None:
                self.ask(0.0, "the recurring check")

    def start_child(self) -> None:
        host = self.service.host
        argv, env = host.groom_command(self.root)
        log_path = self.service.state_dir / GROOM_LOG
        why = self.ask_why or "asked"
        self.ask_at = self.ask_why = None
        self.timer_at = time.monotonic() + RECHECK_SECONDS
        rotate(log_path)
        try:
            with open(log_path, "ab") as out:
                out.write(f"{now_iso()} [{self.slug}] {host.name} groom --if-due "
                          f"({why})\n".encode())
                out.flush()
                self.child = subprocess.Popen(argv, cwd=str(self.root), env=env, stdout=out,
                                              stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL,
                                              start_new_session=True)
        except OSError as exc:
            self.log(f"cannot start `{host.name} groom --if-due`: {exc}")
            self.ask(START_FAILED_SECONDS, "a start that failed")
            return
        self.child_started = now_iso()
        self.child_deadline = time.monotonic() + CHILD_LONGEST_SECONDS
        self.log(f"started `{host.name} groom --if-due`, pid {self.child.pid}: {why}")

    def reap(self) -> None:
        if self.child is None:
            return
        code = self.child.poll()
        if code is None:
            if time.monotonic() >= self.child_deadline:
                self.log(f"`{self.service.host.name} groom --if-due` ran past "
                         f"{int(CHILD_LONGEST_SECONDS)} seconds; stopped")
                self.stop_child()
                self.last = {"exit": None, "at": now_iso(), "stopped": True}
            return
        self.child = None
        self.last = {"exit": code, "at": now_iso()}
        outcome = {0: "a pass ran, or none was due",
                   7: "another grooming pass holds the project"}.get(
            code, f"see {self.service.state_dir / GROOM_LOG}")
        self.log(f"`{self.service.host.name} groom --if-due` ended with exit {code}: {outcome}")
        if self.again:
            self.again = False
            self.ask(SETTLE_SECONDS, "its memory changed")

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

    def __init__(self, host, *, mode: str, state_dir: Path, tag: str = MACHINE_TAG,
                 max_passes: int = MAX_PASSES):
        self.host = host
        self.mode = mode
        self.state_dir = Path(state_dir)
        self.tag = tag
        self.max_passes = max_passes
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
        if len(text) > LINE_LONGEST:
            text = text[:LINE_LONGEST] + " ..."
        line = f"{now_iso()} {self.host.name} service [{project}]: {text}\n"
        path = self.state_dir / LOG_FILE
        rotate(path)
        with contextlib.suppress(OSError):
            with path.open("a", encoding="utf-8") as handle:
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

    def running(self) -> int:
        return sum(1 for s in self.served.values() if s.child is not None)

    def status(self) -> dict:
        return {"mode": self.mode, "pid": os.getpid(), "node": self.node,
                "started_at": self.started_at, "published_at": now_iso(),
                "max_passes": self.max_passes, "grooming": self.running(),
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
            self.close()
            with contextlib.suppress(OSError):
                (self.state_dir / STATUS_FILE).unlink()
            with contextlib.suppress(OSError):
                (self.state_dir / PID_FILE).unlink()
            handle.close()

    def close(self) -> None:
        for served in list(self.served.values()):
            if served.link is not None or served.lock_handle is not None:
                served.release("released: the service stops")
        for link in self.links.values():
            if link.follower is not None:
                with contextlib.suppress(Exception):
                    link.follower.close()
        self.log_machine("stopped")

    def until_next(self) -> float:
        seconds = FOLLOW_SECONDS
        now = time.monotonic()
        for served in self.served.values():
            if served.link is not None and served.child is None and served.ask_at is not None:
                seconds = min(seconds, served.ask_at - now)
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
                    if served.agent_id in moved:
                        served.moved()
                link.reached(self.log_machine)
            except Exception as exc:
                link.failed(exc, self.log_machine)
        for served in self.served.values():
            if served.link is not None:
                served.tick()
        self.start_due()
        self.publish(force=self.changed)

    def start_due(self) -> None:
        """Start the asks that are due, the longest waiting first, while the
        machine's cap allows and the agent's store answers; each agent is
        admitted again just before."""
        now = time.monotonic()
        waiting = sorted((s for s in self.served.values()
                          if s.link is not None and s.link.error is None
                          and s.child is None and s.ask_at is not None
                          and now >= s.ask_at),
                         key=lambda s: s.ask_at)
        for served in waiting:
            if self.running() >= self.max_passes:
                return
            if served.check():
                served.start_child()
