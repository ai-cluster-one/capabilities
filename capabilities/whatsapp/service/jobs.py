"""The job register and the job runner: work that outlives the message that asked for it.

A dialogue turn answers while the person is still there. Work that takes longer
is handed to a job: a row in `whatsapp_jobs`, run by the listener's job runner
in arrival order within `defaults.max_parallel_jobs`, and answered into the
chat it came from, quoting the message that asked for it, when it is done.

WHAT A JOB IS DOING, AND WHY
============================
`state` is what the runner acts on: `draft` (written, not yet handed over; no
runner takes it), `waiting` (handed over, waiting for a slot), `running`
(holding a slot and a lease), `stopped`. `outcome` says why a stopped job
stopped: `succeeded`, `failed`, `cancelled` (somebody asked), `interrupted` (the
listener went away under it), `quota` (the subscription is spent; the runner
continues it when the pause lifts) and `model_refused` (the harness refused the
model; asking again without changing the profile is refused the same way).

The engine's session is the checkpoint. It is recorded the moment the harness
starts, so an amendment, a stop or a restart continues the same session rather
than starting the work again.

ONE OWNER PER ATTEMPT
=====================
A claim takes one slot row in `whatsapp_job_slots` and stamps the job with an
attempt token, an owner and a lease the owner renews. Every write about a
running attempt names the token and the owner, so a late write from an owner
that lost its lease matches nothing. Slot rows make `max_parallel_jobs` a fact
of the store rather than of one process's memory.

A RESULT IS DELIVERED ONCE
==========================
A finished job holds its result with `delivery_state = 'pending'`. Delivering
it flips that state and writes the answer as pending rows in
`whatsapp_messages` in one transaction, so the answer is queued exactly once:
a listener that dies before the commit leaves the result pending for the next
one, and one that dies after it leaves nothing to deliver again.

ISOLATION
=========
Every read and write is scoped by project id, environment and account. The
store is shared by projects and machines, and a query that forgets whose work
it is asking about answers with somebody else's.

Nothing here imports the CLI. The listener hands in `cli`, an object whose
attributes are the CLI's own functions, as it does for the dialogue.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import signal
import socket
import threading
import time
import uuid

SURFACE = "whatsapp"

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
MODEL_REFUSED = "model_refused"
OUTCOMES = (SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, QUOTA, MODEL_REFUSED)

JOB_RECOVERY = ("requeue", "inspect")
DEFAULT_RECOVERY = "requeue"
LEASE_SECONDS = 30.0        # an attempt's lease, renewed on every runner tick
POLL_INTERVAL = 2.0         # how often the runner looks at the register
QUOTA_RETRY_SECONDS = 300.0  # how long a spent quota pauses the queue

HOST = socket.gethostname()


class JobError(Exception):
    """Every refusal this module reports, with a slug the CLI maps onto its
    own error envelope."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug = slug
        self.message = message
        self.hint = hint


class _ClaimLost(Exception):
    """Raised inside a claim's transaction to roll it back."""


def _dict(row) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    return datetime.datetime.fromisoformat(str(value)).timestamp()


