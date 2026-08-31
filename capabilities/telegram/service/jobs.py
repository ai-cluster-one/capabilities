"""The job register — the ledger of work that outlives the sentence that asked for it.

Two classes of work run in this service and they are not the same thing. A
conversation is answered while the person is still there, so its unit is the
message and its record is the watermark register in `register.json`. A task
outlives its sentence: the person who asked has gone back to talking, the work
has minutes or hours left, and a correction arriving meanwhile is an amendment
to work already in flight rather than a second request. That second class is
what this module records.

The store is the home, not a private database beside it. `automations` already
established the shape — a capability declares its own namespace and migrates it
itself, and the core tier knows nothing about these columns — and the reason it
is a table rather than a JSON blob is the same one: the runner filters on
`project_id`, `environment`, `surface` and `state` on every tick.

WHAT THE ROW HOLDS, AND WHAT IT REFUSES TO HOLD
===============================================
References, never content. The trajectory is already in the engine's own
rollout, named here by `session_id`, and copying any of it into a row would
create a second copy that drifts from the first. What the row adds is what the
session cannot answer: whose authority the work carries, which channel it
reports into, how long it waited for a slot, and why it stopped.

ONE WAY TO STOP, ONE WAY TO CONTINUE
====================================
Every stop is continuable, because the session is the checkpoint: a rollout of
a job killed mid-turn holds everything the turn had established. So there is no
state a job cannot come back from, and therefore no second halt verb for the
stop that is meant to be final. What a caller decides is not whether the work
can return — it always can — but whether anybody asks it to. `outcome` records
why it stopped; `resume` is how it comes back, from any of them.

AMENDMENT KEEPS THE ROW
=======================
Stopping a job and continuing its session with added context is one task, not
two. The identity is stable across every amendment, `amendments` counts them,
and the added text stays in the session where it already is. That is also why
there is no `parent_id`: an amendment has no lineage to record.

ISOLATION IS A CORRECTNESS CONDITION
====================================
Every queue read is filtered by `project_id` and `environment`. The store is
shared — by two projects on one machine, and by two machines on one project —
so a query that forgets whose work it is asking about answers with somebody
else's. It cannot therefore be a `WHERE` clause each caller is trusted to
remember, which is the whole reason `JobRegister` exists as a boundary.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4


STORE_NAMESPACE = "telegram"
STORE_VERSION = 1

# Portable DDL, in the same dialect the core tier writes: a timestamp is TEXT
# holding an ISO instant and a flag is INTEGER, because those are the two
# constructs SQLite and PostgreSQL both spell the same way. `{json}` is the one
# substitution this schema needs from the store.
STORE_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS tg_worker_jobs (
        id                TEXT PRIMARY KEY,
        project_id        TEXT NOT NULL,
        environment       TEXT NOT NULL,
        surface           TEXT NOT NULL,
        channel_key       TEXT NOT NULL,
        requested_by      TEXT NOT NULL,
        origin_message_id TEXT,
        description       TEXT NOT NULL,
        engine            TEXT NOT NULL,
        model             TEXT,
        session_id        TEXT,
        pid               INTEGER,
        state             TEXT NOT NULL,
        outcome           TEXT,
        attempt           INTEGER NOT NULL DEFAULT 0,
        amendments        INTEGER NOT NULL DEFAULT 0,
        stop_requested    INTEGER NOT NULL DEFAULT 0,
        exit_code         INTEGER,
        error             TEXT,
        log_path          TEXT,
        created_at        TEXT NOT NULL,
        started_at        TEXT,
        finished_at       TEXT,
        updated_at        TEXT NOT NULL
    )
    """,
    # How the runner selects the next job.
    """
    CREATE INDEX IF NOT EXISTS tg_worker_jobs_queue_idx
        ON tg_worker_jobs (project_id, environment, surface, state)
    """,
    # How a caller lists the open jobs of one channel.
    """
    CREATE INDEX IF NOT EXISTS tg_worker_jobs_channel_idx
        ON tg_worker_jobs (project_id, surface, channel_key, state)
    """,
    # Two concurrent resumes of one session cannot both enter `running`.
    # Sequential amendments are unaffected; only the concurrent case is
    # refused, and it is refused by the schema rather than by a lock somebody
    # has to remember to take.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tg_worker_jobs_live_session_idx
        ON tg_worker_jobs (session_id) WHERE state = 'running'
    """,
]

# WHAT A JOB IS DOING, AND WHY IT IS DOING THAT
# =============================================
# Two columns, because they answer two questions and only one of them changes
# what anything does.
#
# `state` is the whole of the runner's interest: a job is waiting for a slot,
# holding one, or holding nothing. Nothing else is a state, because nothing
# else changes what may happen next — the session is the checkpoint, so work
# that stopped for any reason at all can be continued from where it stopped,
# and a job that never started resumes by starting. There is deliberately no
# `finished`: it would differ from `stopped` only in the label, and a label
# that never changes behaviour belongs in the column for labels.
#
# `outcome` is that label — why the job is stopped. It earns its values by
# them behaving differently: `quota` is resumed by the runner when the pause
# lifts, `interrupted` by the configured recovery policy, and the rest only by
# somebody asking. It says nothing while the job is waiting or running.
WAITING = "waiting"
RUNNING = "running"
STOPPED = "stopped"
STATES = (WAITING, RUNNING, STOPPED)

SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
QUOTA = "quota"
OUTCOMES = (SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, QUOTA)

# Resumed without being asked: one when the subscription comes back, one when
# the daemon that was running it restarted. Every other outcome waits for a
# person, because every other outcome is one a person chose or must read.
SELF_RESUMING = (QUOTA, INTERRUPTED)

ENGINES = ("codex", "claude", "stub")

# An environment is a label a person types into settings, and it lands in a
# query that decides whose work a runner may take. Anything ambiguous to
# address stays out.
ENVIRONMENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.IGNORECASE)


class JobError(Exception):
    """Every failure this module reports, carrying a slug the caller maps onto
    its own error envelope."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug = slug
        self.message = message
        self.hint = hint


