"""The tasks service: a dispatcher that runs the conveyor on its own for a set
of projects, one slot each. In project mode the set is one project, the one the
daemon was started in. In machine mode one process serves every project on the
machine's opt-in list: the set follows the list, a project joining or leaving
is noticed on the next pass, and a project that cannot be served is reported
with the reason while the others are served. The machine process owns only
what is the machine's - its store connections, an optional cap on turns
running at once across every project and the order projects are served in
under it - and every turn it starts is the project's, as project mode starts
it.

Every enabled worker of a project that takes something is a lane. Whenever a
lane has a free slot and the store holds work it would take, the daemon starts
one child process running `tasks run <worker> --apply` in that project - the
same claim, frame, profile, turn and settlement a person gets from that command
- and counts it against the lane's cap and the cap across every lane of the
project.

The daemon decides nothing about a task. It asks the store whether a lane would
take something, starts the command that takes it, and reads what the command
wrote back. Everything it knows about a project arrives through the host the
executable hands it for that project, and every question about the project is
asked inside that host's scope, so this file holds the loop, the processes and
the files the daemon publishes, and nothing about workers, settings or the
store's shape. Before it asks the store anything for a project at a wake, and
again before it starts a turn there, it asks the host whether the project may
be served at all, and while it may not it asks and starts nothing for it.

It wakes when the store notifies that a task became claimable, at every poll,
at the earliest moment a pickup or a lease falls due, and when one of its own
turns claims or ends. A store without the notification leaves it the poll.

It holds its connections to the store rather than opening one per question:
one it listens on, and one every question it asks is put to, each question cut
off by the store when it runs past `QUESTION_TIMEOUT_SECONDS`. A question or a
round trip whose answer is not back within `ROUND_TRIP_SECONDS` has its
connection severed and counted lost, so a connection the path dropped under it
holds the loop, and a stop, no longer than that. They are kept per
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

Its turns outlive it. Each runs in a session of its own with its output on
files, and settles its own task, so however the daemon stops - a stop, a
signal, a crash - it stops claiming, publishes, lets go of its lock and leaves
every running turn running. Every spawn writes a turn record in `turns/`
naming the process and when it started, and the next daemon to take the
project's lock adopts every turn whose process is still the one recorded. The
one act that ends running turns is a stop that asks for it: the stop writes an
end-turns intent beside the pid before it signals, and a daemon that finds a
fresh one gives its turns their grace, then ends the rest and settles what
they held.

It is a second heartbeat for its turns. On the beat a turn renews its own
lease, and again before it starts any turn, whose claim sweeps lapsed leases,
it renews the lease of the raise each of its live turns reported, so a lease
lapses only when both the turn and the daemon are gone.
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
END_TURNS_FILE = "end-turns.json"
TURNS_DIR = "turns"
RECEIPT_ENV = "TASKS_TURN_RECEIPT"
EXCLUDE_ENV = "TASKS_TURN_EXCLUDE"

# The tag on every line the machine process writes about itself, its stores or
# its cap; a line about one project carries that project's slug.
MACHINE_TAG = "machine"

# The longest the machine process goes without publishing its status, so a
# probe can tell a process that is alive from one that hangs.
PUBLISH_SECONDS = 30.0

# How often a project on the list that cannot be served yet is asked again
# whether it can: its folder, its declaration of itself, its enable, its
# connection and its lock.
ADMIT_SECONDS = 1.0

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

# The longest a question, or a round trip of the daemon's own, waits for its
# answer before its connection is severed and counted lost. The store cuts off
# a question that runs long before this, so it is reached only when no answer
# can arrive: a path that dropped under a connection the store never closed.
ROUND_TRIP_SECONDS = QUESTION_TIMEOUT_SECONDS + 15

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


def process_started(pid: int | None) -> str | None:
    """When the process `pid` started, as `ps -o lstart=` prints it under the C
    locale, or None when there is no such process or `ps` cannot say. With the
    pid it names one process: a pid used again later starts at another moment."""
    if not pid or pid <= 0:
        return None
    try:
        found = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    said = " ".join(found.stdout.split())
    return said if found.returncode == 0 and said else None


def write_end_turns(state_dir: Path, by: str, timeout_seconds: float) -> dict:
    """Write the intent that the next stop ends running turns: who asked, when,
    and how long the stop that asked waits, past which the intent is stale."""
    intent = {"by": by, "at": now_iso(), "timeout_seconds": timeout_seconds}
    path = Path(state_dir) / END_TURNS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(intent, indent=2) + "\n")
    return intent


def end_turns_age(intent: dict) -> float | None:
    """Seconds since the intent was written, or None when it names no moment."""
    try:
        at = datetime.datetime.fromisoformat(str(intent.get("at")))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - at).total_seconds()


def fresh_end_turns(state_dir: Path, default_limit: float, say) -> dict | None:
    """The end-turns intent in `state_dir` when it is fresh: written no longer
    ago than the stop that wrote it waits, or `default_limit` when it does not
    say. One older than that is stale - the stop it came with is long over - so
    it is said through `say`, removed and ignored, and cannot turn a later
    restart into an ending."""
    path = Path(state_dir) / END_TURNS_FILE
    found = _read_json(path)
    if found is None:
        return None
    age = end_turns_age(found)
    limit = found.get("timeout_seconds")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        limit = default_limit
    if age is None or age > limit:
        say(f"ignored the end-turns intent {found.get('by')} wrote at "
            f"{found.get('at')}: older than the {limit:g}s its stop waits; "
            "running turns are left running")
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return found


def take_lock(state_dir: Path):
    """The one-process lock in `state_dir`, taken without waiting: the open
    file holding it, or None while another process holds it."""
    import fcntl

    Path(state_dir).mkdir(parents=True, exist_ok=True)
    handle = (Path(state_dir) / LOCK_FILE).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _write_atomic(path: Path, text: str) -> None:
    spare = path.with_name(f".{path.name}.{os.getpid()}")
    spare.write_text(text)
    os.replace(spare, path)


def log_line(state_dir: Path, project: str, message: str) -> str:
    """Append one line to the service log, as the daemon writes it, and return
    it: `<moment> tasks service [<project>]: <message>`, the project named by
    its slug. A message that spans lines, as a store's error may, is written
    on one, so every line of the log carries its project. A verb that changes
    the daemon's runtime state records it here too, whether or not a daemon
    runs."""
    message = " ".join(part.strip() for part in str(message).splitlines() if part.strip())
    line = f"{now_iso()} tasks service [{project}]: {message}\n"
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


def _reason(exc: BaseException) -> str:
    """A refusal as a status reason, `code: sentence`, the way every other
    reason a slot carries reads; anything else as `_why` says it."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and isinstance(getattr(exc, "message", None), str):
        return f"{code}: {exc.message}"
    return _why(exc)


