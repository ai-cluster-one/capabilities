"""The tasks service: one daemon per project that runs the conveyor on its own.

Every enabled worker that takes something is a lane. Whenever a lane has a free
slot and the store holds work it would take, the daemon starts one child
process running `tasks run <worker> --apply` - the same claim, frame, profile,
turn and settlement a person gets from that command - and counts it against the
lane's cap and the cap across every lane.

The daemon decides nothing about a task. It asks the store whether a lane would
take something, starts the command that takes it, and reads what the command
wrote back. Everything it knows about the project arrives through the host the
executable hands it, so this file holds the loop, the processes and the files
the daemon publishes, and nothing about workers, settings or the store's shape.

It wakes when the store notifies that a task became claimable, at every poll,
at the earliest moment a pickup or a lease falls due, and when one of its own
turns claims or ends. A store without the notification leaves it the poll.

It holds its connections to the store rather than opening one per question:
one it listens on, and one every question it asks is put to, each question cut
off by the store when it runs past `QUESTION_TIMEOUT_SECONDS`. They are kept per
store in a pool, so whoever asks of the same store shares the same two. The
listener carries a round trip of its own every `PING_SECONDS`, and so does the
question connection when nothing else went over it for that long, so a proxy in
front of the store does not close either as idle.

It outlives the store being away. A connection it loses, or cannot open, is
asked for again on a doubling interval until the store answers, and the moment
it has it again it asks the store for work, since what it missed was neither
announced nor answered.

It never starts a turn on a task one of its turns holds, or on one a turn of its
own ended on less than `retry_delay_seconds` ago: its turns are told to claim
other tasks, and it wakes again when the delay is over. However a turn fails,
its task comes back no faster than that.

It starts no turn on a lane the pause holds. The pause is a file in its state
directory, written by `service pause` and `service resume` whether or not a
daemon runs, and read on every pass of the loop; it is runtime state, not part
of the declaration, so it moves no fingerprint and outlives a restart. Turns
already running are not touched by it.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

LOCK_FILE = "daemon.lock"
PID_FILE = "daemon.pid"
FINGERPRINT_FILE = "daemon.fingerprint"
STATUS_FILE = "daemon.json"
LOG_FILE = "daemon.log"
PAUSE_FILE = "paused.json"
TURNS_DIR = "turns"
RECEIPT_ENV = "TASKS_TURN_RECEIPT"
EXCLUDE_ENV = "TASKS_TURN_EXCLUDE"

# The longest the loop sleeps between looks at its children and its signals.
# A stop or a reload is taken within this, and so is a turn that ended.
TICK_SECONDS = 1.0

# How soon a connection that was lost, or could not be opened, is asked for
# again: the first interval, doubled after every attempt that fails, up to the
# longest. The listener and the connection questions go on keep one each.
RELISTEN_FIRST_SECONDS = 1.0
RELISTEN_LONGEST_SECONDS = 30.0

# How long one question may run before the store cuts it off. A question cut off
# counts as its connection lost, so a store that hangs costs the daemon this
# long and never its loop.
QUESTION_TIMEOUT_SECONDS = 30

# How often a held connection carries a round trip of its own: the listener
# always, and the connection questions go on whenever nothing else went over it
# for this long.
PING_SECONDS = 120.0

# Wakes that may start a turn only to have its claim run the sweeps: a lapsed
# lease and an ended wait are put back by a claim and by nothing else, and no
# write announces either. Starting one on every wake would spin on a sweep that
# keeps failing, so it happens on the clock and nowhere else. Listening again,
# or asking again, after the store was away counts as the clock: a lease may
# have lapsed meanwhile.
_SWEEP_WAKES = {"start", "poll", "pickup", "reload", "relisten", "reconnect", "resume"}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _at(seconds_ahead: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=max(0.0, seconds_ahead))
    return moment.isoformat(timespec="seconds")


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


def read_pid(state_dir: Path) -> int | None:
    try:
        return int((state_dir / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def read_fingerprint(state_dir: Path) -> str | None:
    try:
        return (state_dir / FINGERPRINT_FILE).read_text().strip() or None
    except OSError:
        return None


def read_status(state_dir: Path) -> dict | None:
    try:
        found = json.loads((state_dir / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def _write_atomic(path: Path, text: str) -> None:
    spare = path.with_name(f".{path.name}.{os.getpid()}")
    spare.write_text(text)
    os.replace(spare, path)


def log_line(state_dir: Path, message: str) -> str:
    """Append one line to the service log, as the daemon writes it, and return
    it. A verb that changes the daemon's runtime state records it here too,
    whether or not a daemon runs."""
    line = f"{now_iso()} tasks service: {message}\n"
    with contextlib.suppress(OSError):
        with (Path(state_dir) / LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line)
    return line


def _pause_entry(found) -> dict | None:
    if not isinstance(found, dict):
        return None
    return {"reason": found.get("reason") if isinstance(found.get("reason"), str) else None,
            "at": found.get("at"), "by": found.get("by")}


def read_pause(state_dir: Path) -> dict:
    """The pause as written: `all`, the entry holding every lane or None, and
    `lanes`, an entry per lane held by name. Each entry is its reason, the
    moment it was set and who set it. No file is no pause."""
    found = _read_json(Path(state_dir) / PAUSE_FILE) or {}
    lanes = found.get("lanes") if isinstance(found.get("lanes"), dict) else {}
    return {"all": _pause_entry(found.get("all")),
            "lanes": {str(name): entry for name, entry in
                      ((name, _pause_entry(raw)) for name, raw in sorted(lanes.items()))
                      if entry is not None}}


def write_pause(state_dir: Path, pause: dict) -> None:
    """Write the pause, or remove the file when it holds nothing."""
    path = Path(state_dir) / PAUSE_FILE
    if not pause.get("all") and not pause.get("lanes"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps({"all": pause.get("all"),
                                    "lanes": dict(sorted(pause.get("lanes", {}).items()))},
                                   indent=2) + "\n")


def holding(pause: dict, workers) -> list[str]:
    """The lanes among `workers` that start no new turn under `pause`."""
    return [worker for worker in workers if pause.get("all") or worker in pause.get("lanes", {})]


def _read_json(path: Path) -> dict | None:
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def _last_json(text: str) -> dict | None:
    """The answer a `tasks` command printed: one JSON document on stdout."""
    try:
        found = json.loads(text)
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _why(exc: BaseException) -> str:
    """What went wrong, in the words the executable used. A refusal carries its
    own sentence; an exit raised by the executable has already written its
    error to the log, so its code is all that is left to say."""
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    if isinstance(exc, SystemExit):
        return f"exited {exc.code}; the error is on the line above"
    return f"{type(exc).__name__}: {exc}"


class StoreAway(Exception):
    """A question with no connection to go on: the one held was lost, and the
    doubling interval has not yet come round to asking for another."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Store:
    """One store the daemon reaches, and what it holds of it: the connection it
    listens on, the connection its questions go on, and how many connections it
    has opened to it. Each of the two is asked for again on its own doubling
    interval when it is lost or refused."""

    def __init__(self, key, name: str):
        self.key = key
        self.name = name
        self.listener = None
        self.listen_error: str | None = None
        # While there is no listener: when to ask for one again, and how long
        # the wait after that attempt will be if it fails too.
        self.relisten_at: float | None = None
        self.relisten_delay = RELISTEN_FIRST_SECONDS
        self.listener_pinged = 0.0
        # The same for the connection questions go on, and when the last thing
        # went over it.
        self.query = None
        self.query_error: str | None = None
        self.requery_at: float | None = None
        self.requery_delay = RELISTEN_FIRST_SECONDS
        self.query_used = 0.0
        self.connections_opened = 0
        self.last_opened_at: str | None = None

    def opened(self) -> None:
        self.connections_opened += 1
        self.last_opened_at = now_iso()

    def row(self) -> dict:
        return {"store": self.name, "listening": self.listener is not None,
                "connections_opened": self.connections_opened,
                "last_opened_at": self.last_opened_at,
                "error": self.query_error or self.listen_error}


