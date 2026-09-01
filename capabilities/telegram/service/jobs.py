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

WHAT THE REGISTER HOLDS, AND WHAT IT REFUSES TO HOLD
===================================================
The trajectory stays in the engine's own rollout, named by `session_id`; the
register does not copy it. It does durably hold the two payloads which have not
yet reached that rollout or Telegram: user amendments until engine acceptance,
and the final result until delivery. What the job row adds is what the session
cannot answer: whose authority the work carries, which channel it reports into,
how long it waited for a slot, who owns the current attempt, and why it stopped.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4


STORE_NAMESPACE = "telegram"
STORE_VERSION = 3

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
    # Version 2: execution ownership, durable delivery, and quota recovery.
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN attempt_token TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN lease_owner TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN lease_expires_at TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN owner_host TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN pgid INTEGER
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN resume_at TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN result_text TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN result_silent INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN execution_finished_at TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivery_state TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivery_error TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivered_at TEXT
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tg_worker_jobs_origin_idx
        ON tg_worker_jobs (project_id, environment, surface, channel_key, origin_message_id)
        WHERE origin_message_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS tg_worker_job_amendments (
        id            TEXT PRIMARY KEY,
        project_id    TEXT NOT NULL,
        environment   TEXT NOT NULL,
        surface       TEXT NOT NULL,
        job_id        TEXT NOT NULL,
        text          TEXT NOT NULL,
        state         TEXT NOT NULL,
        claim_token   TEXT,
        created_at    TEXT NOT NULL,
        claimed_at    TEXT,
        acked_at      TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS tg_worker_job_amendments_pending_idx
        ON tg_worker_job_amendments
        (project_id, environment, surface, job_id, state, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS tg_worker_job_slots (
        project_id      TEXT NOT NULL,
        environment     TEXT NOT NULL,
        surface         TEXT NOT NULL,
        slot             INTEGER NOT NULL,
        job_id           TEXT NOT NULL,
        owner_id         TEXT NOT NULL,
        attempt_token    TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        PRIMARY KEY (project_id, environment, surface, slot),
        UNIQUE (project_id, environment, surface, job_id)
    )
    """,
    # Version 3: delivery is itself a leased single-consumer operation.
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivery_owner TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivery_token TEXT
    """,
    """
    ALTER TABLE tg_worker_jobs ADD COLUMN delivery_lease_expires_at TEXT
    """,
]

# Store.migrate applies the supplied body as one version step. Keep each
# additive generation separate so an upgrade from v2 does not replay v2's
# ALTER TABLE statements before adding v3.
STORE_MIGRATION_STEPS = {
    1: STORE_MIGRATIONS[:4],
    2: STORE_MIGRATIONS[4:-3],
    3: STORE_MIGRATIONS[-3:],
}

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
# A job is written before it is handed over. `draft` is that gap made explicit:
# the row exists, so a description can be corrected and material added to it,
# and no runner will take it, because the runner claims `waiting` and nothing
# else. Submitting is the single act that ends the writing and begins the work.
DRAFT = "draft"
WAITING = "waiting"
RUNNING = "running"
STOPPED = "stopped"
STATES = (DRAFT, WAITING, RUNNING, STOPPED)

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
        for version in range(1, STORE_VERSION + 1):
            if store.schema_version(STORE_NAMESPACE) < version:
                store.migrate(
                    STORE_NAMESPACE, version, STORE_MIGRATION_STEPS[version])
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
        self.project_id = project_id
        self.environment = str(environment)
        self.surface = str(surface)

    # -- the scope, applied once ----------------------------------------------

    def _where(self, *predicates: str) -> tuple[str, list]:
        """The isolation clause every read and every write carries.

        Environment is part of every ordinary lookup, including primary-key
        reads. Cross-environment inspection belongs to an explicit operator
        surface, never to the register workers use.
        """
        parts = ["project_id = ?", "surface = ?"]
        params: list = [self.project_id, self.surface]
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
              order: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        clause, scope = self._where(*([predicates] if predicates else []))
        sql = "SELECT * FROM tg_worker_jobs" + clause
        if order:
            sql += f" ORDER BY {order}"
        args = scope + list(params)
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return self._dicts(self.store._execute(sql, tuple(args)))

    @staticmethod
    def _marks(values: Sequence[Any]) -> str:
        return ", ".join("?" for _ in values)

    # -- reads ----------------------------------------------------------------

    def get(self, job_id: str, *, actor_id: str | None = None) -> dict[str, Any] | None:
        predicates, params = ["id = ?"], [job_id]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            params.append(str(actor_id))
        rows = self._rows(" AND ".join(predicates), params)
        return rows[0] if rows else None

    def list(self, *, limit: int = 50, state: str | None = None,
             outcome: str | None = None,
             channel_key: str | None = None,
             actor_id: str | None = None) -> list[dict[str, Any]]:
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
        if actor_id is not None:
            predicates.append("requested_by = ?")
            params.append(str(actor_id))
        return self._rows(" AND ".join(predicates), params,
                          order="created_at DESC, id", limit=limit)

    def open_jobs(self, channel_key: str, *, actor_id: str | None = None,
                  include_stopped: bool = False) -> list[dict[str, Any]]:
        """What is still in flight for one channel, oldest first."""
        states = (WAITING, RUNNING, STOPPED) if include_stopped else (WAITING, RUNNING)
        predicates = [f"channel_key = ? AND state IN ({self._marks(states)})"]
        params: list[Any] = [channel_key, *states]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            params.append(str(actor_id))
        return self._rows(" AND ".join(predicates), params, order="created_at, id")

    def next_waiting(self) -> dict[str, Any] | None:
        """The oldest job waiting for a slot in this project and environment.
        Arrival order, with no estimation, priority or classes of size."""
        rows = self._rows("state = ?", (WAITING,), order="created_at, id", limit=1)
        return rows[0] if rows else None

    def active(self) -> list[dict[str, Any]]:
        return self._rows("state = ?", (RUNNING,), order="started_at, id")

    def expired_active(self) -> list[dict[str, Any]]:
        return self._rows(
            "state = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
            (RUNNING, iso()), order="started_at, id")

    def running(self, *, actor_id: str | None = None) -> int:
        predicates = ["state = ?"]
        values: list[Any] = [RUNNING]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        return self.store._execute("SELECT COUNT(*) FROM tg_worker_jobs" + clause,
                                   tuple(params + values)).fetchone()[0]

    def counts(self, *, actor_id: str | None = None) -> dict[str, Any]:
        """What a person asks first: how much is moving, and how much stopped
        for a reason worth reading."""
        predicates: list[str] = []
        values: list[Any] = []
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        states = dict(self.store._execute(
            "SELECT state, COUNT(*) FROM tg_worker_jobs" + clause + " GROUP BY state",
            tuple(params + values)).fetchall())
        outcomes = dict(self.store._execute(
            "SELECT outcome, COUNT(*) FROM tg_worker_jobs" + clause
            + " AND outcome IS NOT NULL GROUP BY outcome",
            tuple(params + values)).fetchall())
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
                if self._amendment_rows(row["id"], ("pending",))]

    def self_resuming(self) -> list[dict[str, Any]]:
        """Stopped work the daemon continues on its own, without being asked."""
        now = iso()
        return self._rows(
            f"state = ? AND outcome IN ({self._marks(SELF_RESUMING)}) "
            "AND (outcome <> ? OR resume_at IS NULL OR resume_at <= ?)",
            [STOPPED, *SELF_RESUMING, QUOTA, now], order="created_at, id")

    def quota_until(self) -> str | None:
        row = self.store._execute(
            "SELECT MAX(resume_at) FROM tg_worker_jobs WHERE project_id = ? AND "
            "environment = ? AND surface = ? AND state = ? AND outcome = ? "
            "AND resume_at > ?",
            (self.project_id, self.environment, self.surface, STOPPED, QUOTA, iso())).fetchone()
        return row[0] if row else None

    def pending_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("delivery_state = ?", ("pending",),
                          order="execution_finished_at, id", limit=limit)

    def claim_deliveries(self, owner_id: str, *, limit: int = 20,
                         lease_seconds: float = 30) -> list[dict[str, Any]]:
        """Lease results to one sender. Delivery is deliberately at-least-once."""
        now = iso()
        until = (datetime.now(timezone.utc)
                 + timedelta(seconds=max(1.0, lease_seconds))).isoformat(
                     timespec="microseconds")
        claimed: list[str] = []
        with self.store.transaction():
            rows = self.store._execute(
                "SELECT id FROM tg_worker_jobs WHERE project_id = ? AND environment = ? "
                "AND surface = ? AND (delivery_state = ? OR (delivery_state = ? "
                "AND delivery_lease_expires_at <= ?)) ORDER BY execution_finished_at, id LIMIT ?",
                (self.project_id, self.environment, self.surface, "pending",
                 "delivering", now, max(1, int(limit)))).fetchall()
            for (job_id,) in rows:
                token = str(uuid4())
                clause, params = self._where(
                    "id = ?", "(delivery_state = ? OR (delivery_state = ? "
                    "AND delivery_lease_expires_at <= ?))")
                cur = self.store._execute(
                    "UPDATE tg_worker_jobs SET delivery_state = ?, delivery_owner = ?, "
                    "delivery_token = ?, delivery_lease_expires_at = ?, "
                    "delivery_attempts = delivery_attempts + 1, updated_at = ?" + clause,
                    tuple(["delivering", owner_id, token, until, now] + params
                          + [job_id, "pending", "delivering", now]))
                if cur.rowcount == 1:
                    claimed.append(job_id)
        return [row for job_id in claimed if (row := self.get(job_id)) is not None]

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
        origin = None if origin_message_id is None else str(origin_message_id)
        if origin is not None:
            existing = self._rows(
                "channel_key = ? AND origin_message_id = ?",
                (str(channel_key), origin), limit=1)
            if existing:
                return existing[0]
        job_id = str(uuid4())
        now = iso()
        try:
            with self.store.transaction():
                self.store._execute(
                    "INSERT INTO tg_worker_jobs (id, project_id, environment, surface, "
                    "channel_key, requested_by, origin_message_id, description, engine, "
                    "model, state, attempt, amendments, stop_requested, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)",
                    (job_id, self.project_id, self.environment, self.surface,
                     str(channel_key), str(requested_by), origin,
                     description, engine, model, DRAFT, now, now))
        except Exception:
            # The unique origin index is the concurrent-delivery fence. If the
            # other registrar won, return that one durable identity; otherwise
            # preserve the actual storage failure.
            if origin is not None:
                existing = self._rows(
                    "channel_key = ? AND origin_message_id = ?",
                    (str(channel_key), origin), limit=1)
                if existing:
                    return existing[0]
            raise
        return self.get(job_id)

    def update(self, job_id: str, **columns: Any) -> dict[str, Any] | None:
        """One scoped write. `updated_at` moves on every one of them, amendments
        included, so the column means what it says."""
        columns.setdefault("updated_at", iso())
        assignments = ", ".join(f"{name} = ?" for name in columns)
        clause, params = self._where("id = ?")
        with self.store.transaction():
            self.store._execute(f"UPDATE tg_worker_jobs SET {assignments}" + clause,
                                tuple(list(columns.values()) + params + [job_id]))
        return self.get(job_id)

    def start(self, job_id: str, *, pid: int | None = None,
              session_id: str | None = None, model: str | None = None,
              log_path: str | None = None, owner_id: str | None = None,
              owner_host: str | None = None, lease_seconds: float = 30) -> dict[str, Any] | None:
        """Take a waiting job into `running`.

        The claim is conditional on the row still waiting, so on a store two
        runners share, whichever updates first owns the job and the other is
        told by getting no row back. The outcome is cleared because it
        described the stop this start has just ended.
        """
        now = iso()
        token = str(uuid4())
        lease_until = (datetime.now(timezone.utc)
                       + timedelta(seconds=max(1.0, lease_seconds))).isoformat(
                           timespec="microseconds")
        clause, params = self._where("id = ?", "state = ?")
        with self.store.transaction():
            cur = self.store._execute(
            "UPDATE tg_worker_jobs SET state = ?, outcome = NULL, "
            "attempt = attempt + 1, attempt_token = ?, lease_owner = ?, "
            "lease_expires_at = ?, owner_host = ?, pid = ?, pgid = ?, "
            "started_at = COALESCE(started_at, ?), updated_at = ?" + clause,
            tuple([RUNNING, token, owner_id, lease_until, owner_host, pid, pid,
                   now, now] + params + [job_id, WAITING]))
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

    def claim_next(self, *, owner_id: str, owner_host: str,
                   max_parallel: int, lease_seconds: float = 30) -> dict[str, Any] | None:
        """Claim the oldest job and one shared slot.

        Slot rows make the parallel budget a store invariant rather than one
        daemon's in-memory count. Expired slot rows are recoverable leases; the
        attempt token on the job fences any late completion from their owner.
        """
        max_parallel = max(1, int(max_parallel))
        now = iso()
        lease_until = (datetime.now(timezone.utc)
                       + timedelta(seconds=max(1.0, lease_seconds))).isoformat(
                           timespec="microseconds")
        token = str(uuid4())
        try:
            with self.store.transaction():
                used = {int(r[0]) for r in self.store._execute(
                    "SELECT slot FROM tg_worker_job_slots WHERE project_id = ? AND "
                    "environment = ? AND surface = ?",
                    (self.project_id, self.environment, self.surface)).fetchall()}
                slot = next((n for n in range(max_parallel) if n not in used), None)
                if slot is None:
                    return None
                candidate = self.store._execute(
                    "SELECT id FROM tg_worker_jobs WHERE project_id = ? AND surface = ? "
                    "AND environment = ? AND state = ? ORDER BY created_at, id LIMIT ?",
                    (self.project_id, self.surface, self.environment, WAITING, 1)).fetchone()
                if not candidate:
                    return None
                job_id = candidate[0]
                self.store._execute(
                    "INSERT INTO tg_worker_job_slots (project_id, environment, surface, "
                    "slot, job_id, owner_id, attempt_token, lease_expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.project_id, self.environment, self.surface, slot, job_id,
                     owner_id, token, lease_until))
                clause, params = self._where("id = ?", "state = ?")
                cur = self.store._execute(
                    "UPDATE tg_worker_jobs SET state = ?, outcome = NULL, "
                    "attempt = attempt + 1, attempt_token = ?, lease_owner = ?, "
                    "lease_expires_at = ?, owner_host = ?, pid = NULL, pgid = NULL, "
                    "started_at = COALESCE(started_at, ?), updated_at = ?" + clause,
                    tuple([RUNNING, token, owner_id, lease_until, owner_host, now, now]
                          + params + [job_id, WAITING]))
                if not cur.rowcount:
                    raise JobError("claim_lost", f"job {job_id} was claimed concurrently")
        except JobError:
            return None
        except Exception as exc:
            # Uniqueness races for the same slot or candidate are ordinary:
            # another daemon got there first. A subsequent tick will retry.
            if ("integrity" in type(exc).__name__.lower()
                    or getattr(exc, "sqlstate", None) == "23505"):
                return None
            raise
        return self.get(job_id)

    def attach_process(self, job_id: str, attempt_token: str, owner_id: str, *, pid: int,
                       pgid: int | None = None) -> bool:
        clause, params = self._where(
            "id = ?", "state = ?", "attempt_token = ?", "lease_owner = ?")
        with self.store.transaction():
            cur = self.store._execute(
                "UPDATE tg_worker_jobs SET pid = ?, pgid = ?, updated_at = ?" + clause,
                tuple([int(pid), int(pgid or pid), iso()] + params
                      + [job_id, RUNNING, attempt_token, owner_id]))
        return bool(cur.rowcount)

    def update_attempt(self, job_id: str, attempt_token: str, owner_id: str,
                       **columns: Any) -> dict[str, Any] | None:
        columns.setdefault("updated_at", iso())
        clause, params = self._where(
            "id = ?", "state = ?", "attempt_token = ?", "lease_owner = ?")
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.store.transaction():
            cur = self.store._execute(
                f"UPDATE tg_worker_jobs SET {assignments}" + clause,
                tuple(list(columns.values()) + params
                      + [job_id, RUNNING, attempt_token, owner_id]))
        return self.get(job_id) if cur.rowcount else None

    def renew(self, job_id: str, attempt_token: str, owner_id: str,
              lease_seconds: float = 30) -> bool:
        until = (datetime.now(timezone.utc)
                 + timedelta(seconds=max(1.0, lease_seconds))).isoformat(
                     timespec="microseconds")
        clause, params = self._where(
            "id = ?", "state = ?", "attempt_token = ?", "lease_owner = ?")
        try:
            with self.store.transaction():
                cur = self.store._execute(
                    "UPDATE tg_worker_jobs SET lease_expires_at = ?, updated_at = ?" + clause,
                    tuple([until, iso()] + params
                          + [job_id, RUNNING, attempt_token, owner_id]))
                if cur.rowcount != 1:
                    raise JobError("lease_lost", f"job {job_id} no longer owns its attempt")
                slot = self.store._execute(
                    "UPDATE tg_worker_job_slots SET lease_expires_at = ? WHERE project_id = ? "
                    "AND environment = ? AND surface = ? AND job_id = ? AND owner_id = ? "
                    "AND attempt_token = ?",
                    (until, self.project_id, self.environment, self.surface, job_id,
                     owner_id, attempt_token))
                if slot.rowcount != 1:
                    raise JobError("lease_lost", f"job {job_id} no longer owns its slot")
        except JobError:
            return False
        return True

    def fence_expired_attempt(
            self, row: dict[str, Any], reason: str,
            before_release: Callable[[], None] | None = None) -> dict[str, Any] | None:
        """Stop exactly the expired attempt observed by a reconciler.

        The row transition and slot release are one transaction. If its owner
        renewed either record after the snapshot, no predicate matches and the
        slot remains unavailable.
        """
        job_id = row["id"]
        token = row.get("attempt_token")
        owner = row.get("lease_owner")
        observed = row.get("lease_expires_at")
        if not token or not owner or not observed:
            return None
        now = iso()
        clause, params = self._where(
            "id = ?", "state = ?", "attempt_token = ?", "lease_owner = ?",
            "lease_expires_at = ?", "lease_expires_at <= ?")
        with self.store.transaction():
            cur = self.store._execute(
                "UPDATE tg_worker_jobs SET state = ?, outcome = ?, pid = NULL, pgid = NULL, "
                "stop_requested = 0, finished_at = ?, execution_finished_at = ?, "
                "error = ?, lease_expires_at = NULL, updated_at = ?" + clause,
                tuple([STOPPED, INTERRUPTED, now, now, str(reason)[:500], now]
                      + params + [job_id, RUNNING, token, owner, observed, now]))
            if cur.rowcount != 1:
                return None
            # A local process group must be stopped while the exact attempt is
            # fenced and its slot is still held. SQLite's BEGIN IMMEDIATE and
            # PostgreSQL's row update keep renew/claim from crossing this
            # callback; only after it returns may the slot become reusable.
            if before_release is not None:
                before_release()
            slot = self.store._execute(
                "DELETE FROM tg_worker_job_slots WHERE project_id = ? AND environment = ? "
                "AND surface = ? AND job_id = ? AND owner_id = ? AND attempt_token = ? "
                "AND lease_expires_at = ?",
                (self.project_id, self.environment, self.surface, job_id,
                 owner, token, observed))
            if slot.rowcount != 1:
                raise JobError("slot_mismatch", f"job {job_id} expired without its exact slot")
            self.store._execute(
                "UPDATE tg_worker_job_amendments SET state = ?, claim_token = NULL, "
                "claimed_at = NULL WHERE project_id = ? AND environment = ? AND surface = ? "
                "AND job_id = ? AND state = ? AND claim_token = ?",
                ("pending", self.project_id, self.environment, self.surface,
                 job_id, "claimed", token))
        return self.get(job_id)

    # -- the amendment hand-off -----------------------------------------------

    def _amendment_rows(self, job_id: str, states: Sequence[str]) -> list[dict[str, Any]]:
        marks = self._marks(states)
        cur = self.store._execute(
            "SELECT id, text, state, claim_token, created_at FROM "
            "tg_worker_job_amendments WHERE project_id = ? AND environment = ? "
            f"AND surface = ? AND job_id = ? AND state IN ({marks}) "
            "ORDER BY created_at, id",
            (self.project_id, self.environment, self.surface, job_id, *states))
        return [{"id": r[0], "text": r[1], "state": r[2],
                 "claim_token": r[3], "created_at": r[4]}
                for r in cur.fetchall()]

    def stage_amendment(self, job_id: str, text: str,
                        *, actor_id: str | None = None) -> str | None:
        """Append one user correction atomically and count exactly that row."""
        text = str(text or "").strip()
        current = self.get(job_id, actor_id=actor_id)
        if not text or current is None:
            return None
        self._refuse_while_delivering(current, "amend")
        amendment_id, now = str(uuid4()), iso()
        predicates = ["id = ?"]
        values: list[Any] = [job_id]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        with self.store.transaction():
            self.store._execute(
                "INSERT INTO tg_worker_job_amendments (id, project_id, environment, "
                "surface, job_id, text, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (amendment_id, self.project_id, self.environment, self.surface,
                 job_id, text, "pending", now))
            cur = self.store._execute(
                "UPDATE tg_worker_jobs SET amendments = amendments + 1, updated_at = ?"
                + clause, tuple([now] + params + values))
            if not cur.rowcount:
                raise JobError("job_not_found", f"no job {job_id} in this actor scope")
        return amendment_id

    def pending_amendment(self, job_id: str,
                          *, actor_id: str | None = None) -> list[str]:
        if self.get(job_id, actor_id=actor_id) is None:
            return []
        return [row["text"] for row in self._amendment_rows(
            job_id, ("pending", "claimed"))]

    def has_pending_amendment(self, job_id: str) -> bool:
        return bool(self._amendment_rows(job_id, ("pending",)))

    def _lock_attempt(self, job_id: str, attempt_token: str, owner_id: str) -> bool:
        """Lock one exact attempt and its slot in the common row-then-slot order."""
        lock = " FOR UPDATE" if self.store.dialect == "postgres" else ""
        job = self.store._execute(
            "SELECT 1 FROM tg_worker_jobs WHERE project_id = ? AND environment = ? "
            "AND surface = ? AND id = ? AND state = ? AND attempt_token = ? "
            "AND lease_owner = ?" + lock,
            (self.project_id, self.environment, self.surface, job_id, RUNNING,
             attempt_token, owner_id)).fetchone()
        if not job:
            return False
        slot = self.store._execute(
            "SELECT 1 FROM tg_worker_job_slots WHERE project_id = ? AND environment = ? "
            "AND surface = ? AND job_id = ? AND attempt_token = ? AND owner_id = ?" + lock,
            (self.project_id, self.environment, self.surface, job_id,
             attempt_token, owner_id)).fetchone()
        return bool(slot)

    def claim_amendments(self, job_id: str, attempt_token: str,
                         owner_id: str) -> list[dict[str, Any]]:
        """Claim pending additions without deleting them.

        A crash leaves immutable rows behind. A later attempt may reclaim them;
        only an explicit engine-acceptance acknowledgement removes them from
        the pending view.
        """
        now = iso()
        with self.store.transaction():
            if not self._lock_attempt(job_id, attempt_token, owner_id):
                return []
            self.store._execute(
                "UPDATE tg_worker_job_amendments SET state = ?, claim_token = ?, "
                "claimed_at = ? WHERE project_id = ? AND environment = ? AND surface = ? "
                "AND job_id = ? AND state = ?",
                ("claimed", attempt_token, now, self.project_id, self.environment,
                 self.surface, job_id, "pending"))
        return [row for row in self._amendment_rows(job_id, ("claimed",))
                if row["claim_token"] == attempt_token]

    def ack_amendments(self, job_id: str, attempt_token: str,
                       owner_id: str) -> int:
        with self.store.transaction():
            if not self._lock_attempt(job_id, attempt_token, owner_id):
                return 0
            cur = self.store._execute(
                "UPDATE tg_worker_job_amendments SET state = ?, acked_at = ? WHERE "
                "project_id = ? AND environment = ? AND surface = ? AND job_id = ? "
                "AND state = ? AND claim_token = ?",
                ("acked", iso(), self.project_id, self.environment, self.surface,
                 job_id, "claimed", attempt_token))
        return int(cur.rowcount or 0)

    def take_amendment(self, job_id: str) -> list[str]:
        """Operator compatibility for idle jobs only; runners use claim/ack."""
        row = self.get(job_id)
        if row is None or row["state"] == RUNNING:
            return []
        pending = self._amendment_rows(job_id, ("pending",))
        if not pending:
            return []
        ids = [item["id"] for item in pending]
        with self.store.transaction():
            self.store._execute(
                f"UPDATE tg_worker_job_amendments SET state = ?, acked_at = ? WHERE "
                f"id IN ({self._marks(ids)}) AND state = ?",
                ("acked", iso(), *ids, "pending"))
        return [item["text"] for item in pending]

    def amend(self, job_id: str, text: str | None = None,
              *, actor_id: str | None = None) -> dict[str, Any] | None:
        """Record that the job was stopped and continued with added context.

        The row keeps its identity and its session; the added text goes to the
        session by way of `stage_amendment`, and the count answers how many
        user additions were made without confusing them with runner batches.
        """
        current = self.get(job_id, actor_id=actor_id)
        if current is None:
            return None
        self._refuse_while_delivering(current, "amend")
        if text:
            self.stage_amendment(job_id, text, actor_id=actor_id)
        if current["state"] == RUNNING:
            # The amendment row is durable intent. Only the daemon that owns
            # this exact attempt may stop its process and transition the job.
            return self.get(job_id, actor_id=actor_id)
        if current["state"] == DRAFT:
            # Amending a draft is the draft being written. Continuing it is
            # what `submit` is for, and doing it here would hand a job over
            # at the very moment somebody said it was not right yet.
            return self.get(job_id, actor_id=actor_id)
        if (current["state"] == STOPPED and current.get("engine") != "stub"
                and not current.get("session_id")):
            # No checkpoint means continuing may repeat work already performed.
            # Keep the addition pending until an explicit resume decision.
            return self.get(job_id, actor_id=actor_id)
        now = iso()
        predicates = ["id = ?"]
        values: list[Any] = [job_id]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        with self.store.transaction():
            self.store._execute(
            "UPDATE tg_worker_jobs SET state = ?, "
            "outcome = NULL, pid = NULL, pgid = NULL, stop_requested = 0, error = NULL, "
            "lease_owner = NULL, lease_expires_at = NULL, "
            "exit_code = NULL, finished_at = NULL, execution_finished_at = NULL, "
            "resume_at = NULL, result_text = NULL, result_silent = 0, "
            "delivery_state = NULL, delivery_attempts = 0, delivery_error = NULL, "
            "delivered_at = NULL, delivery_owner = NULL, delivery_token = NULL, "
            "delivery_lease_expires_at = NULL, updated_at = ?" + clause,
            tuple([WAITING, now] + params + values))
        return self.get(job_id)

    def finish_amendment(self, job_id: str, attempt_token: str,
                         owner_id: str) -> dict[str, Any] | None:
        """Transition an amended attempt after its owner stopped the process."""
        now = iso()
        clause, params = self._where(
            "id = ?", "state = ?", "attempt_token = ?", "lease_owner = ?")
        with self.store.transaction():
            current = self.store._execute(
                "SELECT engine, session_id FROM tg_worker_jobs" + clause,
                tuple(params + [job_id, RUNNING, attempt_token, owner_id])).fetchone()
            if not current:
                return None
            safe = current[0] == "stub" or bool(current[1])
            state, outcome = (WAITING, None) if safe else (STOPPED, INTERRUPTED)
            error = None if safe else (
                "amendment is pending but this attempt exposed no resumable checkpoint")
            cur = self.store._execute(
                "UPDATE tg_worker_jobs SET state = ?, outcome = ?, pid = NULL, pgid = NULL, "
                "stop_requested = 0, error = ?, lease_expires_at = NULL, finished_at = ?, "
                "updated_at = ?" + clause,
                tuple([state, outcome, error, None if safe else now, now]
                      + params + [job_id, RUNNING, attempt_token, owner_id]))
            if cur.rowcount != 1:
                return None
            slot = self.store._execute(
                "DELETE FROM tg_worker_job_slots WHERE project_id = ? AND environment = ? "
                "AND surface = ? AND job_id = ? AND owner_id = ? AND attempt_token = ?",
                (self.project_id, self.environment, self.surface, job_id,
                 owner_id, attempt_token))
            if slot.rowcount != 1:
                raise JobError("slot_mismatch", f"job {job_id} lost its owned slot")
            self.store._execute(
                "UPDATE tg_worker_job_amendments SET state = ?, claim_token = NULL, "
                "claimed_at = NULL WHERE project_id = ? AND environment = ? AND surface = ? "
                "AND job_id = ? AND state = ? AND claim_token = ?",
                ("pending", self.project_id, self.environment, self.surface,
                 job_id, "claimed", attempt_token))
        return self.get(job_id)

    # -- stopping and continuing ----------------------------------------------
    #
    # One way to halt work and one way to continue it. There is no second pair
    # for "stop for good", because the session is the checkpoint and every stop
    # is therefore continuable: what a caller chooses is not whether the work
    # can come back, only whether anybody asks it to.

    def request_stop(self, job_id: str, *, actor_id: str | None = None) -> dict[str, Any] | None:
        """Ask for the work to stop.

        Asynchronous by nature: the runner holds the process group, so this
        records the ask and the next tick acts on it. Without the flag there is
        no state between "asked to stop" and "stopped".
        """
        row = self.get(job_id, actor_id=actor_id)
        if row is None or row["state"] == STOPPED:
            return None
        predicates = ["id = ?"]
        values: list[Any] = [job_id]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        with self.store.transaction():
            cur = self.store._execute(
                "UPDATE tg_worker_jobs SET stop_requested = 1, updated_at = ?" + clause,
                tuple([iso()] + params + values))
        return self.get(job_id, actor_id=actor_id) if cur.rowcount else None

    def discard(self, job_id: str, *, actor_id: str | None = None) -> dict[str, Any] | None:
        """Drop a draft that is never going to be submitted.

        This is not the second half of stopping. A draft holds no session and
        no work: nothing has run, so there is no checkpoint to keep and nothing
        to continue. What it holds is a line in the register that every turn is
        told to read before submitting, so a draft somebody thought better of
        is noise in exactly the surface a turn has to look at. Only a draft can
        be discarded; work that has started stops and stays continuable.
        """
        row = self.get(job_id, actor_id=actor_id)
        if row is None or row["state"] != DRAFT:
            return None
        predicates = ["id = ?", "state = ?"]
        values: list[Any] = [job_id, DRAFT]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        with self.store.transaction():
            cur = self.store._execute(
                "DELETE FROM tg_worker_jobs" + clause, tuple(params + values))
        return row if cur.rowcount else None

    def cancel_waiting(self, job_id: str, *, error: str,
                       result_text: str | None = None) -> dict[str, Any] | None:
        row = self.get(job_id)
        if row is None or row["state"] != WAITING or not row["stop_requested"]:
            return None
        return self.stop(job_id, CANCELLED, error=error, result_text=result_text,
                         expected_state=WAITING, require_stop_requested=True)

    def stop(self, job_id: str, outcome: str, *, exit_code: int | None = None,
             error: str | None = None, session_id: str | None = None,
             model: str | None = None, attempt_token: str | None = None,
             owner_id: str | None = None,
             resume_at: str | None = None, result_text: str | None = None,
             result_silent: bool = False, expected_state: str | None = None,
             require_stop_requested: bool = False) -> dict[str, Any] | None:
        """Land a stop, whatever caused it — the work finishing counts."""
        if outcome not in OUTCOMES:
            raise JobError("bad_outcome",
                           f"outcome {outcome!r} must be one of {', '.join(OUTCOMES)}")
        columns: dict[str, Any] = {
            "state": STOPPED,
            "outcome": outcome,
            "pid": None,
            "pgid": None,
            "stop_requested": 0,
            "finished_at": iso(),
            "execution_finished_at": iso(),
            "exit_code": exit_code,
            "error": None if error is None else str(error)[:500],
            "resume_at": resume_at,
            "lease_expires_at": None,
        }
        if result_text is not None or result_silent:
            columns["result_text"] = result_text
            columns["result_silent"] = 1 if result_silent else 0
            columns["delivery_state"] = "delivered" if result_silent else "pending"
            columns["delivered_at"] = iso() if result_silent else None
            columns["delivery_owner"] = None
            columns["delivery_token"] = None
            columns["delivery_lease_expires_at"] = None
        if session_id is not None:
            columns["session_id"] = session_id
        if model is not None:
            columns["model"] = model
        columns["updated_at"] = iso()
        if attempt_token is None and expected_state is None:
            current = self.get(job_id)
            if (current is not None and current["state"] == RUNNING
                    and current.get("lease_owner")):
                raise JobError(
                    "attempt_fence_required",
                    "a leased running attempt may only be completed by its exact owner")
        predicates = ["id = ?"]
        values: list[Any] = [job_id]
        if attempt_token is not None:
            if not owner_id:
                raise JobError("no_attempt_owner",
                               "a fenced attempt completion requires its lease owner")
            predicates += ["state = ?", "attempt_token = ?", "lease_owner = ?"]
            values += [RUNNING, attempt_token, owner_id]
        elif expected_state is not None:
            predicates.append("state = ?")
            values.append(expected_state)
        if require_stop_requested:
            predicates.append("stop_requested = 1")
        clause, params = self._where(*predicates)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.store.transaction():
            cur = self.store._execute(
                f"UPDATE tg_worker_jobs SET {assignments}" + clause,
                tuple(list(columns.values()) + params + values))
            if cur.rowcount:
                slot_sql = (
                    "DELETE FROM tg_worker_job_slots WHERE project_id = ? AND environment = ? "
                    "AND surface = ? AND job_id = ?")
                slot_params: list[Any] = [self.project_id, self.environment,
                                          self.surface, job_id]
                if attempt_token is not None:
                    slot_sql += " AND attempt_token = ? AND owner_id = ?"
                    slot_params += [attempt_token, owner_id]
                slot = self.store._execute(slot_sql, slot_params)
                if attempt_token is not None and slot.rowcount != 1:
                    raise JobError("slot_mismatch", f"job {job_id} lost its owned slot")
                if attempt_token is not None:
                    self.store._execute(
                        "UPDATE tg_worker_job_amendments SET state = ?, claim_token = NULL, "
                        "claimed_at = NULL WHERE project_id = ? AND environment = ? "
                        "AND surface = ? AND job_id = ? AND state = ? AND claim_token = ?",
                        ("pending", self.project_id, self.environment, self.surface,
                         job_id, "claimed", attempt_token))
        return self.get(job_id) if cur.rowcount else None

    def finish_delivery(self, job_id: str, delivery_token: str, owner_id: str,
                        *, delivered: bool,
                        error: str | None = None) -> dict[str, Any] | None:
        now = iso()
        clause, params = self._where(
            "id = ?", "delivery_state = ?", "delivery_token = ?", "delivery_owner = ?")
        with self.store.transaction():
            cur = self.store._execute(
                "UPDATE tg_worker_jobs SET delivery_state = ?, delivery_error = ?, "
                "delivered_at = ?, delivery_owner = NULL, delivery_token = NULL, "
                "delivery_lease_expires_at = NULL, updated_at = ?" + clause,
                tuple(["delivered" if delivered else "pending",
                       None if delivered else str(error or "delivery failed")[:500],
                       now if delivered else None, now]
                      + params + [job_id, "delivering", delivery_token, owner_id]))
        return self.get(job_id) if cur.rowcount else None

    def submit(self, job_id: str, *, actor_id: str | None = None) -> dict[str, Any] | None:
        """Hand a draft over to the runner.

        The one transition that ends authoring and begins work. It is
        conditional on the row still being a draft, so a second submit of the
        same job is refused rather than silently restarting work already in
        flight, and a job that has moved on is left exactly where it is.
        """
        row = self.get(job_id, actor_id=actor_id)
        if row is None:
            return None
        if row["state"] != DRAFT:
            raise JobError("job_not_draft",
                           f"job {job_id} is {row['state']}, not a draft",
                           "only a draft is submitted; amend or resume the rest")
        return self._actor_update(job_id, actor_id, state=WAITING)

    def resume(self, job_id: str, *, actor_id: str | None = None) -> dict[str, Any] | None:
        """Make sure this job is on its way, from wherever it is.

        The exact counterpart of `request_stop`, and idempotent for the same
        reason it is: both are asks about a direction, not about a transition.
        Stopped work goes back to waiting on the session it already has — the
        same job, same row, same rollout, and one that never started resumes by
        starting. Work that is already moving simply loses a stop nobody wants
        any more. There is no state this refuses, because there is no state a
        job cannot continue from.
        """
        row = self.get(job_id, actor_id=actor_id)
        if row is None:
            return None
        self._refuse_while_delivering(row, "resume")
        if row["state"] != STOPPED:
            if not row["stop_requested"]:
                return row
            return self._actor_update(job_id, actor_id, stop_requested=0)
        return self._actor_update(
            job_id, actor_id, state=WAITING, outcome=None, pid=None,
            pgid=None, stop_requested=0, error=None, exit_code=None,
            finished_at=None, execution_finished_at=None,
            attempt_token=None, lease_owner=None, lease_expires_at=None,
            resume_at=None, result_text=None, result_silent=0,
            delivery_state=None, delivery_attempts=0, delivery_error=None,
            delivered_at=None, delivery_owner=None, delivery_token=None,
            delivery_lease_expires_at=None)

    @staticmethod
    def _refuse_while_delivering(row: dict[str, Any], action: str) -> None:
        if row.get("delivery_state") not in ("pending", "delivering"):
            return
        raise JobError(
            "result_delivery_pending",
            f"cannot {action} job {row['id']} until its existing result is delivered",
            "wait for result delivery, then try again")

    def _actor_update(self, job_id: str, actor_id: str | None,
                      **columns: Any) -> dict[str, Any] | None:
        columns.setdefault("updated_at", iso())
        predicates = ["id = ?"]
        values: list[Any] = [job_id]
        if actor_id is not None:
            predicates.append("requested_by = ?")
            values.append(str(actor_id))
        clause, params = self._where(*predicates)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.store.transaction():
            cur = self.store._execute(
                f"UPDATE tg_worker_jobs SET {assignments}" + clause,
                tuple(list(columns.values()) + params + values))
        return self.get(job_id, actor_id=actor_id) if cur.rowcount else None

    def interrupt_active(self, reason: str) -> list[dict[str, Any]]:
        """Compatibility helper which fences only attempts already expired."""
        rows = self.expired_active()
        interrupted = []
        for row in rows:
            if self.fence_expired_attempt(row, reason) is not None:
                interrupted.append(row)
        return interrupted