def iso() -> str:
    """Microsecond resolution, because arrival order is the whole queue
    discipline. At second resolution two jobs registered in the same second tie,
    and the tiebreak falls to a uuid, which does not order — so the oldest
    queued job stops being the one that arrived first."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def project_identity(envelope: Path) -> dict:
    """The project this service records its work under."""
    try:
        return json.loads((Path(envelope) / "project.json").read_text())
    except (OSError, ValueError) as exc:
        raise JobError("no_project_identity",
                       f"no project identity under {envelope}",
                       "run `capabilities init` in the project") from exc


def open_register(store_module, envelope: Path, environment: str,
                  surface: str = "telegram", url: str | None = None):
    """The store this project's jobs live in, and the register onto it.

    There is no file-mode register. A queue, a slot count and a cancellation
    flag are records nobody authors and nobody reviews, and a capability keeping
    its own database for them was only ever the easy thing. The caller closes
    the store.
    """
    identity = project_identity(envelope)
    slug = identity.get("slug")
    if not slug:
        raise JobError("no_project_identity",
                       f"{Path(envelope) / 'project.json'} declares no slug",
                       "run `capabilities init` in the project")
    store = store_module.open_store(url)
    try:
        store.migrate()
        store.project_register(identity["id"], slug)
        store.migrate(STORE_NAMESPACE, STORE_VERSION, STORE_MIGRATIONS)
    except store_module.StoreError as exc:
        store.close()
        raise JobError("store_unavailable",
                       f"cannot prepare the store: {exc.message}") from exc
    except Exception:
        store.close()
        raise
    project_id = store._project_id(slug)
    return store, JobRegister(store, project_id, environment, surface, slug=slug)


class JobRegister:
    """Every question and every claim about jobs, in one place that knows whose.

    The same class serves a local SQLite store and a coordinated backend; the
    scope and the query shape do not change with the deployment.
    """

    def __init__(self, store, project_id: str, environment: str,
                 surface: str = "telegram", slug: str | None = None):
        if not project_id:
            raise JobError("no_project_scope",
                           "the job register requires a registered project")
        if not ENVIRONMENT_RE.fullmatch(str(environment or "")):
            raise JobError("bad_environment",
                           f"environment {environment!r} must match "
                           f"{ENVIRONMENT_RE.pattern}")
        self.store = store
        self.db = store._conn
        self.scope = ("project", slug) if slug else None
        self.project_id = project_id
        self.environment = str(environment)
        self.surface = str(surface)

    # -- the scope, applied once ----------------------------------------------

    def _where(self, *predicates: str, environment: bool = True) -> tuple[str, list]:
        """The isolation clause every read and every write carries.

        `environment` is dropped only where the identity alone already selects
        the row — a lookup by primary key — so that a job written under one
        environment can still be read back by a doctor run under another.
        """
        parts = ["project_id = ?", "surface = ?"]
        params: list = [self.project_id, self.surface]
        if environment:
            parts.append("environment = ?")
            params.append(self.environment)
        parts.extend(predicates)
        return " WHERE " + " AND ".join(parts), params

    @staticmethod
    def _dicts(cursor) -> list[dict[str, Any]]:
        """Rows as dicts without depending on a row factory: the register runs
        against a bare store connection and, later, against a driver that has
        no such notion at all."""
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _rows(self, predicates: str = "", params: Sequence[Any] = (),
              order: str = "", limit: int | None = None,
              environment: bool = True) -> list[dict[str, Any]]:
        clause, scope = self._where(*([predicates] if predicates else []),
                                    environment=environment)
        sql = "SELECT * FROM tg_worker_jobs" + clause
        if order:
            sql += f" ORDER BY {order}"
        args = scope + list(params)
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return self._dicts(self.db.execute(sql, tuple(args)))

    @staticmethod
    def _marks(values: Sequence[Any]) -> str:
        return ", ".join("?" for _ in values)

    # -- reads ----------------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any] | None:
        rows = self._rows("id = ?", (job_id,), environment=False)
        return rows[0] if rows else None

    def list(self, *, limit: int = 50, state: str | None = None,
             outcome: str | None = None,
             channel_key: str | None = None) -> list[dict[str, Any]]:
        predicates, params = [], []
        if state:
            predicates.append("state = ?")
            params.append(state)
        if outcome:
            predicates.append("outcome = ?")
            params.append(outcome)
        if channel_key:
            predicates.append("channel_key = ?")
            params.append(channel_key)
        return self._rows(" AND ".join(predicates), params,
                          order="created_at DESC, id", limit=limit)

    def open_jobs(self, channel_key: str) -> list[dict[str, Any]]:
        """What is still in flight for one channel, oldest first."""
        return self._rows(f"channel_key = ? AND state IN ({self._marks((WAITING, RUNNING))})",
                          [channel_key, WAITING, RUNNING], order="created_at, id")

    def next_waiting(self) -> dict[str, Any] | None:
        """The oldest job waiting for a slot in this project and environment.
        Arrival order, with no estimation, priority or classes of size."""
        rows = self._rows("state = ?", (WAITING,), order="created_at, id", limit=1)
        return rows[0] if rows else None

    def active(self) -> list[dict[str, Any]]:
        return self._rows("state = ?", (RUNNING,), order="started_at, id")

    def running(self) -> int:
        clause, params = self._where("state = ?")
        return self.db.execute("SELECT COUNT(*) FROM tg_worker_jobs" + clause,
                               tuple(params + [RUNNING])).fetchone()[0]

    def counts(self) -> dict[str, Any]:
        """What a person asks first: how much is moving, and how much stopped
        for a reason worth reading."""
        clause, params = self._where()
        states = dict(self.db.execute(
            "SELECT state, COUNT(*) FROM tg_worker_jobs" + clause + " GROUP BY state",
            tuple(params)).fetchall())
        outcomes = dict(self.db.execute(
            "SELECT outcome, COUNT(*) FROM tg_worker_jobs" + clause
            + " AND outcome IS NOT NULL GROUP BY outcome", tuple(params)).fetchall())
        return {"state": states, "outcome": outcomes}

    def stop_pending(self) -> list[dict[str, Any]]:
        return self._rows(f"stop_requested = 1 AND state IN ({self._marks((WAITING, RUNNING))})",
                          [WAITING, RUNNING], order="created_at, id")

    def amend_pending(self) -> list[dict[str, Any]]:
        """Running jobs whose next turn is already waiting for them.

        The staged text is the request: something wrote an amendment, and until
        the process is stopped and the row goes back to waiting, nothing has
        acted on it. That makes a column for the intent unnecessary — the text
        being there is the intent.
        """
        return [row for row in self._rows("state = ?", (RUNNING,),
                                          order="started_at, id")
                if self.pending_amendment(row["id"])]

    def self_resuming(self) -> list[dict[str, Any]]:
        """Stopped work the daemon continues on its own, without being asked."""
        return self._rows(
            f"state = ? AND outcome IN ({self._marks(SELF_RESUMING)})",
            [STOPPED, *SELF_RESUMING], order="created_at, id")

    # -- writes ---------------------------------------------------------------

    def register(self, *, channel_key: str, requested_by: str, description: str,
                 engine: str, origin_message_id: str | None = None,
                 model: str | None = None) -> dict[str, Any]:
        """Enter one task into the queue. No duration is promised, because none
        can be honoured."""
        description = str(description or "").strip()
        if not description:
            raise JobError("no_description",
                           "a job needs one line describing it in the operator's terms")
        if engine not in ENGINES:
            raise JobError("bad_engine",
                           f"engine {engine!r} must be one of {', '.join(ENGINES)}")
        if not str(channel_key or "").strip():
            raise JobError("no_channel", "a job needs the channel it reports into")
        if not str(requested_by or "").strip():
            raise JobError("no_requester",
                           "a job needs the operator whose authority it carries")
        job_id = str(uuid4())
        now = iso()
        self.db.execute(
            "INSERT INTO tg_worker_jobs (id, project_id, environment, surface, "
            "channel_key, requested_by, origin_message_id, description, engine, "
            "model, state, attempt, amendments, stop_requested, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)",
            (job_id, self.project_id, self.environment, self.surface,
             str(channel_key), str(requested_by),
             None if origin_message_id is None else str(origin_message_id),
             description, engine, model, WAITING, now, now))
        self.db.commit()
        return self.get(job_id)

    def update(self, job_id: str, **columns: Any) -> dict[str, Any] | None:
        """One scoped write. `updated_at` moves on every one of them, amendments
        included, so the column means what it says."""
        columns.setdefault("updated_at", iso())
        assignments = ", ".join(f"{name} = ?" for name in columns)
        clause, params = self._where("id = ?", environment=False)
        self.db.execute(f"UPDATE tg_worker_jobs SET {assignments}" + clause,
                        tuple(list(columns.values()) + params + [job_id]))
        self.db.commit()
        return self.get(job_id)

    def start(self, job_id: str, *, pid: int | None = None,
              session_id: str | None = None, model: str | None = None,
              log_path: str | None = None) -> dict[str, Any] | None:
        """Take a waiting job into `running`.

        The claim is conditional on the row still waiting, so on a store two
        runners share, whichever updates first owns the job and the other is
        told by getting no row back. The outcome is cleared because it
        described the stop this start has just ended.
        """
        now = iso()
        clause, params = self._where("id = ?", "state = ?", environment=False)
        cur = self.db.execute(
            "UPDATE tg_worker_jobs SET state = ?, outcome = NULL, "
            "attempt = attempt + 1, pid = ?, started_at = COALESCE(started_at, ?), "
            "updated_at = ?" + clause,
            tuple([RUNNING, pid, now, now] + params + [job_id, WAITING]))
        self.db.commit()
        if not cur.rowcount:
            return None
        columns: dict[str, Any] = {}
        if session_id is not None:
            columns["session_id"] = session_id
        if model is not None:
            columns["model"] = model
        if log_path is not None:
            columns["log_path"] = log_path
        return self.update(job_id, **columns) if columns else self.get(job_id)

    # -- the amendment hand-off -----------------------------------------------
    #
    # The added text belongs in the session, and that is where it ends up — but
    # the caller that wrote it and the runner that delivers it are two
    # processes, so something has to hold it in between. That something is a
    # state record, which is what the store's state collection is for, and it
    # is deliberately not a column: a register that kept amendment texts would
    # be keeping a second copy of what the rollout already holds.

    def _amendment_key(self, job_id: str) -> str:
        return f"job-amendment.{job_id}"

    def stage_amendment(self, job_id: str, text: str) -> None:
        text = str(text or "").strip()
        if not text or self.scope is None:
            return
        pending = self.pending_amendment(job_id)
        pending.append(text)
        self.store.state_set(self.surface, self._amendment_key(job_id),
                             pending, self.scope)

    def pending_amendment(self, job_id: str) -> list[str]:
        if self.scope is None:
            return []
        value = self.store.state_get(self.surface, self._amendment_key(job_id),
                                     self.scope)
        return list(value) if isinstance(value, list) else []

    def take_amendment(self, job_id: str) -> list[str]:
        """Read the pending text and clear it, so a redelivery cannot repeat it."""
        pending = self.pending_amendment(job_id)
        if pending and self.scope is not None:
            self.store.state_delete(self.surface, self._amendment_key(job_id),
                                    self.scope)
        return pending

    def amend(self, job_id: str, text: str | None = None) -> dict[str, Any] | None:
        """Record that the job was stopped and continued with added context.

        The row keeps its identity and its session; the added text goes to the
        session by way of `stage_amendment`, and the count answers "was this
        reformulated?" without opening it.
        """
        if self.get(job_id) is None:
            return None
        if text:
            self.stage_amendment(job_id, text)
        now = iso()
        clause, params = self._where("id = ?", environment=False)
        self.db.execute(
            "UPDATE tg_worker_jobs SET amendments = amendments + 1, state = ?, "
            "outcome = NULL, pid = NULL, stop_requested = 0, error = NULL, "
            "exit_code = NULL, finished_at = NULL, updated_at = ?" + clause,
            tuple([WAITING, now] + params + [job_id]))
        self.db.commit()
        return self.get(job_id)

    # -- stopping and continuing ----------------------------------------------
    #
    # One way to halt work and one way to continue it. There is no second pair
    # for "stop for good", because the session is the checkpoint and every stop
    # is therefore continuable: what a caller chooses is not whether the work
    # can come back, only whether anybody asks it to.

    def request_stop(self, job_id: str) -> dict[str, Any] | None:
        """Ask for the work to stop.

        Asynchronous by nature: the runner holds the process group, so this
        records the ask and the next tick acts on it. Without the flag there is
        no state between "asked to stop" and "stopped".
        """
        row = self.get(job_id)
        if row is None or row["state"] == STOPPED:
            return None
        return self.update(job_id, stop_requested=1)

    def stop(self, job_id: str, outcome: str, *, exit_code: int | None = None,
             error: str | None = None, session_id: str | None = None,
             model: str | None = None) -> dict[str, Any] | None:
        """Land a stop, whatever caused it — the work finishing counts."""
        if outcome not in OUTCOMES:
            raise JobError("bad_outcome",
                           f"outcome {outcome!r} must be one of {', '.join(OUTCOMES)}")
        columns: dict[str, Any] = {
            "state": STOPPED,
            "outcome": outcome,
            "pid": None,
            "stop_requested": 0,
            "finished_at": iso(),
            "exit_code": exit_code,
            "error": None if error is None else str(error)[:500],
        }
        if session_id is not None:
            columns["session_id"] = session_id
        if model is not None:
            columns["model"] = model
        return self.update(job_id, **columns)

    def resume(self, job_id: str) -> dict[str, Any] | None:
        """Make sure this job is on its way, from wherever it is.

        The exact counterpart of `request_stop`, and idempotent for the same
        reason it is: both are asks about a direction, not about a transition.
        Stopped work goes back to waiting on the session it already has — the
        same job, same row, same rollout, and one that never started resumes by
        starting. Work that is already moving simply loses a stop nobody wants
        any more. There is no state this refuses, because there is no state a
        job cannot continue from.
        """
        row = self.get(job_id)
        if row is None:
            return None
        if row["state"] != STOPPED:
            return (self.update(job_id, stop_requested=0)
                    if row["stop_requested"] else row)
        return self.update(job_id, state=WAITING, outcome=None, pid=None,
                           stop_requested=0, error=None, exit_code=None,
                           finished_at=None)

    def interrupt_active(self, reason: str) -> list[dict[str, Any]]:
        """On daemon start, a row that claims to be running names a process that
        is not. Recording that is what keeps the register honest across a box
        restart; whether the work is then continued is the recovery policy's
        call, not this method's."""
        rows = self.active()
        for row in rows:
            self.stop(row["id"], INTERRUPTED, error=reason)
        return rows