class StorePool:
    """The stores the daemon holds connections to, one entry per distinct store,
    so everything asked of the same store goes over the same two connections."""

    def __init__(self):
        self.stores: dict = {}

    def store(self, key, name: str) -> Store:
        if key not in self.stores:
            self.stores[key] = Store(key, name)
        return self.stores[key]

    def rows(self) -> list[dict]:
        return [store.row() for store in self.stores.values()]


class Turn:
    """One child running `tasks run <worker> --apply`."""

    def __init__(self, turn_id: str, worker: str, process: subprocess.Popen,
                 receipt: Path, output: Path, errors: Path, reason: str):
        self.id = turn_id
        self.worker = worker
        self.process = process
        self.receipt = receipt
        self.output = output
        self.errors = errors
        self.reason = reason
        self.started_at = now_iso()
        # Claiming until the child says what it took, then working. A lane with
        # a child still claiming is not asked again: the store would answer with
        # the task that child is about to take.
        self.phase = "claiming"
        self.claim: dict | None = None

    def read_receipt(self) -> bool:
        if self.claim is None:
            self.claim = _read_json(self.receipt)
            if self.claim is not None:
                self.phase = "working"
                return True
        return False

    def row(self) -> dict:
        claim = self.claim or {}
        return {"id": self.id, "worker": self.worker, "task": claim.get("task"),
                "execution": claim.get("execution"), "attempt": claim.get("attempt"),
                "pid": self.process.pid, "started_at": self.started_at,
                "phase": self.phase}

    def discard_files(self) -> None:
        for path in (self.receipt, self.output, self.errors):
            with contextlib.suppress(OSError):
                path.unlink()