def owner_pid(owner_id) -> int | None:
    """The pid an owner id was minted by: `<host>:<pid>:<nanoseconds>`."""
    parts = str(owner_id or "").rsplit(":", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def new_owner_id() -> str:
    return f"{HOST}:{os.getpid()}:{time.time_ns()}"


# ── The register ────────────────────────────────────────────────────────────


class JobRegister:
    """Every question and every claim about jobs, in one place that knows whose."""

    def __init__(self, cli, db, project_id: str, environment: str):
        if not project_id:
            raise JobError("no_project_scope", "the job register needs the project's id")
        self.cli = cli
        self.db = db
        self.project_id = str(project_id)
        self.environment = str(environment)
        self.account = db.account

    # -- the scope, applied once ------------------------------------------------

    def _where(self, *predicates: str) -> tuple[str, list]:
        parts = ["project_id = %s", "environment = %s", "account = %s", *predicates]
        return " WHERE " + " AND ".join(parts), [self.project_id, self.environment,
                                                 self.account]

    def _scope(self) -> list:
        return [self.project_id, self.environment, self.account]

    def _rows(self, predicates: str = "", params=(), order: str = "created_at, id",
              limit: int | None = None) -> list[dict]:
        clause, scope = self._where(*([predicates] if predicates else []))
        sql = "SELECT * FROM whatsapp_jobs" + clause
        if order:
            sql += f" ORDER BY {order}"
        args = scope + list(params)
        if limit is not None:
            sql += " LIMIT %s"
            args.append(int(limit))
        return [_dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _update(self, job_id: str, predicates: list[str], params: list,
                columns: dict) -> dict | None:
        """One scoped write; answers the row it moved, or None when the
        predicates matched nothing."""
        assignments = ", ".join(f"{name} = %s" for name in columns)
        clause, scope = self._where("id = %s", *predicates)
        with self.cli._writing(self.db):
            row = self.db.execute(
                f"UPDATE whatsapp_jobs SET {assignments}, updated_at = now()"
                + clause + " RETURNING *",
                list(columns.values()) + scope + [job_id] + list(params)).fetchone()
        return _dict(row)

    # -- reads --------------------------------------------------------------------

    def get(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        predicates, params = ["id = %s"], [job_id]
        if actor_id is not None:
            predicates.append("requested_by = %s")
            params.append(str(actor_id))
        rows = self._rows(" AND ".join(predicates), params)
        return rows[0] if rows else None

    def list(self, *, limit: int = 50, state: str | None = None,
             outcome: str | None = None, chat_id: str | None = None,
             actor_id: str | None = None) -> list[dict]:
        predicates, params = [], []
        for column, value in (("state", state), ("outcome", outcome),
                              ("chat_id", chat_id), ("requested_by", actor_id)):
            if value is not None:
                predicates.append(f"{column} = %s")
                params.append(str(value))
        return self._rows(" AND ".join(predicates), params,
                          order="created_at DESC, id", limit=limit)

    def open_jobs(self, chat_id: str | None = None, *, actor_id: str | None = None,
                  include_drafts: bool = False) -> list[dict]:
        """What is still in flight, oldest first: for one chat, or everywhere."""
        states = [WAITING, RUNNING] + ([DRAFT] if include_drafts else [])
        predicates, params = ["state = ANY(%s)"], [states]
        if chat_id is not None:
            predicates.append("chat_id = %s")
            params.append(chat_id)
        if actor_id is not None:
            predicates.append("requested_by = %s")
            params.append(str(actor_id))
        return self._rows(" AND ".join(predicates), params)

    def counts(self, *, actor_id: str | None = None) -> dict:
        extra = " AND requested_by = %s" if actor_id is not None else ""
        clause, scope = self._where()
        params = scope + ([str(actor_id)] if actor_id is not None else [])
        states = {r["state"]: r["n"] for r in self.db.execute(
            "SELECT state, count(*) AS n FROM whatsapp_jobs" + clause + extra
            + " GROUP BY state", params).fetchall()}
        outcomes = {r["outcome"]: r["n"] for r in self.db.execute(
            "SELECT outcome, count(*) AS n FROM whatsapp_jobs" + clause + extra
            + " AND outcome IS NOT NULL GROUP BY outcome", params).fetchall()}
        return {"state": states, "outcome": outcomes}

    def slots_in_use(self) -> int:
        return int(self.db.execute(
            "SELECT count(*) AS n FROM whatsapp_job_slots WHERE project_id = %s"
            " AND environment = %s AND account = %s", self._scope()).fetchone()["n"])

    def running(self) -> list[dict]:
        return self._rows("state = %s", [RUNNING], order="started_at, id")

    def stop_pending(self) -> list[dict]:
        return self._rows("stop_requested AND state = ANY(%s)", [[WAITING, RUNNING]])

    def amend_pending(self) -> list[dict]:
        """Running jobs with an amendment waiting for them: the staged text is
        the request to stop and continue."""
        return self._rows(
            "state = %s AND EXISTS (SELECT 1 FROM whatsapp_job_amendments a"
            " WHERE a.project_id = whatsapp_jobs.project_id"
            " AND a.environment = whatsapp_jobs.environment"
            " AND a.account = whatsapp_jobs.account AND a.job_id = whatsapp_jobs.id"
            " AND a.state = 'pending')", [RUNNING], order="started_at, id")

    def quota_paused(self) -> list[dict]:
        """Jobs stopped by a spent quota, whatever their resume time."""
        return self._rows("state = %s AND outcome = %s", [STOPPED, QUOTA])

    def quota_until(self) -> float | None:
        clause, scope = self._where("state = %s", "outcome = %s", "resume_at > now()")
        row = self.db.execute("SELECT max(resume_at) AS until FROM whatsapp_jobs"
                              + clause, scope + [STOPPED, QUOTA]).fetchone()
        return _epoch(row["until"]) if row else None

    def submitted_from(self, chat_id: str, origin_message_id: str) -> dict | None:
        """The job a message was handed to, once it is handed over."""
        rows = self._rows("chat_id = %s AND origin_message_id = %s AND state <> %s",
                          [chat_id, str(origin_message_id), DRAFT], limit=1)
        return rows[0] if rows else None

    def pending_deliveries(self, limit: int = 20) -> list[dict]:
        return self._rows("delivery_state = 'pending'",
                          order="execution_finished_at, id", limit=limit)

    # -- registering and handing over ------------------------------------------

    def register(self, *, chat_id: str, requested_by: str, description: str,
                 origin_message_id: str | None = None, profile: str | None = None,
                 engine: str | None = None, model: str | None = None) -> dict:
        """Write one task as a draft. One origin message carries one job: a
        second registration from it answers with the job it already has."""
        description = str(description or "").strip()
        if not description:
            raise JobError("no_description",
                           "a job needs one line describing it in the requester's terms")
        if not str(chat_id or "").strip():
            raise JobError("no_chat", "a job needs the chat it reports into")
        if not str(requested_by or "").strip():
            raise JobError("no_requester", "a job needs the person whose authority it carries")
        origin = None if origin_message_id in (None, "") else str(origin_message_id)
        if origin is not None:
            existing = self._rows("chat_id = %s AND origin_message_id = %s",
                                  [chat_id, origin], limit=1)
            if existing:
                return existing[0]
        job_id = str(uuid.uuid4())
        with self.cli._writing(self.db):
            row = self.db.execute(
                """INSERT INTO whatsapp_jobs
                       (id, project_id, environment, account, chat_id, surface,
                        requested_by, origin_message_id, description, profile, engine,
                        model, state, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           clock_timestamp(), clock_timestamp())
                   ON CONFLICT DO NOTHING RETURNING *""",
                (job_id, *self._scope(), chat_id, SURFACE, str(requested_by), origin,
                 description, profile, engine, model, DRAFT)).fetchone()
        if row is None and origin is not None:
            # The origin index is the fence against two registrations from one
            # message racing: the other one won, and its row is the answer.
            existing = self._rows("chat_id = %s AND origin_message_id = %s",
                                  [chat_id, origin], limit=1)
            if existing:
                return existing[0]
        if row is None:
            raise JobError("job_not_registered", "the job could not be written")
        return _dict(row)

    def submit(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        row = self.get(job_id, actor_id=actor_id)
        if row is None:
            return None
        if row["state"] != DRAFT:
            raise JobError("job_not_draft", f"job {job_id} is {row['state']}, not a draft",
                           "only a draft is submitted; amend or resume the rest")
        return self._update(job_id, ["state = %s"], [DRAFT], {"state": WAITING})

    def discard(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        row = self.get(job_id, actor_id=actor_id)
        if row is None or row["state"] != DRAFT:
            return None
        clause, scope = self._where("id = %s", "state = %s")
        with self.cli._writing(self.db):
            gone = self.db.execute("DELETE FROM whatsapp_jobs" + clause + " RETURNING id",
                                   scope + [job_id, DRAFT]).fetchone()
        return row if gone else None

    # -- amendments ---------------------------------------------------------------

    def _amendment_rows(self, job_id: str, states) -> list[dict]:
        return [_dict(r) for r in self.db.execute(
            "SELECT * FROM whatsapp_job_amendments WHERE project_id = %s"
            " AND environment = %s AND account = %s AND job_id = %s"
            " AND state = ANY(%s) ORDER BY created_at, id",
            self._scope() + [job_id, list(states)]).fetchall()]

    def pending_amendment(self, job_id: str) -> list[str]:
        return [r["text"] for r in self._amendment_rows(job_id, ("pending", "claimed"))]

    def has_pending_amendment(self, job_id: str) -> bool:
        return bool(self._amendment_rows(job_id, ("pending",)))

    def stage_amendment(self, job_id: str, text: str, *,
                        actor_id: str | None = None) -> str | None:
        text = str(text or "").strip()
        current = self.get(job_id, actor_id=actor_id)
        if not text or current is None:
            return None
        self._refuse_while_delivering(current, "amend")
        amendment_id = str(uuid.uuid4())
        clause, scope = self._where("id = %s")
        with self.cli._writing(self.db):
            self.db.execute(
                """INSERT INTO whatsapp_job_amendments
                       (id, project_id, environment, account, job_id, text, state,
                        created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending', clock_timestamp())""",
                (amendment_id, *self._scope(), job_id, text))
            self.db.execute("UPDATE whatsapp_jobs SET amendments = amendments + 1,"
                            " updated_at = now()" + clause, scope + [job_id])
        return amendment_id

    def amend(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        """After an amendment was staged: a stopped job that can continue goes
        back to waiting. A running one is the runner's to stop; a draft is still
        being written; one without a session would start over, so it waits for
        somebody to resume it."""
        current = self.get(job_id, actor_id=actor_id)
        if current is None:
            return None
        if current["state"] != STOPPED or not current.get("session_id"):
            return current
        return self._continue(job_id)

    def claim_amendments(self, job_id: str, token: str, owner: str) -> list[dict]:
        with self.cli._writing(self.db):
            if not self._holds(job_id, token, owner):
                return []
            self.db.execute(
                "UPDATE whatsapp_job_amendments SET state = 'claimed', claim_token = %s,"
                " claimed_at = now() WHERE project_id = %s AND environment = %s"
                " AND account = %s AND job_id = %s AND state = 'pending'",
                [token] + self._scope() + [job_id])
        return [r for r in self._amendment_rows(job_id, ("claimed",))
                if r["claim_token"] == token]

    def ack_amendments(self, job_id: str, token: str, owner: str) -> int:
        with self.cli._writing(self.db):
            if not self._holds(job_id, token, owner):
                return 0
            done = self.db.execute(
                "UPDATE whatsapp_job_amendments SET state = 'acked', acked_at = now()"
                " WHERE project_id = %s AND environment = %s AND account = %s"
                " AND job_id = %s AND state = 'claimed' AND claim_token = %s"
                " RETURNING id", self._scope() + [job_id, token]).fetchall()
        return len(done)

    def _release_amendments(self, job_id: str, token: str) -> None:
        self.db.execute(
            "UPDATE whatsapp_job_amendments SET state = 'pending', claim_token = NULL,"
            " claimed_at = NULL WHERE project_id = %s AND environment = %s"
            " AND account = %s AND job_id = %s AND state = 'claimed'"
            " AND claim_token = %s", self._scope() + [job_id, token])

    def _holds(self, job_id: str, token: str, owner: str) -> bool:
        """Lock one exact attempt and its slot, row first."""
        clause, scope = self._where("id = %s", "state = %s", "attempt_token = %s",
                                    "lease_owner = %s")
        job = self.db.execute("SELECT 1 FROM whatsapp_jobs" + clause + " FOR UPDATE",
                              scope + [job_id, RUNNING, token, owner]).fetchone()
        if not job:
            return False
        slot = self.db.execute(
            "SELECT 1 FROM whatsapp_job_slots WHERE project_id = %s AND environment = %s"
            " AND account = %s AND job_id = %s AND attempt_token = %s AND owner_id = %s"
            " FOR UPDATE", self._scope() + [job_id, token, owner]).fetchone()
        return bool(slot)

    def finish_amendment(self, job_id: str, token: str, owner: str) -> dict | None:
        """An amended attempt whose process was stopped goes back to waiting, to
        continue its session with the staged text."""
        return self._land(job_id, token, owner, {"state": WAITING, "outcome": None,
                                                 "pid": None, "pgid": None,
                                                 "stop_requested": False, "error": None,
                                                 "lease_expires_at": None})

    # -- stopping and continuing --------------------------------------------------

    def request_stop(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        row = self.get(job_id, actor_id=actor_id)
        if row is None or row["state"] not in (WAITING, RUNNING):
            return None
        return self._update(job_id, [], [], {"stop_requested": True})

    def cancel_waiting(self, job_id: str, *, result_text: str | None = None) -> dict | None:
        columns = self._stop_columns(CANCELLED, error="stopped by request",
                                     result_text=result_text)
        with self.cli._writing(self.db):
            row = self._update(job_id, ["state = %s", "stop_requested"], [WAITING], columns)
        return row

    @staticmethod
    def _stop_columns(outcome: str, *, error=None, exit_code=None, session_id=None,
                      resume_at=None, result_text=None, result_silent=False) -> dict:
        if outcome not in OUTCOMES:
            raise JobError("bad_outcome", f"outcome {outcome!r} must be one of "
                           + ", ".join(OUTCOMES))
        now = datetime.datetime.now(datetime.timezone.utc)
        columns = {"state": STOPPED, "outcome": outcome, "pid": None, "pgid": None,
                   "stop_requested": False, "finished_at": now,
                   "execution_finished_at": now, "exit_code": exit_code,
                   "error": None if error is None else str(error)[:500],
                   "resume_at": resume_at, "lease_expires_at": None}
        if result_text is not None or result_silent:
            columns.update(result_text=result_text, result_silent=bool(result_silent),
                           delivery_state=None if result_silent else "pending",
                           delivered_at=now if result_silent else None)
        if session_id is not None:
            columns["session_id"] = session_id
        return columns

    def finish(self, job_id: str, token: str, owner: str, outcome: str,
               **details) -> dict | None:
        """Land the end of one exact attempt, whatever ended it, and free its
        slot. A writer that no longer owns the attempt changes nothing."""
        return self._land(job_id, token, owner, self._stop_columns(outcome, **details))

    def _land(self, job_id: str, token: str, owner: str, columns: dict) -> dict | None:
        with self.cli._writing(self.db):
            row = self._update(job_id, ["state = %s", "attempt_token = %s",
                                        "lease_owner = %s"],
                               [RUNNING, token, owner], columns)
            if row is None:
                return None
            self.db.execute(
                "DELETE FROM whatsapp_job_slots WHERE project_id = %s AND environment = %s"
                " AND account = %s AND job_id = %s AND attempt_token = %s AND owner_id = %s",
                self._scope() + [job_id, token, owner])
            self._release_amendments(job_id, token)
        return row

    def resume(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        """Put a job back on its way from wherever it is: a stopped one waits
        again on the session it has, a moving one loses a stop nobody wants."""
        row = self.get(job_id, actor_id=actor_id)
        if row is None:
            return None
        self._refuse_while_delivering(row, "resume")
        if row["state"] != STOPPED:
            if not row["stop_requested"]:
                return row
            return self._update(job_id, [], [], {"stop_requested": False})
        return self._continue(job_id)

    def _continue(self, job_id: str) -> dict | None:
        return self._update(job_id, ["state = %s"], [STOPPED], {
            "state": WAITING, "outcome": None, "pid": None, "pgid": None,
            "stop_requested": False, "error": None, "exit_code": None,
            "finished_at": None, "execution_finished_at": None, "attempt_token": None,
            "lease_owner": None, "lease_expires_at": None, "resume_at": None,
            "result_text": None, "result_silent": False, "delivery_state": None,
            "delivery_error": None, "delivered_at": None})

    @staticmethod
    def _refuse_while_delivering(row: dict, action: str) -> None:
        if row.get("delivery_state") == "pending":
            raise JobError("result_delivery_pending",
                           f"cannot {action} job {row['id']} until its result is delivered",
                           "wait for the listener to deliver it, then try again")

    # -- the runner's claim on an attempt -------------------------------------------

    def claim_next(self, *, owner_id: str, host: str, max_parallel: int,
                   lease_seconds: float = LEASE_SECONDS) -> dict | None:
        """Take the oldest waiting job and one free slot, or nothing.

        The candidate is locked and skipped by any concurrent claimant, and the
        slot is a row whose key no two claims can share; whichever claim cannot
        have both rolls back and takes nothing."""
        max_parallel = max(1, int(max_parallel))
        token = str(uuid.uuid4())
        try:
            with self.cli._writing(self.db):
                used = {int(r["slot"]) for r in self.db.execute(
                    "SELECT slot FROM whatsapp_job_slots WHERE project_id = %s"
                    " AND environment = %s AND account = %s", self._scope()).fetchall()}
                slot = next((n for n in range(max_parallel) if n not in used), None)
                if slot is None:
                    return None
                clause, scope = self._where("state = %s")
                candidate = self.db.execute(
                    "SELECT id FROM whatsapp_jobs" + clause
                    + " ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    scope + [WAITING]).fetchone()
                if candidate is None:
                    return None
                job_id = candidate["id"]
                taken = self.db.execute(
                    """INSERT INTO whatsapp_job_slots
                           (project_id, environment, account, slot, job_id, owner_id,
                            attempt_token, lease_expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s,
                               now() + make_interval(secs => %s))
                       ON CONFLICT DO NOTHING RETURNING slot""",
                    self._scope() + [slot, job_id, owner_id, token,
                                     float(lease_seconds)]).fetchone()
                if taken is None:
                    raise _ClaimLost()
                clause, scope = self._where("id = %s", "state = %s")
                row = self.db.execute(
                    "UPDATE whatsapp_jobs SET state = %s, outcome = NULL,"
                    " attempt = attempt + 1, attempt_token = %s, lease_owner = %s,"
                    " lease_expires_at = now() + make_interval(secs => %s), host = %s,"
                    " pid = NULL, pgid = NULL, started_at = COALESCE(started_at, now()),"
                    " updated_at = now()" + clause + " RETURNING *",
                    [RUNNING, token, owner_id, float(lease_seconds), host]
                    + scope + [job_id, WAITING]).fetchone()
                if row is None:
                    raise _ClaimLost()
        except _ClaimLost:
            return None
        return _dict(row)

    def attach_process(self, job_id: str, token: str, owner: str, *, pid,
                       session_id=None, engine=None, model=None) -> bool:
        """Record the harness process and its session the moment it starts."""
        columns = {"pid": pid, "pgid": pid}
        if session_id:
            columns["session_id"] = session_id
        if engine:
            columns["engine"] = engine
        if model:
            columns["model"] = model
        return self._update(job_id, ["state = %s", "attempt_token = %s", "lease_owner = %s"],
                            [RUNNING, token, owner], columns) is not None

    def renew(self, job_id: str, token: str, owner: str,
              lease_seconds: float = LEASE_SECONDS) -> bool:
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=lease_seconds)
        with self.cli._writing(self.db):
            row = self._update(job_id, ["state = %s", "attempt_token = %s",
                                        "lease_owner = %s"], [RUNNING, token, owner],
                               {"lease_expires_at": until})
            if row is None:
                return False
            self.db.execute(
                "UPDATE whatsapp_job_slots SET lease_expires_at = %s WHERE project_id = %s"
                " AND environment = %s AND account = %s AND job_id = %s"
                " AND attempt_token = %s AND owner_id = %s",
                [until] + self._scope() + [job_id, token, owner])
        return True

    def abandoned(self, *, owner_id: str, host: str) -> list[dict]:
        """Running attempts whose owner is gone: the lease ran out, or the owner
        was a process on this machine that no longer exists."""
        now = time.time()
        out = []
        for row in self.running():
            if row.get("lease_owner") == owner_id:
                continue
            expired = (_epoch(row.get("lease_expires_at")) or 0) <= now
            dead = (row.get("host") == host
                    and not pid_alive(owner_pid(row.get("lease_owner"))))
            if expired or dead:
                out.append({**row, "_dead_owner": dead})
        return out

    def fence(self, row: dict, reason: str, *, before_release=None,
              result_text: str | None = None) -> dict | None:
        """Stop exactly the abandoned attempt that was observed, as interrupted,
        and free its slot. An owner that renewed since is left alone."""
        token, owner = row.get("attempt_token"), row.get("lease_owner")
        if not token or not owner:
            return None
        predicates = ["state = %s", "attempt_token = %s", "lease_owner = %s"]
        params = [RUNNING, token, owner]
        if not row.get("_dead_owner"):
            predicates.append("lease_expires_at <= now()")
        columns = self._stop_columns(INTERRUPTED, error=reason, result_text=result_text)
        with self.cli._writing(self.db):
            stopped = self._update(row["id"], predicates, params, columns)
            if stopped is None:
                return None
            if before_release is not None:
                before_release()
            self.db.execute(
                "DELETE FROM whatsapp_job_slots WHERE project_id = %s AND environment = %s"
                " AND account = %s AND job_id = %s AND attempt_token = %s AND owner_id = %s",
                self._scope() + [row["id"], token, owner])
            self._release_amendments(row["id"], token)
        return stopped

    # -- delivery -----------------------------------------------------------------

    def deliver(self, job_id: str, send) -> dict | None:
        """Hand one pending result to `send`, which queues it as messages, in
        the transaction that marks it delivered. Answers the delivered row, or
        None when nothing was pending (another delivery took it)."""
        with self.cli._writing(self.db):
            row = self._update(job_id, ["delivery_state = 'pending'"], [],
                               {"delivery_state": "delivered",
                                "delivered_at": datetime.datetime.now(
                                    datetime.timezone.utc),
                                "delivery_error": None})
            if row is None:
                return None
            sent = send(row) or []
            if sent:
                row = self._update(job_id, [], [], {"delivered_message_id": sent[0]})
        return row

    def delivery_failed(self, job_id: str, error: str) -> None:
        clause, scope = self._where("id = %s", "delivery_state = 'pending'")
        with self.cli._writing(self.db):
            self.db.execute("UPDATE whatsapp_jobs SET delivery_attempts ="
                            " delivery_attempts + 1, delivery_error = %s,"
                            " updated_at = now()" + clause,
                            [str(error)[:500]] + scope + [job_id])


# ── The runner ──────────────────────────────────────────────────────────────


class LiveJob:
    """One attempt this listener is running: its cancel event, and why it was
    asked to end early (`stop`, `amend`, `shutdown`) when it was."""

    def __init__(self, row: dict):
        self.row = row
        self.cancel = threading.Event()
        self.reason: str | None = None
        self.thread: threading.Thread | None = None
        self.pid = None

    def end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.cancel.set()


def _failure_kind(result) -> str:
    failure = getattr(result, "failure", None)
    kind = getattr(failure, "kind", None)
    return str(getattr(kind, "value", kind) or "error")


class JobRunner:
    """The listener's job runner: renews its leases, fences abandoned ones,
    honours stops and amendments, keeps the quota pause, delivers results, and
    claims the oldest waiting job while a slot is free. Each job runs on its own
    thread through the harness runner, from the job's profile."""

    def __init__(self, cli, *, dialogue, db, owner_id: str | None = None,
                 host: str | None = None, run=None, sessions=None, kill_group=None,
                 poll: float = POLL_INTERVAL, lease: float = LEASE_SECONDS,
                 now=time.time):
        self.cli = cli
        self.dialogue = dialogue
        self.db = db
        self.register = JobRegister(cli, db, dialogue.project_id, dialogue.environment)
        self.owner_id = owner_id or new_owner_id()
        self.host = host or HOST
        self._run = run
        self._sessions = sessions
        self.kill_group = kill_group or _kill_group
        self.poll = poll
        self.lease = max(lease, poll * 3)
        self.now = now
        self.live: dict[str, LiveJob] = {}
        self.lock = threading.RLock()
        self.paused_until: float | None = None
        self.pause_reason: str | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.store_error: str | None = None
        self.stats = {"claimed": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
                      "interrupted": 0, "quota": 0, "model_refused": 0, "delivered": 0}

    def log(self, message: str) -> None:
        self.dialogue.log(message)

    # -- settings, read live from the dialogue's policy -------------------------

    def max_parallel(self) -> int:
        return max(1, int(self.dialogue.policy.default("max_parallel_jobs") or 1))

    def recovery(self) -> str:
        value = self.dialogue.policy.default("job_recovery") or DEFAULT_RECOVERY
        return value if value in JOB_RECOVERY else DEFAULT_RECOVERY

    def reachable(self) -> bool:
        """Whether the register answers at all."""
        try:
            self.db.execute("SELECT 1 FROM whatsapp_jobs LIMIT 1")
            return True
        except Exception:
            return False

    def paused(self) -> bool:
        return bool(self.paused_until and self.now() < self.paused_until)

    def pause_notice(self) -> str | None:
        if not self.paused():
            return None
        resumes = datetime.datetime.fromtimestamp(self.paused_until, datetime.timezone.utc)
        return (f"{self.pause_reason or 'The quota is spent.'} Queued work waits; the "
                f"queue resumes at {resumes:%H:%M} UTC.")

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name="job-runner", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stopping.is_set():
            self.tick()
            self.stopping.wait(self.poll)

    def shutdown(self, wait: float = 20.0) -> None:
        """End every job this listener runs as interrupted, continuing each per
        `job_recovery`, and stop looking at the register."""
        self.stopping.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(wait)
        with self.lock:
            live = list(self.live.values())
        for job in live:
            job.end("shutdown")
        deadline = time.monotonic() + wait
        for job in live:
            if job.thread is not None:
                job.thread.join(max(0.0, deadline - time.monotonic()))

    # -- one look at the register -----------------------------------------------

    def tick(self) -> None:
        try:
            if self.db.broken():
                self.db.reconnect()
            self.renew()
            self.reconcile()
            self.honour_asks()
            self.keep_pause()
            self.deliver()
            if not self.stopping.is_set():
                self.claim()
            self.store_error = None
        except Exception as exc:
            line = self.cli._error_line(exc)
            if line != self.store_error:
                self.log(f"jobs: runner tick failed: {line}")
            self.store_error = line

    def renew(self) -> None:
        with self.lock:
            live = list(self.live.values())
        for job in live:
            if not self.register.renew(job.row["id"], job.row["attempt_token"],
                                       self.owner_id, self.lease):
                self.log(f"jobs: {job.row['id']} lost its lease; ending its run")
                job.end("lost")

    def reconcile(self) -> list[dict]:
        """Fence attempts whose owner is gone: interrupted, and continued or
        reported as `job_recovery` says."""
        fenced = []
        for row in self.register.abandoned(owner_id=self.owner_id, host=self.host):
            local = row.get("host") == self.host
            pgid = row.get("pgid") or row.get("pid")

            def stop_group(pgid=pgid, local=local):
                if local and pgid:
                    self.kill_group(int(pgid))

            requeue = self.recovery() == "requeue" and local and bool(row.get("session_id"))
            stopped = self.register.fence(
                row, "the listener running it went away", before_release=stop_group,
                result_text=None if requeue else interrupted_notice(row))
            if stopped is None:
                continue
            fenced.append(stopped)
            self.stats["interrupted"] += 1
            if requeue:
                self.register.resume(row["id"])
                self.log(f"jobs: {row['id']} was interrupted with its listener; "
                         f"continuing session {row['session_id']}")
            else:
                self.log(f"jobs: {row['id']} was interrupted with its listener; "
                         "stopped and reported")
        return fenced

    def honour_asks(self) -> None:
        for row in self.register.stop_pending():
            if row["state"] == WAITING:
                if self.register.cancel_waiting(
                        row["id"], result_text=stopped_notice(row)) is not None:
                    self.stats["cancelled"] += 1
                    self.log(f"jobs: {row['id']} stopped before it started")
            elif row.get("lease_owner") == self.owner_id:
                job = self.live.get(row["id"])
                if job is not None:
                    job.end("stop")
        for row in self.register.amend_pending():
            job = self.live.get(row["id"])
            if row.get("lease_owner") == self.owner_id and job is not None:
                job.end("amend")

    def enter_pause(self, reason: str) -> datetime.datetime:
        self.paused_until = self.now() + QUOTA_RETRY_SECONDS
        self.pause_reason = str(reason or "The quota is spent.")[:200]
        until = datetime.datetime.fromtimestamp(self.paused_until, datetime.timezone.utc)
        self.log(f"jobs: queue paused until {until:%H:%M:%S}Z: {self.pause_reason}")
        return until

    def keep_pause(self) -> None:
        """A pause another listener recorded is honoured here too; once it lifts,
        the jobs it stopped wait again."""
        durable = self.register.quota_until()
        if durable and durable > (self.paused_until or 0):
            self.paused_until = durable
            self.pause_reason = self.pause_reason or "A job found the quota spent."
        if self.paused():
            return
        self.paused_until = self.pause_reason = None
        for row in self.register.quota_paused():
            if (_epoch(row.get("resume_at")) or 0) > self.now():
                continue
            try:
                self.register.resume(row["id"])
            except JobError:
                continue
            self.log(f"jobs: {row['id']} waiting again after the quota pause")

    def deliver(self) -> None:
        for row in self.register.pending_deliveries():
            try:
                done = self.register.deliver(row["id"], self._send)
            except Exception as exc:
                self.register.delivery_failed(row["id"], self.cli._error_line(exc))
                self.log(f"jobs: {row['id']} result not delivered yet: "
                         f"{self.cli._error_line(exc)}")
                continue
            if done is not None:
                self.stats["delivered"] += 1
                self.log(f"jobs: {row['id']} result delivered to {row['chat_id']}"
                         f" quoting #{row.get('origin_message_id')}")

    def _send(self, row: dict) -> list[str]:
        """Queue one result as pending messages, the first quoting the message
        that asked for the work. Runs inside the delivery's transaction."""
        sent = []
        for index, part in enumerate(self.dialogue.split_text(row.get("result_text") or "")):
            request = {"chat_id": row["chat_id"], "text": part, "mentions": [],
                       "typing": index == 0,
                       "reply_to": row.get("origin_message_id") if index == 0 else None}
            try:
                queued = self.cli._queue_outgoing(self.db, request)
            except self.cli._Refusal as refusal:
                if refusal.code != "quoted_not_found":
                    raise
                request["reply_to"] = None
                queued = self.cli._queue_outgoing(self.db, request)
            sent.append(queued["local_id"])
        return sent

    def claim(self) -> None:
        while not self.paused() and not self.stopping.is_set():
            with self.lock:
                if len(self.live) >= self.max_parallel():
                    return
            row = self.register.claim_next(owner_id=self.owner_id, host=self.host,
                                           max_parallel=self.max_parallel(),
                                           lease_seconds=self.lease)
            if row is None:
                return
            self.stats["claimed"] += 1
            job = LiveJob(row)
            with self.lock:
                self.live[row["id"]] = job
            job.thread = threading.Thread(target=self.run_job, args=(job,),
                                          name=f"job-{row['id'][:8]}", daemon=True)
            job.thread.start()

    # -- the chat's view ------------------------------------------------------------

    def open_jobs(self, chat_id: str) -> list[dict]:
        return self.register.open_jobs(chat_id)

    def stop_chat(self, chat_id: str) -> bool:
        """`/stop`: stop the chat's waiting and running jobs. A running one
        here is ended at once; the rest are asked through the register."""
        stopped = False
        for row in self.register.open_jobs(chat_id):
            if self.register.request_stop(row["id"]) is None:
                continue
            stopped = True
            job = self.live.get(row["id"])
            if job is not None:
                job.end("stop")
        return stopped

    def summary(self) -> dict:
        with self.lock:
            running = len(self.live)
        return {"running": running, "max_parallel": self.max_parallel(),
                "paused_until": (datetime.datetime.fromtimestamp(
                    self.paused_until, datetime.timezone.utc).isoformat(timespec="seconds")
                    if self.paused() else None),
                "store_error": self.store_error, **self.stats}

    # -- one job ------------------------------------------------------------------

    def runner_run(self):
        if self._run is not None:
            return self._run
        return self.dialogue.profiles.runner().run

    def session_for(self, session_id):
        if self._sessions is not None:
            fresh, resume = self._sessions
            return resume(session_id) if session_id else fresh()
        lib = self.dialogue.profiles.runner()
        return lib.Session.resume(session_id) if session_id else lib.Session.fresh()

    def run_job(self, job: LiveJob) -> None:
        """One attempt, start to landing. Every ending is written down against
        the exact attempt, and none of them escapes the thread."""
        row = job.row
        job_id, token, owner = row["id"], row["attempt_token"], self.owner_id
        reg = self.register
        authority_file = None
        try:
            amendments = reg.claim_amendments(job_id, token, owner)
            call = self.dialogue.job_call(row, [a["text"] for a in amendments])
            harness = call["origin"].get("harness")
            if row.get("engine") and harness and row["engine"] != harness:
                self._fail(job, FAILED,
                           f"the job's session belongs to {row['engine']} and its profile "
                           f"{call['origin']['name']} now runs {harness}")
                return
            if call["authority"] is not None:
                authority_file = self.dialogue.write_authority(call["authority"],
                                                               f"job-{job_id}")
                call["extra_env"]["CAPABILITIES_AUTH_CONTEXT"] = authority_file
            current = reg.get(job_id)
            if current is not None and current.get("stop_requested"):
                job.end("stop")
            if job.cancel.is_set():
                self._land_early(job)
                return
            self.log(f"jobs: {job_id} attempt {row['attempt']} started on "
                     f"{call['origin']['name']} ({harness})"
                     + (f", continuing session {row['session_id']}"
                        if row.get("session_id") else ""))

            def on_start(started):
                job.pid = getattr(started, "pid", None)
                accepted = reg.attach_process(
                    job_id, token, owner, pid=job.pid,
                    session_id=getattr(started, "session_id", None),
                    engine=getattr(started, "harness", None) or harness,
                    model=call["origin"].get("model"))
                if not accepted:
                    job.end("lost")
                elif amendments:
                    reg.ack_amendments(job_id, token, owner)

            result = self.runner_run()(
                call["prompt"], call["profile"], call["cwd"],
                session=self.session_for(row.get("session_id")),
                environ=call["environ"], extra_env=call["extra_env"],
                cancel=job.cancel, on_start=on_start)
            session_id = getattr(result, "session_id", None)
            if job.reason is not None:
                self._land_early(job, session_id=session_id)
                return
            current = reg.get(job_id) or row
            if current.get("stop_requested"):
                job.reason = "stop"
                self._land_early(job, session_id=session_id)
                return
            if reg.has_pending_amendment(job_id):
                job.reason = "amend"
                self._land_early(job, session_id=session_id)
                return
            if getattr(result, "ok", False):
                answer = self.dialogue.cut_answer(getattr(result, "answer", "") or "")
                reg.finish(job_id, token, owner, SUCCEEDED, exit_code=0,
                           session_id=session_id, result_text=answer or None,
                           result_silent=not answer)
                self.stats["succeeded"] += 1
                self.log(f"jobs: {job_id} succeeded"
                         + ("" if answer else " silently"))
                return
            kind = _failure_kind(result)
            message = str(getattr(getattr(result, "failure", None), "message", "") or kind)
            if kind == QUOTA:
                until = self.enter_pause(message)
                reg.finish(job_id, token, owner, QUOTA, error=message,
                           session_id=session_id, resume_at=until,
                           result_text=self.pause_notice())
                self.stats["quota"] += 1
                return
            self._fail(job, MODEL_REFUSED if kind == MODEL_REFUSED else FAILED,
                       f"{kind}: {message}", session_id=session_id)
        except Exception as exc:
            self.log(f"jobs: {job_id} error: {type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                self._fail(job, FAILED, f"error: {type(exc).__name__}: {exc}")
        finally:
            if authority_file:
                with contextlib.suppress(OSError):
                    os.unlink(authority_file)
            with self.lock:
                self.live.pop(job_id, None)

    def _fail(self, job: LiveJob, outcome: str, detail: str, *, session_id=None) -> None:
        row = job.row
        role = self.dialogue.job_role(row)
        text = (f"Job failed: «{row['description']}»\n{detail}" if role == "supervisor"
                else f"«{row['description']}» could not be completed. Please tell an "
                     "administrator.")
        self.register.finish(row["id"], row["attempt_token"], self.owner_id, outcome,
                             error=detail, session_id=session_id, result_text=text)
        self.stats[outcome] = self.stats.get(outcome, 0) + 1
        self.log(f"jobs: {row['id']} {outcome}: {detail}"[:600])

    def _land_early(self, job: LiveJob, *, session_id=None) -> None:
        """A run that ended because it was asked to: stopped, amended, its
        listener stopping, or its lease lost."""
        row = job.row
        reg = self.register
        if job.reason == "lost":
            self.log(f"jobs: {row['id']} ended without its lease; its next owner decides")
            return
        if job.reason == "amend":
            if reg.finish_amendment(row["id"], row["attempt_token"], self.owner_id):
                if session_id and not (reg.get(row["id"]) or {}).get("session_id"):
                    reg._update(row["id"], [], [], {"session_id": session_id})
                self.log(f"jobs: {row['id']} amended; continuing its session")
            return
        if job.reason == "shutdown":
            current = reg.get(row["id"]) or row
            has_session = bool(session_id or current.get("session_id"))
            requeue = self.recovery() == "requeue" and has_session
            stopped = reg.finish(row["id"], row["attempt_token"], self.owner_id,
                                 INTERRUPTED, error="the listener stopped",
                                 session_id=session_id,
                                 result_text=None if requeue else interrupted_notice(row))
            if stopped is not None:
                self.stats["interrupted"] += 1
                if requeue:
                    reg.resume(row["id"])
                self.log(f"jobs: {row['id']} interrupted by the listener stopping; "
                         + ("requeued" if requeue else "stopped and reported"))
            return
        if reg.finish(row["id"], row["attempt_token"], self.owner_id, CANCELLED,
                      error="stopped by request", session_id=session_id,
                      result_text=stopped_notice(row)) is not None:
            self.stats["cancelled"] += 1
            self.log(f"jobs: {row['id']} stopped by request")


def stopped_notice(row: dict) -> str:
    return f"Stopped: «{row['description']}». It keeps its place and can be continued."


def interrupted_notice(row: dict) -> str:
    return (f"Interrupted: «{row['description']}» did not finish because the service "
            "restarted. It keeps its place and can be continued.")


def _kill_group(pgid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(int(pgid), sig)