class StoreAway(Exception):
    """A question with no connection to go on: the one held was lost, and the
    doubling interval has not yet come round to asking for another."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Store:
    """One store the dispatcher reaches, and what it holds of it: the connection it
    listens on, the connection its questions go on, and how many connections it
    has opened to it. Each of the two is asked for again on its own doubling
    interval when it is lost or refused."""

    def __init__(self, key, name: str, host=None):
        self.key = key
        self.name = name
        # The host whose connection entry opens this store's connections.
        self.host = host
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

    def store(self, key, name: str, host=None) -> Store:
        if key not in self.stores:
            self.stores[key] = Store(key, name, host)
        elif self.stores[key].host is None:
            self.stores[key].host = host
        return self.stores[key]

    def rows(self) -> list[dict]:
        return [store.row() for store in self.stores.values()]


class Turn:
    """One process running `tasks run <worker> --apply`: a child this daemon
    started, held by its `process`, or a turn a daemon before it started and
    this one adopted, which has no `process` and is watched by its pid."""

    def __init__(self, turn_id: str, worker: str, process: subprocess.Popen | None,
                 receipt: Path, output: Path, errors: Path, reason: str,
                 project: str | None = None, *, pid: int | None = None,
                 started_at: str | None = None, lstart: str | None = None,
                 record: Path | None = None):
        self.id = turn_id
        self.project = project
        self.worker = worker
        self.process = process
        self.pid = process.pid if process is not None else pid
        self.adopted = process is None
        self.receipt = receipt
        self.output = output
        self.errors = errors
        self.record = record
        self.reason = reason
        self.started_at = started_at or now_iso()
        # When the process started, as `ps -o lstart=` prints it: with the pid,
        # what tells this turn's process from another given its pid later.
        self.lstart = lstart
        # Claiming until the child says what it took, then working. A lane with
        # a child still claiming is not asked again: the store would answer with
        # the task that child is about to take.
        self.phase = "claiming"
        self.claim: dict | None = None

    def ended(self) -> tuple[bool, int | None]:
        """Whether the turn's process has ended, and its exit code: always None
        for an adopted turn, which this daemon cannot wait on."""
        if self.process is not None:
            code = self.process.poll()
            return code is not None, code
        try:
            # A turn adopted from a daemon that ran in this same process is
            # still its child; anywhere else the pid answers.
            reaped, _status = os.waitpid(self.pid, os.WNOHANG)
            if reaped == self.pid:
                return True, None
        except ChildProcessError:
            pass
        except OSError:
            return True, None
        return not pid_alive(self.pid), None

    def still_recorded(self) -> bool:
        """Whether the pid is still the process this turn was recorded as."""
        return bool(self.lstart) and process_started(self.pid) == self.lstart

    def record_text(self) -> str:
        return json.dumps({"id": self.id, "project": self.project, "worker": self.worker,
                           "pid": self.pid, "lstart": self.lstart,
                           "receipt": str(self.receipt), "output": str(self.output),
                           "errors": str(self.errors), "started_at": self.started_at},
                          indent=2) + "\n"

    def read_receipt(self) -> bool:
        if self.claim is None:
            self.claim = _read_json(self.receipt)
            if self.claim is not None:
                self.phase = "working"
                return True
        return False

    def row(self) -> dict:
        claim = self.claim or {}
        return {"project": self.project, "id": self.id, "worker": self.worker, "task": claim.get("task"),
                "execution": claim.get("execution"), "attempt": claim.get("attempt"),
                "pid": self.pid, "started_at": self.started_at,
                "adopted": self.adopted, "phase": self.phase}

    def discard_files(self) -> None:
        for path in (self.receipt, self.output, self.errors, self.record):
            if path is None:
                continue
            with contextlib.suppress(OSError):
                path.unlink()


class ProjectSlot:
    """One project the dispatcher serves, and everything held for it: its
    declaration and fingerprint, its wakes, its poll and pickup deadlines, the
    lanes held until the poll and the tasks held back by the retry delay, the
    lane rotation, the pause, its turns, its lock and its state root.

    `host` answers every question about the project; `declaration` is what the
    slot was started with, already validated. Every question the slot asks of
    its host runs inside the host's scope for that project, opened from the
    dispatcher's start environment, so nothing one project resolves is seen by
    another. Before it asks the store anything at a wake, and again before it
    starts a turn, the slot asks the host whether the project may be served at
    all; while it may not, the slot asks and starts nothing."""

    def __init__(self, dispatcher: "Dispatcher", host, declaration: dict, *,
                 machine: bool = False):
        self.dispatcher = dispatcher
        self.host = host
        self.declaration = declaration
        # Machine mode adds the refusals only a machine process needs; a slot
        # in project mode resolves its project exactly as the project's own
        # commands do.
        self.machine = machine
        self.slug = host.slug
        self.state_dir = Path(host.state_dir)
        self.turns_dir = self.state_dir / TURNS_DIR
        self.turns: dict[str, Turn] = {}
        self.store = dispatcher.pool.store(host.store_key(), host.store_name(), host)
        self.notification_installed: bool | None = None
        self.next_poll = 0.0
        self.next_moment: float | None = None
        # When the leases of its running turns are next renewed, and why the
        # last renewal failed.
        self.next_renew = 0.0
        self.renew_error: str | None = None
        self.wakes: set[str] = {"start"}
        self.last_wake: dict | None = None
        self.held: set[str] = set()
        # Tasks a turn ended on, and when the delay before another is over.
        self.recent: dict[str, dict] = {}
        self.rotation = 0
        self.reload_error: str | None = None
        self.pause: dict = {"all": None, "lanes": {}}
        # Why the project may not be served, as ("refused" | "error", reason),
        # from the last check; and that check's answer for this pass, None
        # until it is asked.
        self.refusal: tuple[str, str] | None = None
        self.checked: bool | None = None
        # Why the declaration has never loaded, for a machine slot whose first
        # load failed: it holds its lock and serves nothing until a reload
        # takes a declaration.
        self.load_error: str | None = None
        # Had a wake and room when the machine cap bound: kept until any turn
        # ends, then offered again.
        self.waiting_for_cap = False
        self.started_at = now_iso()
        self._lock = None
        self._published: str | None = None
        self._log_path = self.state_dir / LOG_FILE
        self._log_to_stderr = True

    # --- what it says ----------------------------------------------------------

    def scope(self):
        """The host's scope for this project, opened from the start environment."""
        return self.host.scope(self.dispatcher.environment)

    def log(self, message: str) -> None:
        self.dispatcher.echo(self, log_line(self.state_dir, self.slug, message))

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

    @property
    def stopping(self) -> bool:
        return self.dispatcher.stopping

    def settings(self) -> dict:
        return self.declaration["settings"]

    def lanes(self) -> list[dict]:
        return self.declaration["lanes"]

    def state(self) -> str:
        """`refused` or `error` while the project may not be served, `paused`
        while the pause holds every lane, and `served` otherwise. A machine
        slot is also `error` while its declaration has never loaded and while
        its store is away."""
        return self._state_and_reason()[0]

    def _state_and_reason(self) -> tuple[str, str | None]:
        if self.load_error is not None:
            return "error", self.load_error
        if self.refusal is not None:
            return self.refusal
        away = self.store.query_error or self.store.listen_error
        if self.machine and away:
            return "error", f"store_away: {away}"
        lanes = [lane["worker"] for lane in self.lanes()]
        if lanes and set(self.holding()) == set(lanes):
            return "paused", None
        return "served", None

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
        state, why = self._state_and_reason()
        # A machine slot says so, and whether the machine cap holds it back;
        # a project-mode status carries neither.
        machine = ({"mode": "machine", "machine_pid": os.getpid(),
                    "waiting_for_cap": self.waiting_for_cap} if self.machine else {})
        return {
            **machine,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "project": self.host.project,
            "schema": self.host.schema,
            "state": state,
            "reason": why,
            "fingerprint": self.declaration["fingerprint"],
            "wake_by": ("notification" if listening and self.notification_installed
                        else "poll"),
            "notification": {"channel": self.host.channel, "listening": listening,
                             "installed": self.notification_installed,
                             **({"error": listen_error} if listen_error else {})},
            "stores": [self.store.row()],
            "poll_seconds": self.settings()["poll_seconds"],
            "max_parallel": self.settings()["max_parallel"],
            "shutdown_grace_seconds": self.settings()["shutdown_grace_seconds"],
            "retry_delay_seconds": self.settings()["retry_delay_seconds"],
            "lanes": [{"project": self.slug, "worker": lane["worker"],
                       "max_parallel": lane["max_parallel"],
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

    def open(self, lock=None) -> None:
        """Take the project's one daemon slot and say which declaration holds it.

        The lock is what makes it one daemon per project, in either mode; the
        pid and the fingerprint are written behind it, because they answer for
        the same process. A machine slot is handed the lock it already took. A
        slot whose declaration never loaded publishes no fingerprint, so a
        reload in the project always reaches it. Then it takes up the turns a
        daemon before it left running: every turn record in `turns/` whose pid
        is alive and started at the recorded moment is adopted, and every other
        is a turn that ended while nothing watched it, said in the log and
        removed. Files in `turns/` that no record names are cleared."""
        import fcntl

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_to_stderr = not self._stderr_is_log()
        lock_path = self.state_dir / LOCK_FILE
        if lock is not None:
            self._lock = lock
        else:
            self._lock = lock_path.open("a+")
            try:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._lock.close()
                self._lock = None
                raise RuntimeError(
                    f"another tasks daemon for this project holds {lock_path}") from exc
        (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
        if self.declaration["fingerprint"] is None:
            with contextlib.suppress(OSError):
                (self.state_dir / FINGERPRINT_FILE).unlink()
        else:
            _write_atomic(self.state_dir / FINGERPRINT_FILE,
                          self.declaration["fingerprint"] + "\n")
        self.turns_dir.mkdir(exist_ok=True)
        adopted, gone = self._read_records()
        named = {path for turn in adopted + gone
                 for path in (turn.record, turn.receipt, turn.output, turn.errors)}
        # A receipt being written lands under a spare name first.
        spares = tuple(f".{turn.receipt.name}." for turn in adopted)
        left = [path for path in sorted(self.turns_dir.iterdir())
                if path not in named and not path.name.startswith(spares)]
        for path in left:
            with contextlib.suppress(OSError):
                path.unlink()
        self.pause = read_pause(self.state_dir)
        held = self.holding()
        self.log(f"started, pid {os.getpid()}, project {self.host.project}, "
                 f"lanes {', '.join(self._lane_words()) or 'none'}"
                 + (f"; adopted {len(adopted)} running turn(s)" if adopted else "")
                 + (f"; cleared {len(left)} file(s) in turns/ that no turn record names"
                    if left else "")
                 + (f"; paused, starting no turn on {', '.join(held)}" if held else ""))
        for turn in adopted:
            self.turns[turn.id] = turn
            turn.read_receipt()
            self.log(f"turn {turn.id} adopted: worker {turn.worker}, pid {turn.pid}, "
                     f"started {turn.started_at}"
                     + (f", holding {turn.claim.get('task')} (attempt "
                        f"{turn.claim.get('attempt')})" if turn.claim else ", not yet claimed"))
        for turn in gone:
            turn.read_receipt()
            said = self._said(turn)
            self.log(f"turn {turn.id} ended while unwatched: worker {turn.worker}, "
                     f"pid {turn.pid}, exit unknown" + (f", {', '.join(said)}" if said else ""))
            turn.discard_files()

    def _read_records(self) -> tuple[list[Turn], list[Turn]]:
        """The turns the records in `turns/` name: those whose process is still
        the one recorded, and those whose process is gone - ended, or its pid
        now another process's. A record that cannot be read names no turn."""
        adopted, gone = [], []
        for path in sorted(self.turns_dir.glob("*.json")):
            if path.name.count(".") != 1 or path.name.startswith("."):
                continue  # a receipt, `<id>.claim.json`, or a spare
            found = _read_json(path)
            try:
                turn = Turn(str(found["id"]), str(found["worker"]), None,
                            Path(found["receipt"]), Path(found["output"]),
                            Path(found["errors"]), "adopted",
                            found.get("project") or self.slug, pid=int(found["pid"]),
                            started_at=found.get("started_at"),
                            lstart=found.get("lstart"), record=path)
            except (TypeError, KeyError, ValueError):
                continue
            (adopted if turn.still_recorded() else gone).append(turn)
        return adopted, gone

    def unpublish(self) -> None:
        for name in (PID_FILE, FINGERPRINT_FILE, STATUS_FILE):
            with contextlib.suppress(OSError):
                (self.state_dir / name).unlink()

    def release(self, said: str = "stopped") -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        self.log(said)

    # --- may it be served ------------------------------------------------------

    def served(self, fresh: bool = False) -> bool:
        """Whether the project may be served now, asked of the host inside the
        project's scope: once per pass at the first wake that would ask the
        store anything, and afresh when `fresh`, which is before every turn.
        A change of answer is said once in the log."""
        if self.load_error is not None:
            # Nothing to serve it by until a reload takes a declaration.
            self.checked = False
            return False
        if self.checked is not None and not fresh:
            return self.checked
        try:
            # A machine slot first asks whether its project is still on the
            # opt-in list; leaving it is the project's own act.
            found = self.dispatcher.listed(self) if self.machine else None
            if found is None:
                with self.scope():
                    found = self.host.recheck(self.machine)
        except (Exception, SystemExit) as exc:
            found = ("error", _why(exc))
        if found != self.refusal:
            if found is not None:
                self.log(f"{found[0]}: {found[1]}; asking the store nothing and starting "
                         "no turn for this project until a later check passes")
            else:
                self.log("served again: the check before every action passes")
        self.refusal = found
        self.checked = found is None
        return self.checked

    # --- asking ----------------------------------------------------------------

    def ask(self, question, *, at_once: bool = False):
        """Put one question about this project to its store, inside its scope."""
        return self.dispatcher.ask(self.store, question, scope=self.scope, at_once=at_once)

    def check_notification(self, catch_up: bool = True) -> None:
        # The store is brought up to this version first, when all it lacks is
        # additive, so a store that lacked the notification gains it here. A
        # store that cannot be asked is said by the check below. When a
        # listener opens, a schema several slots share is brought up once.
        try:
            applied = self.ask(self.host.catch_up) if catch_up else None
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

    def heard(self, payload) -> bool:
        """Whether a notification is this project's: its schema and its id."""
        return (isinstance(payload, dict) and payload.get("project") == self.host.project
                and payload.get("schema") == self.host.schema)

    def notified(self, payload: dict) -> None:
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

    def deadlines(self) -> list[float]:
        return ([self.next_poll] + ([self.next_moment] if self.next_moment else [])
                + ([self.next_renew] if any(t.claim for t in self.turns.values()) else [])
                + [r["until"] for r in self.recent.values()])

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

    def dispatch(self, wakes: set[str], limit: int | None = None) -> int:
        """Start a turn for every lane with room and work, while the cap across
        every lane allows, and at most `limit` when the machine cap deals turns
        one at a time; how many started. Lanes are asked in a rotating order,
        so one lane's work cannot keep another's waiting behind the shared cap
        for ever."""
        lanes = self.lanes()
        if not lanes:
            return 0
        free = self.settings()["max_parallel"] - len(self.turns)
        if limit is not None:
            free = min(free, limit)
        start = self.rotation % len(lanes)
        self.rotation += 1
        started = 0
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
                return started
            if has_work:
                if self.spawn(lane["worker"], "work", exclude) is None:
                    return started
                free -= 1
                started += 1
        if started or free <= 0 or not (wakes & _SWEEP_WAKES):
            return started
        try:
            due = self.ask(self.host.sweep_due)
        except (Exception, SystemExit) as exc:
            self.log(f"cannot ask the store what a claim would put back: {_why(exc)}")
            return started
        if due:
            lane = next((lane for lane in lanes if self._room(lane)), None)
            if lane is not None and self.spawn(lane["worker"], "sweep", exclude) is not None:
                started += 1
        return started

    def renew(self, at_once: bool = False) -> None:
        """The second heartbeat: renew the lease of the raise each running turn
        holds, on the beat a turn renews its own, so a lease lapses only when
        both the turn and this daemon are gone. `at_once` renews whether or not
        the beat is due: every turn's claim sweeps lapsed leases, so one is
        asked for before every turn starts. Only a turn whose process is still
        the one recorded, and which has reported its claim, is renewed for."""
        now = time.monotonic()
        if not at_once and now < self.next_renew:
            return
        held = [(turn.worker, turn.claim) for turn in self.turns.values()
                if turn.claim and turn.claim.get("execution") and not turn.ended()[0]
                and (not turn.adopted or turn.still_recorded())]
        if not held or not self.served():
            return
        try:
            answer = self.ask(lambda conn: self.host.renew_leases(conn, held))
        except (Exception, SystemExit) as exc:
            why = _why(exc)
            if why != self.renew_error:
                self.log(f"cannot renew the leases of its running turns: {why}")
            self.renew_error = why
            self.next_renew = now + RELISTEN_FIRST_SECONDS
            return
        if self.renew_error is not None:
            self.log("renews the leases of its running turns again")
        self.renew_error = None
        self.next_renew = now + float(answer["every"])

    def spawn(self, worker: str, reason: str, exclude: tuple = ()) -> Turn | None:
        """Start one turn, unless the check taken again just before it refuses
        the project, in which case nothing is started."""
        if not self.served(fresh=True):
            return None
        self.renew(at_once=True)
        turn_id = uuid.uuid4().hex[:12]
        receipt = self.turns_dir / f"{turn_id}.claim.json"
        output = self.turns_dir / f"{turn_id}.out"
        errors = self.turns_dir / f"{turn_id}.err"
        # What every turn starts from is the dispatcher's start environment,
        # never the environment a scope left behind.
        env = dict(self.host.turn_env(self.dispatcher.environment))
        env[RECEIPT_ENV] = str(receipt)
        if exclude:
            env[EXCLUDE_ENV] = ",".join(exclude)
        with output.open("w") as out, errors.open("w") as err:
            process = subprocess.Popen(
                self.host.turn_command(worker), cwd=str(self.host.root), env=env,
                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True, close_fds=True)
        turn = Turn(turn_id, worker, process, receipt, output, errors, reason, self.slug,
                    lstart=process_started(process.pid),
                    record=self.turns_dir / f"{turn_id}.json")
        # The record is what lets the next daemon adopt this turn if this one
        # stops first; one that cannot be written leaves the turn unadoptable.
        with contextlib.suppress(OSError):
            _write_atomic(turn.record, turn.record_text())
        self.turns[turn_id] = turn
        self.log(f"turn {turn_id} started: worker {worker}, pid {process.pid}"
                 + (", to let its claim put back what is due" if reason == "sweep" else ""))
        return turn

    def reap(self) -> int:
        """Read what each turn reported and let go of those that ended; how
        many ended."""
        ended_now = 0
        for turn in list(self.turns.values()):
            if turn.read_receipt():
                self.log(f"turn {turn.id} claimed {turn.claim.get('task')} "
                         f"(attempt {turn.claim.get('attempt')})")
                if not self.stopping:
                    self.wakes.add("claimed")
            ended, code = turn.ended()
            if not ended:
                continue
            self._finish(turn, code)
            ended_now += 1
        return ended_now

    def _finish(self, turn: Turn, code: int | None) -> None:
        """Say how a turn ended and let go of it. An adopted turn's exit code is
        not this daemon's to read, so it is unknown; what it answered is read
        from its output file either way."""
        turn.read_receipt()
        del self.turns[turn.id]
        if turn.claim and turn.claim.get("task_id"):
            delay = self.settings()["retry_delay_seconds"]
            self.recent[str(turn.claim["task_id"])] = {
                "task": turn.claim.get("task"), "until": time.monotonic() + delay}
        said = self._said(turn)
        if turn.claim and turn.claim.get("task_id"):
            said.append(f"{turn.claim.get('task')} not run again before "
                        f"{_at(self.settings()['retry_delay_seconds'])}")
        self.log(f"turn {turn.id} ended: worker {turn.worker}, "
                 f"exit {'unknown' if code is None else code}"
                 + (", adopted" if turn.adopted else "")
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

    @staticmethod
    def _said(turn: Turn) -> list[str]:
        """What a turn's output and error files say about how it ended: what it
        claimed and how it handed it back, its hooks, and its last error line."""
        try:
            answer = _last_json(turn.output.read_text(errors="replace"))
            trouble = turn.errors.read_text(errors="replace").strip()
        except OSError:
            answer, trouble = None, ""
        said = []
        if answer:
            for key in ("claimed", "parked", "returned_unspent", "waiting", "handoff",
                        "lease_lost", "beat_failures", "release_refused"):
                if answer.get(key) not in (None, False):
                    said.append(f"{key} {answer[key]}"
                                if key in ("claimed", "lease_lost", "beat_failures",
                                           "release_refused")
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
        return said

    # --- reload and stop -------------------------------------------------------

    def take_end_turns(self) -> bool:
        """Whether the stop under way is to end this project's running turns:
        an end-turns intent is in the state root, written no longer ago than the
        stop that wrote it waits. One older than that is stale - the stop it
        came with is long over - so it is said, removed and ignored, and cannot
        turn a later restart into an ending."""
        found = fresh_end_turns(self.state_dir, self.settings()["shutdown_grace_seconds"] + 30,
                                self.log)
        if found is None:
            return False
        self.log(f"ending running turns, as {found.get('by')} asked at {found.get('at')}")
        return True

    def drop_end_turns(self) -> None:
        with contextlib.suppress(OSError):
            (self.state_dir / END_TURNS_FILE).unlink()

    def reload(self) -> None:
        """Take up the declaration on disk, or keep the one running.

        It is read and validated whole, inside the project's scope, before
        anything is replaced, so an edit that does not load leaves this slot
        dispatching exactly what it had. Turns already running are children
        held by id and the swap does not touch them. The published fingerprint
        moves last, and only on success."""
        try:
            with self.scope():
                declaration = self.host.load()
        except (Exception, SystemExit) as exc:
            if self.load_error is not None:
                # It never had a declaration to keep: still nothing to serve
                # it by.
                self.load_error = _reason(exc)
                self.log(f"reload rejected, still serving nothing: {self.load_error}")
                return
            self.reload_error = _why(exc)
            self.log(f"reload rejected, keeping the declaration loaded: {self.reload_error}")
            return
        if self.load_error is not None:
            self.load_error = None
            self.checked = None
            self.log("its declaration loads now; serving it")
        self.declaration = declaration
        self.reload_error = None
        self.held.clear()
        self.next_poll = min(self.next_poll,
                             time.monotonic() + self.settings()["poll_seconds"])
        _write_atomic(self.state_dir / FINGERPRINT_FILE, declaration["fingerprint"] + "\n")
        self.wakes.add("reload")
        self.log(f"reloaded: lanes {', '.join(self._lane_words()) or 'none'}")

    def cut_off(self, grace: float | None = None) -> None:
        """End every turn still running and settle each raise it held the way a
        lapsed lease is settled. An adopted turn is ended only while its pid is
        still the process recorded, so a pid used again is never ended. `grace`
        is the wait that came before, for the log: the project's own, or the
        machine's in machine mode."""
        if grace is None:
            grace = self.settings()["shutdown_grace_seconds"]
        for turn in list(self.turns.values()):
            turn.read_receipt()
            if turn.process is not None or turn.still_recorded():
                with contextlib.suppress(Exception):
                    self.host.kill_tree([turn.pid])
            if turn.process is not None:
                with contextlib.suppress(Exception):
                    turn.process.wait(timeout=10)
            else:
                deadline = time.monotonic() + 10
                while not turn.ended()[0] and time.monotonic() < deadline:
                    time.sleep(0.1)
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


class Dispatcher:
    """The loop over a set of project slots: it owns the signals, the stores it
    holds connections to, the machine cap and the start environment every
    project's scope and every turn begin from. Project mode is a dispatcher with
    exactly one slot, the project it was started in, and no machine cap.

    The start environment is taken when the dispatcher is made, before any
    scope opens, because resolving a project writes what it resolved into the
    process environment."""

    def __init__(self, *, tick: float = TICK_SECONDS, pool: StorePool | None = None,
                 machine_cap: int | None = None, environment=None):
        self.environment = dict(os.environ if environment is None else environment)
        self.tick = tick
        self.pool = pool if pool is not None else StorePool()
        # Turns running at once across every slot; None leaves only each
        # project's own limits, which is what project mode runs by.
        self.machine_cap = machine_cap
        # Where the next round under the machine cap starts: the project after
        # the last one that got a turn.
        self.pointer = 0
        self.slots: list[ProjectSlot] = []
        self.stop_requested = False
        self.reload_requested = False
        self.stopping = False

    def add(self, host, declaration: dict, *, machine: bool = False) -> ProjectSlot:
        slot = ProjectSlot(self, host, declaration, machine=machine)
        self.slots.append(slot)
        return slot

    def stores(self) -> list[Store]:
        """The stores the slots reach, each once, in the order slots name them."""
        seen: dict = {}
        for slot in self.slots:
            seen.setdefault(slot.store.key, slot.store)
        return list(seen.values())

    def on(self, store: Store) -> list[ProjectSlot]:
        return [slot for slot in self.slots if slot.store is store]

    def running(self) -> int:
        """Turns running across every slot, adopted ones included."""
        return sum(len(slot.turns) for slot in self.slots)

    # --- what it says ----------------------------------------------------------

    def echo(self, slot: ProjectSlot, line: str) -> None:
        """Where a slot's log line goes besides its project's own log: the
        daemon's stderr, unless that is the log already."""
        if slot._log_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def store_log(self, store: Store, message: str, per_slot=None) -> None:
        """Say something about a store: in project mode on the line of the
        project it serves, `per_slot(slot)` when the words depend on it."""
        for slot in self.on(store):
            slot.log(per_slot(slot) if per_slot else message)

    def listed(self, slot: ProjectSlot) -> tuple[str, str] | None:
        """Whether a machine slot's project is still on the opt-in list; a
        project-mode dispatcher has no list."""
        return None

    # --- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        for slot in self.slots:
            slot.open()
        for store in self.stores():
            self._connect_listener(store)
        for slot in self.slots:
            slot.next_poll = time.monotonic() + slot.settings()["poll_seconds"]
            slot.publish()

    def close(self) -> None:
        for slot in self.slots:
            slot.unpublish()
        for store in self.stores():
            self._drop_listener(store)
            self._drop_query(store)
        for slot in self.slots:
            slot.release()

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

    def reload(self) -> None:
        for slot in self.slots:
            slot.reload()

    def before_pass(self) -> None:
        """What a pass does before it looks at the slots; nothing in project
        mode."""

    def after_pass(self) -> None:
        """What a pass does once every slot has published; nothing in project
        mode."""

    def cap_freed(self) -> None:
        """A turn ended somewhere: every slot the machine cap held back is
        offered a turn again, with the wakes it was holding."""
        for slot in self.slots:
            if slot.waiting_for_cap:
                slot.waiting_for_cap = False
                slot.wakes.add("cap")

    def step(self, wait: bool = True) -> None:
        """One pass: take a reload, look at the children, decide which slots
        have a wake, dispatch each one that may be served, and wait for the
        next reason to look."""
        if self.reload_requested:
            self.reload_requested = False
            self.reload()
        self.before_pass()
        ended = 0
        for slot in self.slots:
            # A slot is checked afresh at this pass's first wake.
            slot.checked = None
            slot._take_up_pause()
            ended += slot.reap()
            slot.renew()
        if ended:
            self.cap_freed()
        now = time.monotonic()
        polled: dict = {}
        for slot in self.slots:
            if now >= slot.next_poll:
                slot.wakes.add("poll")
                slot.next_poll = now + slot.settings()["poll_seconds"]
                # What was held back is asked again on the clock, and a listener
                # that was lost is opened again.
                slot.held.clear()
                polled.setdefault(slot.store.key, []).append(slot)
        for store in self.stores():
            if store.key in polled:
                if store.listener is None:
                    self._connect_listener(store)
                else:
                    for slot in polled[store.key]:
                        if slot.served():
                            slot.check_notification()
            elif store.listener is None and store.relisten_at is not None \
                    and now >= store.relisten_at:
                self._connect_listener(store)
            if store.query is None and store.requery_at is not None \
                    and now >= store.requery_at:
                self._reconnect(store)
        self._ping(time.monotonic())
        candidates = []
        for slot in self.slots:
            if slot.next_moment is not None and now >= slot.next_moment:
                slot.wakes.add("pickup")
                slot.next_moment = None
            over = [tid for tid, r in slot.recent.items() if now >= r["until"]]
            for tid in over:
                del slot.recent[tid]
            if over:
                slot.wakes.add("retry")
            if slot.wakes and not self.stop_requested and not slot.waiting_for_cap:
                candidates.append(slot)
        self.deal(candidates)
        for slot in self.slots:
            slot.publish()
        self.after_pass()
        if wait and not self.stop_requested:
            deadlines = [d for slot in self.slots for d in slot.deadlines()]
            for store in self.stores():
                deadlines += ([store.relisten_at] if store.relisten_at else []) \
                    + ([store.requery_at] if store.requery_at else [])
            self.wait(max(0.0, min([self.tick] + [d - time.monotonic() for d in deadlines])))

    def deal(self, candidates: list[ProjectSlot]) -> None:
        """Hand each slot with a wake its turns. With no machine cap every slot
        that may be served dispatches what its own limits allow.

        Under the cap the slots that may be served are dealt one turn each, in
        rounds from a rotating project pointer, each slot's turn chosen by its
        own lane rotation, until the cap is used up or a round starts nothing;
        the pointer moves to the project after the last one that got a turn.
        Projects are equal. A slot still in the deal when the cap is used up
        keeps its wakes and waits for the cap: any turn ending offers it a turn
        again, so with the cap at one, starts go round the projects with work
        in turn."""
        if self.machine_cap is None:
            for slot in candidates:
                wakes, slot.wakes = slot.wakes, set()
                slot.last_wake = {"at": now_iso(), "reasons": sorted(wakes)}
                if slot.served():
                    slot.dispatch(wakes)
                    slot._plan_next_moment()
            return
        ready = []
        for slot in candidates:
            wakes, slot.wakes = slot.wakes, set()
            slot.last_wake = {"at": now_iso(), "reasons": sorted(wakes)}
            if slot.served():
                ready.append((slot, wakes))
        if not ready:
            return
        count = len(self.slots)
        dealing = sorted(ready, key=lambda item: (self.slots.index(item[0]) - self.pointer)
                         % count)
        free = self.machine_cap - self.running()
        last = None
        while dealing:
            kept, started = [], 0
            for slot, wakes in dealing:
                if free <= 0:
                    kept.append((slot, wakes))
                    continue
                got = slot.dispatch(wakes, limit=1)
                if got:
                    free -= got
                    started += got
                    last = slot
                    kept.append((slot, wakes))
            if free <= 0:
                for slot, wakes in kept:
                    slot.waiting_for_cap = True
                    slot.wakes |= wakes
                break
            if not started:
                break
            dealing = kept
        for slot, _wakes in ready:
            slot._plan_next_moment()
        if last is not None:
            self.pointer = (self.slots.index(last) + 1) % count

    def ending(self, slot: ProjectSlot) -> tuple[bool, float]:
        """Whether the stop under way ends this slot's running turns, and the
        grace they are given first: the project's own intent and grace."""
        return slot.take_end_turns(), slot.settings()["shutdown_grace_seconds"]

    def drop_intent(self, slot: ProjectSlot) -> None:
        slot.drop_end_turns()

    def shutdown(self) -> None:
        """Stop claiming. A slot whose stop asked for its turns to end gives
        them the grace period, then ends what is left and settles each raise it
        held the way a lapsed lease is settled, and removes the intent; every
        other slot leaves its running turns running, for the next daemon to
        adopt."""
        self.stopping = True
        deadlines = {}
        for slot in self.slots:
            slot.reap()
            ending, grace = self.ending(slot)
            if ending:
                deadlines[id(slot)] = (time.monotonic() + grace, grace)
                if slot.turns:
                    slot.log(f"stopping: waiting up to {grace:g}s for {len(slot.turns)} "
                             "turn(s), then ending the rest")
            elif slot.turns:
                slot.log(f"stopping: leaving {len(slot.turns)} running turn(s) running; "
                         "the next daemon to serve this project adopts them")
            slot.publish()
        while True:
            waiting = [slot for slot in self.slots if id(slot) in deadlines
                       and slot.turns and time.monotonic() < deadlines[id(slot)][0]]
            if not waiting:
                break
            for slot in waiting:
                slot.reap()
            if any(slot.turns for slot in waiting):
                time.sleep(0.2)
        for slot in self.slots:
            if id(slot) in deadlines:
                slot.cut_off(deadlines[id(slot)][1])
                self.drop_intent(slot)
                slot.publish()

    # --- waking ----------------------------------------------------------------

    def _connect_listener(self, store: Store) -> None:
        """Open the listener. A failure is said once, not on every attempt, and
        the next attempt is planned; a listener opened after one was lost or
        refused is a wake, since what the store announced meanwhile was lost.
        It is opened outside every scope: the secret it needs is resolved
        here, never inside a project's."""
        self._drop_listener(store)
        try:
            store.listener = store.host.listen()
        except (Exception, SystemExit) as exc:
            store.listener = None
            why = _why(exc)
            if why != store.listen_error:
                self.store_log(
                    store, f"cannot listen for the store's notification, waking by the "
                           f"poll and asking again: {why}",
                    lambda slot: f"cannot listen for the store's notification, waking by the "
                                 f"poll every {slot.settings()['poll_seconds']}s and asking "
                                 f"again: {why}")
            store.listen_error = why
            self._relisten_later(store)
            return
        store.opened()
        store.listener_pinged = time.monotonic()
        if store.listen_error is not None:
            self.store_log(store, f"listening on {store.host.channel} again; asking the "
                                  "store for work")
            for slot in self.on(store):
                slot.wakes.add("relisten")
        store.listen_error = None
        store.relisten_at = None
        store.relisten_delay = RELISTEN_FIRST_SECONDS
        if store.query is None:
            # The store answers, so the questions need not wait out their own
            # interval: the next one asks for its connection at once.
            store.requery_at = None
        caught_up: set = set()
        for slot in self.on(store):
            if slot.served():
                slot.check_notification(catch_up=slot.host.schema not in caught_up)
                caught_up.add(slot.host.schema)

    def _relisten_later(self, store: Store) -> None:
        store.relisten_at = time.monotonic() + store.relisten_delay
        store.relisten_delay = min(store.relisten_delay * 2, RELISTEN_LONGEST_SECONDS)

    def _lose_listener(self, store: Store, exc: BaseException) -> None:
        self.store_log(store, f"lost the store's notification, waking by the poll and "
                              f"asking for it again: {_why(exc)}")
        self._drop_listener(store)
        store.listen_error = _why(exc)
        self._relisten_later(store)

    def _drop_listener(self, store: Store) -> None:
        if store.listener is not None:
            with contextlib.suppress(Exception):
                store.listener.close()
        store.listener = None

    def wait(self, seconds: float) -> None:
        """Sleep until a store notifies or `seconds` pass. A notification is
        handed only to the slot whose project and schema it names; one for
        another project or another schema on the same database is no wake."""
        listening = [store for store in self.stores() if store.listener is not None]
        if not listening:
            time.sleep(seconds)
            return
        if len(listening) == 1:
            [store] = listening
            try:
                got = list(store.listener.notifies(timeout=seconds, stop_after=1))
                if got:
                    got += list(store.listener.notifies(timeout=0))
            except Exception as exc:
                self._lose_listener(store, exc)
                return
            self._route(store, got)
            return
        # Several stores: what any of them already holds is taken first, then
        # the wait is on all of their sockets at once.
        if self._drain(listening):
            return
        import select
        with contextlib.suppress(Exception):
            select.select([store.listener.fileno() for store in listening], [], [], seconds)
        self._drain(listening)

    def _drain(self, stores: list[Store]) -> bool:
        heard = False
        for store in stores:
            if store.listener is None:
                continue
            try:
                got = list(store.listener.notifies(timeout=0))
            except Exception as exc:
                self._lose_listener(store, exc)
                continue
            heard = heard or bool(got)
            self._route(store, got)
        return heard

    def _route(self, store: Store, notes) -> None:
        slots = self.on(store)
        for note in notes:
            try:
                payload = json.loads(note.payload)
            except (ValueError, AttributeError):
                continue
            for slot in slots:
                if slot.heard(payload):
                    slot.notified(payload)

    # --- asking ----------------------------------------------------------------

    def ask(self, store: Store, question, *, scope=None, at_once: bool = False):
        """Put one question to the store on the connection questions go on, and
        answer what it answers. The connection is opened when there is none,
        outside every scope, unless the doubling interval since it was lost has
        not come round, in which case the question is refused at once;
        `at_once` asks for it regardless. The question itself runs inside
        `scope()` when one is given. A question that loses its connection, or
        runs past the store's timeout, drops it and plans the next attempt; any
        other failure is the question's own and leaves the connection held."""
        if store.query is None:
            if (not at_once and store.requery_at is not None
                    and time.monotonic() < store.requery_at):
                raise StoreAway(f"no connection to the store until it answers again: "
                                f"{store.query_error}")
            self._open_query(store)
        conn = store.query
        try:
            with store.host.bounded(conn, ROUND_TRIP_SECONDS):
                with (scope() if scope is not None else contextlib.nullcontext()):
                    answer = question(conn)
                conn.commit()
        except BaseException as exc:
            if store.host.lost(conn, exc):
                self._lose_query(store, exc)
            else:
                try:
                    conn.rollback()
                except Exception as gone:
                    self._lose_query(store, gone)
            raise
        store.query_used = time.monotonic()
        store.requery_delay = RELISTEN_FIRST_SECONDS
        return answer

    def _open_query(self, store: Store) -> None:
        """Open the connection questions go on, or plan the next attempt and
        refuse the question. A failure is said once, not on every attempt."""
        try:
            store.query = store.host.open_query(QUESTION_TIMEOUT_SECONDS)
        except (Exception, SystemExit) as exc:
            store.query = None
            why = _why(exc)
            if why != store.query_error:
                self.store_log(store, f"cannot connect to the store to ask it for work, "
                                      f"asking again: {why}")
            store.query_error = why
            self._requery_later(store)
            raise StoreAway(why) from exc
        store.opened()
        store.query_used = time.monotonic()
        store.requery_at = None
        if store.query_error is not None:
            self.store_log(store, "connected to the store again for its questions")
        store.query_error = None

    def _reconnect(self, store: Store) -> None:
        """The interval since the question connection was lost has come round:
        open it, and ask for work, since what was asked meanwhile went
        unanswered."""
        try:
            self._open_query(store)
        except StoreAway:
            return
        for slot in self.on(store):
            slot.wakes.add("reconnect")

    def _requery_later(self, store: Store) -> None:
        store.requery_at = time.monotonic() + store.requery_delay
        store.requery_delay = min(store.requery_delay * 2, RELISTEN_LONGEST_SECONDS)

    def _lose_query(self, store: Store, exc: BaseException) -> None:
        why = _why(exc)
        self._drop_query(store)
        if why != store.query_error:
            self.store_log(store, f"lost the store connection its questions go on, asking "
                                  f"for it again in {store.requery_delay:g}s: {why}")
        store.query_error = why
        self._requery_later(store)

    def _drop_query(self, store: Store) -> None:
        if store.query is not None:
            with contextlib.suppress(Exception):
                store.query.close()
        store.query = None

    def _ping(self, now: float) -> None:
        """A round trip on each held connection that is due one, so a proxy in
        front of the store sees it in use. A ping that fails is that connection
        lost."""
        for store in self.stores():
            if store.listener is not None and now - store.listener_pinged >= PING_SECONDS:
                store.listener_pinged = now
                try:
                    with store.host.bounded(store.listener, ROUND_TRIP_SECONDS):
                        store.host.ping(store.listener)
                except Exception as exc:
                    self._lose_listener(store, exc)
            if store.query is not None and now - store.query_used >= PING_SECONDS:
                with contextlib.suppress(Exception, SystemExit):
                    self.ask(store, store.host.ping)


class MachineDispatcher(Dispatcher):
    """The dispatcher in machine mode: one per machine, by the lock in its
    machine state root, whose slots follow the opt-in list.

    Everything it knows about the machine arrives through `machine`, the host
    the executable hands it: the list, the machine settings and their
    fingerprint, and for each project on the list either a host for it or why
    it cannot be served yet. On every pass it reads the list again when the
    file moved; a project that left is let go at once, its running turns left
    running, and a project that joined is served from the first pass at which
    it can be. A project that cannot be served yet - its folder gone, not
    explicitly enabling tasks, a connection the machine process may not use, or
    its lock held by a project-mode daemon - is reported with the reason and
    asked again every `ADMIT_SECONDS`. A slot whose declaration does not load
    holds its lock, is `error` with the reason and serves nothing until a
    reload takes one; the others are untouched.

    It loads no project's environment or secret. Its connections are opened
    outside every scope, from its own environment and the machine's tiers, and
    its turns start from the environment it was started with."""

    def __init__(self, machine, *, tick: float = TICK_SECONDS, pool: StorePool | None = None,
                 environment=None):
        super().__init__(tick=tick, pool=pool, environment=environment)
        self.machine = machine
        self.state_dir = Path(machine.state_dir)
        # Every project on the list by its id: what the list says of it, why
        # it is not served while it is not, and its slot once it is.
        self.entries: dict[str, dict] = {}
        self.list_witness = None
        self.list_error: str | None = None
        self.settings: dict = {}
        self.settings_error: str | None = None
        self.reloads = 0
        self.started_at = now_iso()
        self._lock = None
        self._log_path = self.state_dir / LOG_FILE
        self._log_to_stderr = True
        self._published: str | None = None
        self._published_at = 0.0
        self._intent: dict | None = None
        self._intent_read = False

    # --- what it says ----------------------------------------------------------

    def log(self, message: str) -> None:
        """A line about the process, its stores or its cap, tagged `[machine]`."""
        line = log_line(self.state_dir, MACHINE_TAG, message)
        if self._log_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def echo(self, slot: ProjectSlot, line: str) -> None:
        """A project's line, already in its own log, goes to the machine log too."""
        with contextlib.suppress(OSError):
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        if self._log_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def project_log(self, entry: dict, message: str) -> None:
        """A line about a project the machine process holds no slot for yet: in
        the machine log and in the project's own, under its slug."""
        state = Path(self.machine.project_state_dir(entry["slug"]))
        line = (log_line(state, entry["slug"], message) if state.is_dir()
                else f"{now_iso()} tasks service [{entry['slug']}]: "
                     + " ".join(part.strip() for part in message.splitlines()
                                if part.strip()) + "\n")
        with contextlib.suppress(OSError):
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        if self._log_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def store_log(self, store: Store, message: str, per_slot=None) -> None:
        """A store is the machine's: said once under `[machine]`, and in the
        own log of every project on it."""
        self.log(f"store {store.name}: {message}")
        for slot in self.on(store):
            log_line(slot.state_dir, slot.slug, per_slot(slot) if per_slot else message)

    def _stderr_is_log(self) -> bool:
        try:
            mine, log = os.fstat(sys.stderr.fileno()), os.stat(self._log_path)
        except (OSError, ValueError, AttributeError):
            return False
        return (mine.st_dev, mine.st_ino) == (log.st_dev, log.st_ino)

    def status(self) -> dict:
        """The machine status file: the process, its settings, its cap, its
        stores and an entry for every project on the list, served or not."""
        running = self.running()
        cap = self.machine_cap
        return {
            "mode": "machine",
            "pid": os.getpid(),
            "started_at": self.started_at,
            "fingerprint": self.settings.get("fingerprint"),
            "reload_error": self.settings_error,
            "reloads": self.reloads,
            "publish_seconds": PUBLISH_SECONDS,
            "projects_file": {"path": str(self.machine.projects_file),
                              "joined": len(self.entries), "error": self.list_error},
            "cap": {"max_parallel": cap, "running": running,
                    "binding": bool(cap is not None and running >= cap
                                    and any(slot.waiting_for_cap for slot in self.slots))},
            "shutdown_grace_seconds": self.settings.get("shutdown_grace_seconds"),
            "stores": [{**store.row(), "identity": store.host.store_identity(),
                        "projects": [slot.slug for slot in self.on(store)],
                        "retrying": store.relisten_at is not None or store.requery_at is not None}
                       for store in self.stores()],
            "projects": [self._entry_row(entry) for entry in
                         sorted(self.entries.values(), key=lambda e: (e["slug"], e["id"]))],
            "stopping": self.stopping,
        }

    def _entry_row(self, entry: dict) -> dict:
        row = {"project": entry["slug"], "project_id": entry["id"], "root": entry["root"],
               "joined_at": entry.get("joined_at"), "joined_by": entry.get("joined_by")}
        slot = entry.get("slot")
        if slot is None:
            row.update(state=entry["state"], reason=entry["reason"])
            return row
        found = slot.status()
        row.update({key: found[key] for key in (
            "state", "reason", "fingerprint", "reload_error", "wake_by", "lanes", "idle",
            "turns", "deferred", "next_wake", "last_wake", "waiting_for_cap")})
        row["pause"] = found["pause"]
        row["store"] = slot.store.name
        return row

    def publish(self) -> None:
        """Write the machine status when anything in it moved, and at least
        every `PUBLISH_SECONDS`, so its age tells a live process from a hung
        one."""
        text = json.dumps(self.status(), indent=2)
        now = time.monotonic()
        if text == self._published and now - self._published_at < PUBLISH_SECONDS:
            return
        found = json.loads(text)
        found["published_at"] = now_iso()
        with contextlib.suppress(OSError):
            _write_atomic(self.state_dir / STATUS_FILE, json.dumps(found, indent=2) + "\n")
        self._published, self._published_at = text, now

    # --- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        """Take the machine's one-process lock, say which settings hold it, and
        take up every project on the list that can be served."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_to_stderr = not self._stderr_is_log()
        self._lock = take_lock(self.state_dir)
        if self._lock is None:
            raise RuntimeError(f"another machine tasks service holds "
                               f"{self.state_dir / LOCK_FILE}")
        self.settings = self.machine.load_settings()
        self.machine_cap = self.settings["max_parallel"]
        (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
        _write_atomic(self.state_dir / FINGERPRINT_FILE, self.settings["fingerprint"] + "\n")
        self.log(f"started, pid {os.getpid()}, serving the projects on "
                 f"{self.machine.projects_file}; {self._settings_words()}")
        self.before_pass()
        self.publish()

    def _settings_words(self) -> str:
        cap = self.settings.get("max_parallel")
        return (f"machine cap {cap} turn(s)" if cap is not None
                else "no machine cap, each project's own limits apply") + \
            f", stop --end-turns grace {self.settings.get('shutdown_grace_seconds')}s"

    def close(self) -> None:
        super().close()
        for entry in self.entries.values():
            entry["slot"] = None
        for name in (PID_FILE, FINGERPRINT_FILE, STATUS_FILE):
            with contextlib.suppress(OSError):
                (self.state_dir / name).unlink()
        self.log("stopped")
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def reload(self) -> None:
        """SIGHUP: the machine settings, then every slot on its own. Settings
        that do not load leave the ones running, said in the log and the
        status; a slot whose fingerprint did not move is not touched."""
        try:
            settings = self.machine.load_settings()
        except (Exception, SystemExit) as exc:
            self.settings_error = _why(exc)
            self.log(f"reload rejected, keeping the machine settings loaded: "
                     f"{self.settings_error}")
        else:
            self.settings, self.settings_error = settings, None
            self.machine_cap = settings["max_parallel"]
            _write_atomic(self.state_dir / FINGERPRINT_FILE, settings["fingerprint"] + "\n")
            self.log(f"reloaded the machine settings: {self._settings_words()}")
        for slot in self.slots:
            try:
                with slot.scope():
                    current = slot.host.fingerprint()
            except (Exception, SystemExit):
                current = None
            if slot.load_error is not None or current != slot.declaration["fingerprint"]:
                slot.reload()
        self.reloads += 1
        # A cap that grew lets the slots it held back go now.
        self.cap_freed()

    def before_pass(self) -> None:
        """Follow the opt-in list, and ask every project on it not yet served
        whether it can be."""
        self._take_list()
        now = time.monotonic()
        for entry in list(self.entries.values()):
            if entry.get("slot") is None and now >= entry.get("next_try", 0.0) \
                    and not self.stop_requested:
                entry["next_try"] = now + ADMIT_SECONDS
                self._admit(entry)

    def after_pass(self) -> None:
        self.publish()

    # --- the list --------------------------------------------------------------

    def _take_list(self) -> None:
        witness = self.machine.list_witness()
        if witness == self.list_witness:
            return
        self.list_witness = witness
        try:
            listed = self.machine.read_list()
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
            entry = {**found, "id": project, "state": "refused",
                     "reason": "not yet checked", "slot": None, "next_try": 0.0,
                     "said": None}
            self.entries[project] = entry
            self.project_log(entry, f"on the machine service's opt-in list, joined "
                                    f"{found.get('joined_at')} by {found.get('joined_by')}; "
                                    f"project {project} at {found['root']}")

    def _leave(self, project: str) -> None:
        """A project no longer on the list: its slot is let go at once - lock,
        pid and status - and its running turns are left running for whoever
        serves it next."""
        entry = self.entries.pop(project)
        slot = entry.get("slot")
        if slot is None:
            self.project_log(entry, "left the machine service's opt-in list")
            return
        running = len(slot.turns)
        self.slots.remove(slot)
        slot.unpublish()
        slot.release("left the machine service's opt-in list; let go of its lock"
                     + (f", leaving {running} running turn(s) running for whoever "
                        "serves it next" if running else ""))
        self._drop_unused_stores()
        if running:
            self.cap_freed()

    def _drop_unused_stores(self) -> None:
        used = {slot.store.key for slot in self.slots}
        for key in [key for key in self.pool.stores if key not in used]:
            store = self.pool.stores.pop(key)
            self._drop_listener(store)
            self._drop_query(store)
            self.log(f"store {store.name}: no project served on it any more; "
                     "its connections are closed")

    def listed(self, slot: ProjectSlot) -> tuple[str, str] | None:
        """Whether the slot's project is on the list as it is on disk now. The
        list is read again when it moved and nothing is changed here; the next
        pass takes the change up. A list that does not load leaves the last one
        that did."""
        project = slot.host.project
        known = self.entries.get(project)
        if self.machine.list_witness() != self.list_witness:
            try:
                listed = self.machine.read_list()
            except (Exception, SystemExit):
                listed = None
            if listed is not None:
                found = listed.get(project)
                known = (known if found is not None and known is not None
                         and found["root"] == known["root"] else None)
        if known is None or known.get("slot") is not slot:
            return ("refused", "not_joined: the project is no longer on the machine "
                               f"service's opt-in list {self.machine.projects_file}")
        return None

    def _admit(self, entry: dict) -> None:
        """Serve a project on the list if it can be: the checks a slot takes
        before every action, asked of the machine host; then the project's
        lock; then its declaration. What stops it is said once."""
        host, refusal = self.machine.admit(entry, self.environment)
        if refusal is None:
            state_dir = Path(host.state_dir)
            lock = take_lock(state_dir)
            if lock is None:
                holder = read_pid(state_dir)
                refusal = ("refused",
                           f"a project-mode daemon, pid {holder}, serves this project; "
                           "stop it there" if pid_alive(holder) else
                           "another process holds this project's lock "
                           f"{state_dir / LOCK_FILE}")
        if refusal is not None:
            entry["state"], entry["reason"] = refusal
            if entry.get("said") != refusal:
                entry["said"] = refusal
                self.project_log(entry, f"{refusal[0]}: {refusal[1]}; asking again every "
                                        f"{ADMIT_SECONDS:g}s and starting nothing for it "
                                        "meanwhile")
            return
        load_error = None
        try:
            with host.scope(self.environment):
                declaration = host.load()
        except (Exception, SystemExit) as exc:
            load_error = _reason(exc)
            declaration = host.placeholder()
        new_store = host.store_key() not in self.pool.stores
        slot = ProjectSlot(self, host, declaration, machine=True)
        slot.load_error = load_error
        self.slots.append(slot)
        entry["slot"], entry["said"] = slot, None
        slot.open(lock=lock)
        if load_error is not None:
            slot.log(f"error: its declaration does not load, so nothing is asked or "
                     f"started for it until a reload takes one: {load_error}")
        slot.next_poll = time.monotonic() + slot.settings()["poll_seconds"]
        if new_store:
            self._connect_listener(slot.store)
        elif slot.store.listener is not None and slot.served():
            slot.check_notification()
        slot.publish()

    # --- stopping --------------------------------------------------------------

    def ending(self, slot: ProjectSlot) -> tuple[bool, float]:
        """`stop --machine --end-turns` writes its intent in the machine state
        root, and it ends every project's running turns after the machine's
        grace."""
        grace = self.settings.get("shutdown_grace_seconds", 60)
        if not self._intent_read:
            self._intent_read = True
            self._intent = fresh_end_turns(self.state_dir, grace + 30, self.log)
            if self._intent is not None:
                self.log(f"ending running turns in every project, as "
                         f"{self._intent.get('by')} asked at {self._intent.get('at')}")
        return self._intent is not None, grace

    def drop_intent(self, slot: ProjectSlot) -> None:
        with contextlib.suppress(OSError):
            (self.state_dir / END_TURNS_FILE).unlink()

    def shutdown(self) -> None:
        # The intent is the machine's, read once whether or not any slot runs.
        self.ending(None)
        super().shutdown()
        if self._intent is not None:
            self.drop_intent(None)
        self.publish()