class Daemon:
    """The loop. `host` answers every question about the project; `declaration`
    is what it was started with, already validated. The store the host names is
    held in `pool`, one of its own unless one is handed in."""

    def __init__(self, host, declaration: dict, *, tick: float = TICK_SECONDS,
                 pool: StorePool | None = None):
        self.host = host
        self.declaration = declaration
        self.state_dir = Path(host.state_dir)
        self.turns_dir = self.state_dir / TURNS_DIR
        self.tick = tick
        self.turns: dict[str, Turn] = {}
        self.stop_requested = False
        self.reload_requested = False
        self.stopping = False
        self.pool = pool if pool is not None else StorePool()
        self.store = self.pool.store(host.store_key(), host.store_name())
        self.notification_installed: bool | None = None
        self.next_poll = 0.0
        self.next_moment: float | None = None
        self.wakes: set[str] = {"start"}
        self.last_wake: dict | None = None
        self.held: set[str] = set()
        # Tasks a turn ended on, and when the delay before another is over.
        self.recent: dict[str, dict] = {}
        self.rotation = 0
        self.reload_error: str | None = None
        self.pause: dict = {"all": None, "lanes": {}}
        self.started_at = now_iso()
        self._lock = None
        self._published: str | None = None
        self._log_path = self.state_dir / LOG_FILE
        self._log_to_stderr = True

    # --- what it says ----------------------------------------------------------

    def log(self, message: str) -> None:
        line = log_line(self.state_dir, message)
        if self._log_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def _stderr_is_log(self) -> bool:
        """`start` hands the daemon the log file as its stderr, so a line written
        to both would be written twice; `run` under a supervisor hands it the
        supervisor's, which the log file does not replace."""
        try:
            mine, log = os.fstat(sys.stderr.fileno()), os.stat(self._log_path)
        except (OSError, ValueError, AttributeError):
            return False
        return (mine.st_dev, mine.st_ino) == (log.st_dev, log.st_ino)

    @property
    def listener(self):
        return self.store.listener

    def settings(self) -> dict:
        return self.declaration["settings"]

    def lanes(self) -> list[dict]:
        return self.declaration["lanes"]

    def status(self) -> dict:
        now = time.monotonic()
        wakes = [("poll", self.next_poll)]
        if self.next_moment is not None:
            wakes.append(("pickup", self.next_moment))
        if self.recent:
            wakes.append(("retry", min(r["until"] for r in self.recent.values())))
        reason, due = min(wakes, key=lambda item: item[1])
        listening = self.listener is not None
        listen_error = self.store.listen_error
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "project": self.host.project,
            "schema": self.host.schema,
            "fingerprint": self.declaration["fingerprint"],
            "wake_by": ("notification" if listening and self.notification_installed
                        else "poll"),
            "notification": {"channel": self.host.channel, "listening": listening,
                             "installed": self.notification_installed,
                             **({"error": listen_error} if listen_error else {})},
            "stores": self.pool.rows(),
            "poll_seconds": self.settings()["poll_seconds"],
            "max_parallel": self.settings()["max_parallel"],
            "shutdown_grace_seconds": self.settings()["shutdown_grace_seconds"],
            "retry_delay_seconds": self.settings()["retry_delay_seconds"],
            "lanes": [{"worker": lane["worker"], "max_parallel": lane["max_parallel"],
                       "running": sum(1 for t in self.turns.values()
                                      if t.worker == lane["worker"]),
                       "held_until_poll": lane["worker"] in self.held}
                      for lane in self.lanes()],
            "pause": {**self.pause, "holding": self.holding()},
            "idle": self.declaration.get("idle", []),
            "turns": [turn.row() for turn in self.turns.values()],
            "deferred": [{"task": r["task"], "task_id": tid, "until": _at(r["until"] - now)}
                         for tid, r in sorted(self.recent.items(),
                                              key=lambda item: item[1]["until"])],
            "next_wake": {"at": _at(due - now), "reason": reason},
            "last_wake": self.last_wake,
            "reload_error": self.reload_error,
            "stopping": self.stopping,
        }

    def publish(self) -> None:
        """Write the status file when anything in it moved. `next_wake.at` is
        derived from a monotonic deadline and is stable between wakes."""
        text = json.dumps(self.status(), indent=2) + "\n"
        if text != self._published:
            with contextlib.suppress(OSError):
                _write_atomic(self.state_dir / STATUS_FILE, text)
            self._published = text

    # --- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        """Take the project's one daemon slot and say which declaration holds it.

        The lock is what makes it one daemon per project; the pid and the
        fingerprint are written behind it, because they answer for the same
        process. Files left in `turns/` by a daemon that did not stop cleanly
        belong to turns nobody is watching any more: those finish, or their
        leases lapse, on their own."""
        import fcntl

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_to_stderr = not self._stderr_is_log()
        lock_path = self.state_dir / LOCK_FILE
        self._lock = lock_path.open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            self._lock = None
            raise RuntimeError(f"another tasks daemon for this project holds {lock_path}") from exc
        (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
        _write_atomic(self.state_dir / FINGERPRINT_FILE,
                      self.declaration["fingerprint"] + "\n")
        self.turns_dir.mkdir(exist_ok=True)
        left = sorted(self.turns_dir.iterdir())
        for path in left:
            with contextlib.suppress(OSError):
                path.unlink()
        self.pause = read_pause(self.state_dir)
        held = self.holding()
        self.log(f"started, pid {os.getpid()}, project {self.host.project}, "
                 f"lanes {', '.join(self._lane_words()) or 'none'}"
                 + (f"; cleared {len(left)} file(s) a previous daemon left in turns/"
                    if left else "")
                 + (f"; paused, starting no turn on {', '.join(held)}" if held else ""))
        self._connect_listener()
        self.next_poll = time.monotonic() + self.settings()["poll_seconds"]
        self.publish()

    def close(self) -> None:
        for name in (PID_FILE, FINGERPRINT_FILE, STATUS_FILE):
            with contextlib.suppress(OSError):
                (self.state_dir / name).unlink()
        self._drop_listener()
        self._drop_query()
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        self.log("stopped")

    def run(self) -> None:
        self.open()

        def stop(_signum, _frame) -> None:
            self.stop_requested = True

        def reload(_signum, _frame) -> None:
            self.reload_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, reload)
        try:
            while not self.stop_requested:
                self.step()
        finally:
            self.shutdown()
            self.close()

    def step(self, wait: bool = True) -> None:
        """One pass: take a reload, look at the children, decide whether this is
        a wake, dispatch on one, and wait for the next reason to look."""
        if self.reload_requested:
            self.reload_requested = False
            self.reload()
        self._take_up_pause()
        self.reap()
        now = time.monotonic()
        if now >= self.next_poll:
            self.wakes.add("poll")
            self.next_poll = now + self.settings()["poll_seconds"]
            # What was held back is asked again on the clock, and a listener
            # that was lost is opened again.
            self.held.clear()
            if self.listener is None:
                self._connect_listener()
            else:
                self._check_notification()
        elif self.listener is None and self.store.relisten_at is not None \
                and now >= self.store.relisten_at:
            self._connect_listener()
        if self.store.query is None and self.store.requery_at is not None \
                and now >= self.store.requery_at:
            self._reconnect()
        self._ping(time.monotonic())
        if self.next_moment is not None and now >= self.next_moment:
            self.wakes.add("pickup")
            self.next_moment = None
        over = [tid for tid, r in self.recent.items() if now >= r["until"]]
        for tid in over:
            del self.recent[tid]
        if over:
            self.wakes.add("retry")
        if self.wakes and not self.stop_requested:
            wakes, self.wakes = self.wakes, set()
            self.last_wake = {"at": now_iso(), "reasons": sorted(wakes)}
            self.dispatch(wakes)
            self._plan_next_moment()
        self.publish()
        if wait and not self.stop_requested:
            deadlines = ([self.next_poll] + ([self.next_moment] if self.next_moment else [])
                         + ([self.store.relisten_at] if self.store.relisten_at else [])
                         + ([self.store.requery_at] if self.store.requery_at else [])
                         + [r["until"] for r in self.recent.values()])
            self.wait(max(0.0, min([self.tick] + [d - time.monotonic() for d in deadlines])))

    # --- waking ----------------------------------------------------------------

    def _connect_listener(self) -> None:
        """Open the listener. A failure is said once, not on every attempt, and
        the next attempt is planned; a listener opened after one was lost or
        refused is a wake, since what the store announced meanwhile was lost."""
        store = self.store
        self._drop_listener()
        try:
            store.listener = self.host.listen()
        except (Exception, SystemExit) as exc:
            store.listener = None
            why = _why(exc)
            if why != store.listen_error:
                self.log(f"cannot listen for the store's notification, waking by the "
                         f"poll every {self.settings()['poll_seconds']}s and asking "
                         f"again: {why}")
            store.listen_error = why
            self._relisten_later()
            return
        store.opened()
        store.listener_pinged = time.monotonic()
        if store.listen_error is not None:
            self.log(f"listening on {self.host.channel} again; asking the store for work")
            self.wakes.add("relisten")
        store.listen_error = None
        store.relisten_at = None
        store.relisten_delay = RELISTEN_FIRST_SECONDS
        if store.query is None:
            # The store answers, so the questions need not wait out their own
            # interval: the next one asks for its connection at once.
            store.requery_at = None
        self._check_notification()

    def _relisten_later(self) -> None:
        store = self.store
        store.relisten_at = time.monotonic() + store.relisten_delay
        store.relisten_delay = min(store.relisten_delay * 2, RELISTEN_LONGEST_SECONDS)

    def _lose_listener(self, exc: BaseException) -> None:
        self.log(f"lost the store's notification, waking by the poll and asking "
                 f"for it again: {_why(exc)}")
        self._drop_listener()
        self.store.listen_error = _why(exc)
        self._relisten_later()

    # --- asking ----------------------------------------------------------------

    def ask(self, question, *, at_once: bool = False):
        """Put one question to the store on the connection questions go on, and
        answer what it answers. The connection is opened when there is none, unless
        the doubling interval since it was lost has not come round, in which case
        the question is refused at once; `at_once` asks for it regardless. A
        question that loses its connection, or runs past the store's timeout,
        drops it and plans the next attempt; any other failure is the question's
        own and leaves the connection held."""
        store = self.store
        if store.query is None:
            if (not at_once and store.requery_at is not None
                    and time.monotonic() < store.requery_at):
                raise StoreAway(f"no connection to the store until it answers again: "
                                f"{store.query_error}")
            self._open_query()
        conn = store.query
        try:
            answer = question(conn)
            conn.commit()
        except BaseException as exc:
            if self.host.lost(conn, exc):
                self._lose_query(exc)
            else:
                try:
                    conn.rollback()
                except Exception as gone:
                    self._lose_query(gone)
            raise
        store.query_used = time.monotonic()
        store.requery_delay = RELISTEN_FIRST_SECONDS
        return answer

    def _open_query(self) -> None:
        """Open the connection questions go on, or plan the next attempt and
        refuse the question. A failure is said once, not on every attempt."""
        store = self.store
        try:
            store.query = self.host.open_query(QUESTION_TIMEOUT_SECONDS)
        except (Exception, SystemExit) as exc:
            store.query = None
            why = _why(exc)
            if why != store.query_error:
                self.log(f"cannot connect to the store to ask it for work, asking again: "
                         f"{why}")
            store.query_error = why
            self._requery_later()
            raise StoreAway(why) from exc
        store.opened()
        store.query_used = time.monotonic()
        store.requery_at = None
        if store.query_error is not None:
            self.log("connected to the store again for its questions")
        store.query_error = None

    def _reconnect(self) -> None:
        """The interval since the question connection was lost has come round:
        open it, and ask for work, since what was asked meanwhile went
        unanswered."""
        try:
            self._open_query()
        except StoreAway:
            return
        self.wakes.add("reconnect")

    def _requery_later(self) -> None:
        store = self.store
        store.requery_at = time.monotonic() + store.requery_delay
        store.requery_delay = min(store.requery_delay * 2, RELISTEN_LONGEST_SECONDS)

    def _lose_query(self, exc: BaseException) -> None:
        store = self.store
        why = _why(exc)
        self._drop_query()
        if why != store.query_error:
            self.log(f"lost the store connection its questions go on, asking for it "
                     f"again in {store.requery_delay:g}s: {why}")
        store.query_error = why
        self._requery_later()

    def _drop_query(self) -> None:
        if self.store.query is not None:
            with contextlib.suppress(Exception):
                self.store.query.close()
        self.store.query = None

    def _ping(self, now: float) -> None:
        """A round trip on each held connection that is due one, so a proxy in
        front of the store sees it in use. A ping that fails is that connection
        lost."""
        store = self.store
        if store.listener is not None and now - store.listener_pinged >= PING_SECONDS:
            store.listener_pinged = now
            try:
                self.host.ping(store.listener)
            except Exception as exc:
                self._lose_listener(exc)
        if store.query is not None and now - store.query_used >= PING_SECONDS:
            with contextlib.suppress(Exception, SystemExit):
                self.ask(self.host.ping)

    def _check_notification(self) -> None:
        # The store is brought up to this version first, when all it lacks is
        # additive, so a store that lacked the notification gains it here. A
        # store that cannot be asked is said by the check below.
        try:
            applied = self.ask(self.host.catch_up)
        except (Exception, SystemExit):
            applied = None
        if applied:
            self.log(f"brought schema {applied['schema']} up to this version: "
                     + ", ".join(applied["created"] + applied["added"]))
        try:
            installed = bool(self.ask(self.host.notification_installed))
        except (Exception, SystemExit) as exc:
            self.log(f"cannot tell whether the store notifies: {_why(exc)}")
            return
        if installed != self.notification_installed:
            if installed and self.listener is not None:
                self.log(f"listening on {self.host.channel}; the poll every "
                         f"{self.settings()['poll_seconds']}s is the fallback")
            elif not installed:
                self.log("the store has no claimable notification, so this daemon "
                         f"wakes by the poll every {self.settings()['poll_seconds']}s; "
                         "`tasks migrate --apply` adds it")
        self.notification_installed = installed

    def _drop_listener(self) -> None:
        if self.store.listener is not None:
            with contextlib.suppress(Exception):
                self.store.listener.close()
        self.store.listener = None

    def wait(self, seconds: float) -> None:
        """Sleep until the store notifies or `seconds` pass. A notification for
        another project or another schema on the same database is not a wake."""
        if self.listener is None:
            time.sleep(seconds)
            return
        try:
            got = list(self.listener.notifies(timeout=seconds, stop_after=1))
            if got:
                got += list(self.listener.notifies(timeout=0))
        except Exception as exc:
            self._lose_listener(exc)
            return
        for note in got:
            try:
                payload = json.loads(note.payload)
            except (ValueError, AttributeError):
                continue
            if (isinstance(payload, dict) and payload.get("project") == self.host.project
                    and payload.get("schema") == self.host.schema):
                self.wakes.add("notify")
                recent = self.recent.get(str(payload.get("task")))
                if recent is not None:
                    # Not dropped: the delay's own wake asks for it again.
                    self.log(f"task {recent['task']} is claimable again; deferred "
                             f"to {_at(recent['until'] - time.monotonic())}")

    def _plan_next_moment(self) -> None:
        try:
            seconds = self.ask(self.host.next_moment)
        except (Exception, SystemExit) as exc:
            self.log(f"cannot read the next pickup from the store: {_why(exc)}")
            return
        self.next_moment = (time.monotonic() + max(0.0, float(seconds)) + 0.5
                            if seconds is not None else None)

    # --- the pause -------------------------------------------------------------

    def holding(self) -> list[str]:
        return holding(self.pause, [lane["worker"] for lane in self.lanes()])

    def _take_up_pause(self) -> None:
        """Read the pause and act on what moved: a lane newly held starts no
        turn from now on, and a lane let go is a wake."""
        found = read_pause(self.state_dir)
        if found == self.pause:
            return
        before = set(self.holding())
        self.pause = found
        after = self.holding()
        lifted = sorted(before - set(after))
        if after and set(after) != before:
            running = sum(1 for t in self.turns.values() if t.worker in after)
            self.log(f"paused: starting no turn on {', '.join(after)}"
                     + (f"; {running} running turn(s) left to finish" if running else ""))
        if lifted:
            self.log(f"resumed: {', '.join(lifted)} start turns again")
            self.wakes.add("resume")

    # --- turns -----------------------------------------------------------------

    def _lane_words(self) -> list[str]:
        return [f"{lane['worker']} (max {lane['max_parallel']})" for lane in self.lanes()]

    def _room(self, lane: dict) -> bool:
        mine = [t for t in self.turns.values() if t.worker == lane["worker"]]
        return (lane["worker"] not in self.held
                and lane["worker"] not in self.holding()
                and len(mine) < lane["max_parallel"]
                and not any(t.phase == "claiming" for t in mine))

    def excluded(self) -> tuple[str, ...]:
        """The tasks no turn of this daemon may claim now: those a turn holds and
        those a turn ended on within the retry delay."""
        held = {str(t.claim["task_id"]) for t in self.turns.values()
                if t.claim and t.claim.get("task_id")}
        return tuple(sorted(held | set(self.recent)))

    def dispatch(self, wakes: set[str]) -> None:
        """Start a turn for every lane with room and work, while the cap across
        every lane allows. Lanes are asked in a rotating order, so one lane's
        work cannot keep another's waiting behind the shared cap for ever."""
        lanes = self.lanes()
        if not lanes:
            return
        free = self.settings()["max_parallel"] - len(self.turns)
        start = self.rotation % len(lanes)
        self.rotation += 1
        started = False
        exclude = self.excluded()
        for lane in lanes[start:] + lanes[:start]:
            if free <= 0:
                break
            if not self._room(lane):
                continue
            try:
                has_work = self.ask(lambda conn, spec=lane["spec"]:
                                    self.host.lane_has_work(conn, spec, exclude))
            except (Exception, SystemExit) as exc:
                self.log(f"cannot ask the store for {lane['worker']}'s work: {_why(exc)}")
                return
            if has_work:
                self.spawn(lane["worker"], "work", exclude)
                free -= 1
                started = True
        if started or free <= 0 or not (wakes & _SWEEP_WAKES):
            return
        try:
            due = self.ask(self.host.sweep_due)
        except (Exception, SystemExit) as exc:
            self.log(f"cannot ask the store what a claim would put back: {_why(exc)}")
            return
        if due:
            lane = next((lane for lane in lanes if self._room(lane)), None)
            if lane is not None:
                self.spawn(lane["worker"], "sweep", exclude)

    def spawn(self, worker: str, reason: str, exclude: tuple = ()) -> Turn:
        turn_id = uuid.uuid4().hex[:12]
        receipt = self.turns_dir / f"{turn_id}.claim.json"
        output = self.turns_dir / f"{turn_id}.out"
        errors = self.turns_dir / f"{turn_id}.err"
        env = dict(self.host.turn_env())
        env[RECEIPT_ENV] = str(receipt)
        if exclude:
            env[EXCLUDE_ENV] = ",".join(exclude)
        with output.open("w") as out, errors.open("w") as err:
            process = subprocess.Popen(
                self.host.turn_command(worker), cwd=str(self.host.root), env=env,
                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True)
        turn = Turn(turn_id, worker, process, receipt, output, errors, reason)
        self.turns[turn_id] = turn
        self.log(f"turn {turn_id} started: worker {worker}, pid {process.pid}"
                 + (", to let its claim put back what is due" if reason == "sweep" else ""))
        return turn

    def reap(self) -> None:
        for turn in list(self.turns.values()):
            if turn.read_receipt():
                self.log(f"turn {turn.id} claimed {turn.claim.get('task')} "
                         f"(attempt {turn.claim.get('attempt')})")
                if not self.stopping:
                    self.wakes.add("claimed")
            code = turn.process.poll()
            if code is None:
                continue
            self._finish(turn, code)

    def _finish(self, turn: Turn, code: int) -> None:
        turn.read_receipt()
        del self.turns[turn.id]
        if turn.claim and turn.claim.get("task_id"):
            delay = self.settings()["retry_delay_seconds"]
            self.recent[str(turn.claim["task_id"])] = {
                "task": turn.claim.get("task"), "until": time.monotonic() + delay}
        try:
            answer = _last_json(turn.output.read_text(errors="replace"))
            trouble = turn.errors.read_text(errors="replace").strip()
        except OSError:
            answer, trouble = None, ""
        said = []
        if answer:
            for key in ("claimed", "parked", "returned_unspent", "waiting", "handoff",
                        "release_refused"):
                if answer.get(key) not in (None, False):
                    said.append(f"{key} {answer[key]}" if key in ("claimed", "release_refused")
                                else key)
            if answer.get("claimed") is None:
                said.append("claimed nothing")
            # What the worker's own hooks said: each task its `before` hook held
            # back, and an `after` hook that failed.
            for held in answer.get("passed_over") or []:
                if not isinstance(held, dict):
                    continue
                words = f"passed over {held.get('task')}: {held.get('verdict')}"
                if held.get("until"):
                    words += f" until {held['until']}"
                if held.get("why"):
                    words += f" ({held['why']})"
                if held.get("said"):
                    words += f", said {str(held['said'])[:200]}"
                said.append(words)
            after = answer.get("after_hook")
            if isinstance(after, dict) and after.get("exit") != 0:
                said.append(f"after hook failed ({after.get('why')})"
                            + (f", said {str(after['said'])[:200]}" if after.get("said")
                               else ""))
        if trouble:
            said.append(f"said {trouble.splitlines()[-1][:300]}")
        if turn.claim and turn.claim.get("task_id"):
            said.append(f"{turn.claim.get('task')} not run again before "
                        f"{_at(self.settings()['retry_delay_seconds'])}")
        self.log(f"turn {turn.id} ended: worker {turn.worker}, exit {code}"
                 + (f", {', '.join(said)}" if said else ""))
        if turn.claim is None and turn.reason == "work" and not self.stopping:
            # The store said this lane had work and the claim took none, or the
            # command failed before claiming. Asking again at once would ask the
            # same question of the same answer, so the lane waits for the poll.
            self.held.add(turn.worker)
            self.log(f"lane {turn.worker} waits for the next poll: its turn took nothing")
        turn.discard_files()
        if not self.stopping:
            self.wakes.add("turn_ended")

    # --- reload and stop -------------------------------------------------------

    def reload(self) -> None:
        """Take up the declaration on disk, or keep the one running.

        It is read and validated whole before anything is replaced, so an edit
        that does not load leaves this daemon dispatching exactly what it had.
        Turns already running are children held by id and the swap does not
        touch them. The published fingerprint moves last, and only on success."""
        try:
            declaration = self.host.load()
        except (Exception, SystemExit) as exc:
            self.reload_error = _why(exc)
            self.log(f"reload rejected, keeping the declaration loaded: {self.reload_error}")
            return
        self.declaration = declaration
        self.reload_error = None
        self.held.clear()
        self.next_poll = min(self.next_poll,
                             time.monotonic() + self.settings()["poll_seconds"])
        _write_atomic(self.state_dir / FINGERPRINT_FILE, declaration["fingerprint"] + "\n")
        self.wakes.add("reload")
        self.log(f"reloaded: lanes {', '.join(self._lane_words()) or 'none'}")

    def shutdown(self) -> None:
        """Stop claiming, give running turns the grace period, then end what is
        left and settle each raise it held the way a lapsed lease is settled."""
        self.stopping = True
        grace = self.settings()["shutdown_grace_seconds"]
        if self.turns:
            self.log(f"stopping: waiting up to {grace}s for {len(self.turns)} turn(s)")
        self.publish()
        deadline = time.monotonic() + grace
        while self.turns and time.monotonic() < deadline:
            self.reap()
            if self.turns:
                time.sleep(0.2)
        for turn in list(self.turns.values()):
            turn.read_receipt()
            with contextlib.suppress(Exception):
                self.host.kill_tree([turn.process.pid])
            with contextlib.suppress(Exception):
                turn.process.wait(timeout=10)
            del self.turns[turn.id]
            task = (turn.claim or {}).get("task")
            if turn.claim and turn.claim.get("execution"):
                try:
                    self.ask(lambda conn, execution=turn.claim["execution"]:
                             self.host.settle_cut_off(conn, execution), at_once=True)
                    settled = "settled as a lapsed lease"
                except (Exception, SystemExit) as exc:
                    settled = f"not settled, so its lease lapses on its own: {_why(exc)}"
            else:
                settled = ("it had not reported a claim; anything it took is freed "
                           "when its lease lapses")
            self.log(f"turn {turn.id} cut off after {grace}s: worker {turn.worker}"
                     + (f", task {task}" if task else "") + f", {settled}")
            turn.discard_files()
        self.publish()
