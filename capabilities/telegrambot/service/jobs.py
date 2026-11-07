"""The job register and the job runner: work that outlives the message that asked for it.

A dialogue turn answers while the person is still there. Work that takes longer
is handed to a job: a row in `telegrambot_jobs`, run by the bot's job runner in
arrival order within `defaults.max_parallel_jobs`, and answered into the chat
(and topic) it came from, replying to the message that asked for it, when it is
done. The row keeps the job's brief and its result until the result is
delivered; it is a record of work, never a copy of the conversation.

WHAT A JOB IS DOING
===================
`state` is what the runner acts on: `draft` (written, not handed over; no runner
takes it), `waiting` (handed over, waiting for a slot), `running` (holding a slot
and a lease), `stopped`. `outcome` says why a stopped job stopped: `succeeded`,
`failed`, `cancelled` (somebody asked), `interrupted` (the service went away
under it), `quota` (the subscription is spent; the runner continues it when the
pause lifts) and `model_refused`.

The engine's session is the checkpoint, recorded the moment the harness starts,
so an amendment, a stop or a restart continues the same session.

ONE OWNER PER ATTEMPT
=====================
A claim takes one slot row in `telegrambot_job_slots` and stamps the job with an
attempt token, an owner and a lease the owner renews. Every write about a
running attempt names the token and the owner, so a late write from an owner
that lost its lease matches nothing.

A RESULT IS DELIVERED ONCE
==========================
A finished job holds its result with `delivery_state = 'pending'`. Delivery marks
it delivered and then sends it; a send Telegram definitely refused puts it back
to pending, and a send whose outcome is unknown is not repeated.

Every read and write is scoped by agent id and connection: the store is shared
by agents and machines. Nothing here imports the CLI.
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

DRAFT, WAITING, RUNNING, STOPPED = "draft", "waiting", "running", "stopped"
STATES = (DRAFT, WAITING, RUNNING, STOPPED)
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"
INTERRUPTED, QUOTA, MODEL_REFUSED = "interrupted", "quota", "model_refused"
OUTCOMES = (SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, QUOTA, MODEL_REFUSED)
JOB_RECOVERY = ("requeue", "inspect")
LEASE_SECONDS = 30.0
POLL_INTERVAL = 2.0
QUOTA_RETRY_SECONDS = 300.0
HOST = socket.gethostname()

STEPS = [
    ("0004-jobs", """
CREATE TABLE telegrambot_jobs (
  id text PRIMARY KEY,
  agent_id text NOT NULL CHECK (agent_id <> ''),
  connection text NOT NULL CHECK (connection <> ''),
  chat_id bigint NOT NULL,
  topic_id bigint NOT NULL DEFAULT 0,
  requested_by text NOT NULL CHECK (requested_by <> ''),
  origin_message_id bigint,
  external boolean NOT NULL DEFAULT false,
  description text NOT NULL CHECK (description <> ''),
  profile text, engine text, model text, session_id text,
  node text, host text, pid integer, pgid integer,
  state text NOT NULL CHECK (state IN ('draft', 'waiting', 'running', 'stopped')),
  outcome text CHECK (outcome IN ('succeeded', 'failed', 'cancelled', 'interrupted',
                                  'quota', 'model_refused')),
  attempt integer NOT NULL DEFAULT 0,
  amendments integer NOT NULL DEFAULT 0,
  stop_requested boolean NOT NULL DEFAULT false,
  exit_code integer, error text,
  attempt_token text, lease_owner text, lease_expires_at timestamptz,
  resume_at timestamptz,
  result_text text, result_silent boolean NOT NULL DEFAULT false,
  delivery_state text CHECK (delivery_state IN ('pending', 'delivered')),
  delivery_attempts integer NOT NULL DEFAULT 0,
  delivery_error text, delivered_at timestamptz, delivered_message_id bigint,
  created_at timestamptz NOT NULL, started_at timestamptz,
  finished_at timestamptz, execution_finished_at timestamptz,
  updated_at timestamptz NOT NULL);
CREATE INDEX telegrambot_jobs_queue ON telegrambot_jobs (agent_id, connection, state, created_at);
CREATE INDEX telegrambot_jobs_by_chat
  ON telegrambot_jobs (agent_id, connection, chat_id, topic_id, state);
CREATE UNIQUE INDEX telegrambot_jobs_live_session ON telegrambot_jobs (session_id)
  WHERE state = 'running';
CREATE UNIQUE INDEX telegrambot_jobs_by_origin
  ON telegrambot_jobs (agent_id, connection, chat_id, topic_id, origin_message_id)
  WHERE origin_message_id IS NOT NULL;
COMMENT ON TABLE telegrambot_jobs IS
  'Work a telegrambot turn handed to a background job: its brief, its run and its result until delivered. Not a copy of the conversation.';
COMMENT ON COLUMN telegrambot_jobs.requested_by IS
  'The Telegram user id whose request (and authority) the job carries.';
COMMENT ON COLUMN telegrambot_jobs.external IS
  'The request ended with #external: everything the service sends for it ends with #noreply.';
COMMENT ON COLUMN telegrambot_jobs.session_id IS
  'The harness session, the job''s checkpoint: a stop, an amendment or a restart continues it.';
"""),
    ("0005-job-amendments", """
CREATE TABLE telegrambot_job_amendments (
  id text PRIMARY KEY,
  agent_id text NOT NULL CHECK (agent_id <> ''),
  connection text NOT NULL CHECK (connection <> ''),
  job_id text NOT NULL,
  text text NOT NULL,
  state text NOT NULL CHECK (state IN ('pending', 'claimed', 'acked')),
  claim_token text,
  created_at timestamptz NOT NULL, claimed_at timestamptz, acked_at timestamptz);
CREATE INDEX telegrambot_job_amendments_pending
  ON telegrambot_job_amendments (agent_id, connection, job_id, state, created_at);
COMMENT ON TABLE telegrambot_job_amendments IS
  'Text added to a telegrambot job while it runs, kept until the engine has taken it.';
"""),
    ("0006-job-slots", """
CREATE TABLE telegrambot_job_slots (
  agent_id text NOT NULL CHECK (agent_id <> ''),
  connection text NOT NULL CHECK (connection <> ''),
  slot integer NOT NULL,
  job_id text NOT NULL,
  owner_id text NOT NULL,
  attempt_token text NOT NULL,
  lease_expires_at timestamptz NOT NULL,
  PRIMARY KEY (agent_id, connection, slot),
  UNIQUE (agent_id, connection, job_id));
COMMENT ON TABLE telegrambot_job_slots IS
  'One row per running attempt holding one of a bot''s max_parallel_jobs slots, so the budget is a fact of the store.';
"""),
]


# A row names its bot by id in `connection_id` where the store holds that
# connection, else in `file_connection` (service/store.py); an insert gives the
# id three times (`JobRegister._written`).
_HELD = ("(SELECT k.id FROM connections k WHERE k.id = %s"
         " AND k.capability = 'telegrambot')")
CONNECTION_VALUES = f"{_HELD}, nullif(%s, {_HELD})"


class JobError(Exception):
    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug, self.message, self.hint = slug, message, hint


class _ClaimLost(Exception):
    pass


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    return datetime.datetime.fromisoformat(str(value)).timestamp()


def owner_pid(owner_id) -> int | None:
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


class Db:
    """The store as the register asks it: `execute` answers rows as dicts, and a
    `writing()` block is one transaction for the thread inside it."""

    def __init__(self, store):
        self.store = store
        self.local = threading.local()

    @contextlib.contextmanager
    def writing(self):
        if getattr(self.local, "cur", None) is not None:
            yield
            return
        with self.store.transaction() as cur:
            self.local.cur = cur
            try:
                yield
            finally:
                self.local.cur = None

    def execute(self, sql: str, params=()):
        cur = getattr(self.local, "cur", None)
        if cur is not None:
            cur.execute(sql, params)
            return _Result(cur)
        with self.store._operation() as conn, conn.cursor() as fresh:
            fresh.execute(sql, params)
            return _Result(fresh)


class _Result:
    def __init__(self, cur):
        self.rows = ([{d.name: v for d, v in zip(cur.description, row)}
                      for row in cur.fetchall()] if cur.description else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


# ── The register ────────────────────────────────────────────────────────────


class JobRegister:
    """Every question and every claim about one bot's jobs."""

    def __init__(self, db: Db, agent_id: str, connection: str):
        if not agent_id:
            raise JobError("no_project_scope", "the job register needs the agent's id")
        self.db, self.agent_id, self.connection = db, str(agent_id), str(connection)

    def _where(self, *predicates: str) -> tuple[str, list]:
        parts = ["agent_id = %s", "coalesce(connection_id, file_connection) = %s", *predicates]
        return " WHERE " + " AND ".join(parts), [self.agent_id, self.connection]

    def _scope(self) -> list:
        return [self.agent_id, self.connection]

    def _written(self) -> list:
        """The scope as an insert takes it: the agent, then the connection's id
        for `connection_id` where the store holds it, else for
        `file_connection` (`CONNECTION_VALUES`)."""
        return [self.agent_id, self.connection, self.connection, self.connection]

    def _rows(self, predicates: str = "", params=(), order: str = "created_at, id",
              limit: int | None = None) -> list[dict]:
        clause, scope = self._where(*([predicates] if predicates else []))
        sql = "SELECT * FROM telegrambot_jobs" + clause
        if order:
            sql += f" ORDER BY {order}"
        args = scope + list(params)
        if limit is not None:
            sql += " LIMIT %s"
            args.append(int(limit))
        return self.db.execute(sql, args).fetchall()

    def _update(self, job_id: str, predicates: list[str], params: list,
                columns: dict) -> dict | None:
        assignments = ", ".join(f"{name} = %s" for name in columns)
        clause, scope = self._where("id = %s", *predicates)
        with self.db.writing():
            return self.db.execute(
                f"UPDATE telegrambot_jobs SET {assignments}, updated_at = now()"
                + clause + " RETURNING *",
                list(columns.values()) + scope + [job_id] + list(params)).fetchone()

    # -- reads ------------------------------------------------------------------

    def get(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        predicates, params = ["id = %s"], [job_id]
        if actor_id is not None:
            predicates.append("requested_by = %s")
            params.append(str(actor_id))
        rows = self._rows(" AND ".join(predicates), params)
        return rows[0] if rows else None

    def find(self, prefix: str, *, actor_id: str | None = None) -> dict | None:
        """A job by its id or a unique prefix of six or more characters."""
        found = self.get(prefix, actor_id=actor_id)
        if found or len(prefix) < 6:
            return found
        params, extra = [prefix + "%"], ""
        if actor_id is not None:
            extra, params = " AND requested_by = %s", params + [str(actor_id)]
        rows = self._rows("id LIKE %s" + extra, params, limit=2)
        return rows[0] if len(rows) == 1 else None

    def list(self, *, limit: int = 50, state: str | None = None, outcome: str | None = None,
             chat_id: int | None = None, topic_id: int | None = None,
             actor_id: str | None = None) -> list[dict]:
        predicates, params = [], []
        for column, value in (("state", state), ("outcome", outcome), ("chat_id", chat_id),
                              ("topic_id", topic_id), ("requested_by", actor_id)):
            if value is not None:
                predicates.append(f"{column} = %s")
                params.append(value if column in ("chat_id", "topic_id") else str(value))
        return self._rows(" AND ".join(predicates), params, order="created_at DESC, id",
                          limit=limit)

    def open_jobs(self, chat_id: int | None = None, topic_id: int | None = None, *,
                  actor_id: str | None = None, include_drafts: bool = False) -> list[dict]:
        states = [WAITING, RUNNING] + ([DRAFT] if include_drafts else [])
        predicates, params = ["state = ANY(%s)"], [states]
        if chat_id is not None:
            predicates.append("chat_id = %s")
            params.append(chat_id)
        if topic_id is not None:
            predicates.append("topic_id = %s")
            params.append(topic_id)
        if actor_id is not None:
            predicates.append("requested_by = %s")
            params.append(str(actor_id))
        return self._rows(" AND ".join(predicates), params)

    def running(self) -> list[dict]:
        return self._rows("state = %s", [RUNNING], order="started_at, id")

    def stop_pending(self) -> list[dict]:
        return self._rows("stop_requested AND state = ANY(%s)", [[WAITING, RUNNING]])

    def amend_pending(self) -> list[dict]:
        return self._rows(
            "state = %s AND EXISTS (SELECT 1 FROM telegrambot_job_amendments a"
            " WHERE a.agent_id = telegrambot_jobs.agent_id"
            " AND coalesce(a.connection_id, a.file_connection) = coalesce(telegrambot_jobs.connection_id, telegrambot_jobs.file_connection)"
            " AND a.job_id = telegrambot_jobs.id AND a.state = 'pending')",
            [RUNNING], order="started_at, id")

    def quota_paused(self) -> list[dict]:
        return self._rows("state = %s AND outcome = %s", [STOPPED, QUOTA])

    def quota_until(self) -> float | None:
        clause, scope = self._where("state = %s", "outcome = %s", "resume_at > now()")
        row = self.db.execute("SELECT max(resume_at) AS until FROM telegrambot_jobs" + clause,
                              scope + [STOPPED, QUOTA]).fetchone()
        return _epoch(row["until"]) if row else None

    def submitted_from(self, chat_id: int, topic_id: int, origin_message_id: int) -> dict | None:
        rows = self._rows("chat_id = %s AND topic_id = %s AND origin_message_id = %s"
                          " AND state <> %s", [chat_id, topic_id, origin_message_id, DRAFT],
                          limit=1)
        return rows[0] if rows else None

    def pending_deliveries(self, limit: int = 20) -> list[dict]:
        return self._rows("delivery_state = 'pending'", order="execution_finished_at, id",
                          limit=limit)

    # -- registering and handing over -----------------------------------------

    def register(self, *, chat_id: int, topic_id: int = 0, requested_by: str,
                 description: str, origin_message_id: int | None = None,
                 profile: str | None = None, engine: str | None = None,
                 external: bool = False) -> dict:
        """Write one task as a draft. One origin message carries one job: a second
        registration from it answers with the job it already has."""
        description = str(description or "").strip()
        if not description:
            raise JobError("no_description", "a job needs one line describing it in the "
                           "requester's terms")
        if not str(requested_by or "").strip():
            raise JobError("no_requester", "a job needs the person whose authority it carries")
        if origin_message_id is not None:
            existing = self._rows("chat_id = %s AND topic_id = %s AND origin_message_id = %s",
                                  [chat_id, topic_id, origin_message_id], limit=1)
            if existing:
                return existing[0]
        job_id = str(uuid.uuid4())
        with self.db.writing():
            row = self.db.execute(
                """INSERT INTO telegrambot_jobs
                       (id, agent_id, connection_id, file_connection, chat_id, topic_id,
                        requested_by,
                        origin_message_id, external, description, profile, engine, state,
                        created_at, updated_at)
                   VALUES (%s, %s, """ + CONNECTION_VALUES + """, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, clock_timestamp(), clock_timestamp())
                   ON CONFLICT DO NOTHING RETURNING *""",
                (job_id, *self._written(), chat_id, topic_id, str(requested_by),
                 origin_message_id, bool(external), description, profile, engine,
                 DRAFT)).fetchone()
        if row is None and origin_message_id is not None:
            existing = self._rows("chat_id = %s AND topic_id = %s AND origin_message_id = %s",
                                  [chat_id, topic_id, origin_message_id], limit=1)
            if existing:
                return existing[0]
        if row is None:
            raise JobError("job_not_registered", "the job could not be written")
        return row

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
        with self.db.writing():
            gone = self.db.execute("DELETE FROM telegrambot_jobs" + clause + " RETURNING id",
                                   scope + [job_id, DRAFT]).fetchone()
        return row if gone else None

    # -- amendments ---------------------------------------------------------------

    def _amendment_rows(self, job_id: str, states) -> list[dict]:
        return self.db.execute(
            "SELECT * FROM telegrambot_job_amendments WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
            " AND job_id = %s AND state = ANY(%s) ORDER BY created_at, id",
            self._scope() + [job_id, list(states)]).fetchall()

    def pending_amendment(self, job_id: str) -> list[str]:
        return [r["text"] for r in self._amendment_rows(job_id, ("pending", "claimed"))]

    def has_pending_amendment(self, job_id: str) -> bool:
        return bool(self._amendment_rows(job_id, ("pending",)))

    def stage_amendment(self, job_id: str, text: str, *, actor_id: str | None = None):
        text = str(text or "").strip()
        current = self.get(job_id, actor_id=actor_id)
        if not text or current is None:
            return None
        self._refuse_while_delivering(current, "amend")
        amendment_id = str(uuid.uuid4())
        clause, scope = self._where("id = %s")
        with self.db.writing():
            self.db.execute(
                "INSERT INTO telegrambot_job_amendments (id, agent_id, connection_id,"
                " file_connection, job_id, text, state, created_at) VALUES (%s, %s,"
                f" {CONNECTION_VALUES}, %s, %s, 'pending', clock_timestamp())",
                (amendment_id, *self._written(), job_id, text))
            self.db.execute("UPDATE telegrambot_jobs SET amendments = amendments + 1,"
                            " updated_at = now()" + clause, scope + [job_id])
        return amendment_id

    def amend(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
        current = self.get(job_id, actor_id=actor_id)
        if current is None:
            return None
        if current["state"] != STOPPED or not current.get("session_id"):
            return current
        return self._continue(job_id)

    def claim_amendments(self, job_id: str, token: str, owner: str) -> list[dict]:
        with self.db.writing():
            if not self._holds(job_id, token, owner):
                return []
            self.db.execute(
                "UPDATE telegrambot_job_amendments SET state = 'claimed', claim_token = %s,"
                " claimed_at = now() WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
                " AND job_id = %s AND state = 'pending'", [token] + self._scope() + [job_id])
        return [r for r in self._amendment_rows(job_id, ("claimed",))
                if r["claim_token"] == token]

    def ack_amendments(self, job_id: str, token: str, owner: str) -> int:
        with self.db.writing():
            if not self._holds(job_id, token, owner):
                return 0
            done = self.db.execute(
                "UPDATE telegrambot_job_amendments SET state = 'acked', acked_at = now()"
                " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND job_id = %s"
                " AND state = 'claimed' AND claim_token = %s RETURNING id",
                self._scope() + [job_id, token]).fetchall()
        return len(done)

    def _release_amendments(self, job_id: str, token: str) -> None:
        self.db.execute(
            "UPDATE telegrambot_job_amendments SET state = 'pending', claim_token = NULL,"
            " claimed_at = NULL WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND job_id = %s"
            " AND state = 'claimed' AND claim_token = %s", self._scope() + [job_id, token])

    def _holds(self, job_id: str, token: str, owner: str) -> bool:
        clause, scope = self._where("id = %s", "state = %s", "attempt_token = %s",
                                    "lease_owner = %s")
        job = self.db.execute("SELECT 1 AS one FROM telegrambot_jobs" + clause + " FOR UPDATE",
                              scope + [job_id, RUNNING, token, owner]).fetchone()
        if not job:
            return False
        slot = self.db.execute(
            "SELECT 1 AS one FROM telegrambot_job_slots WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
            " AND job_id = %s AND attempt_token = %s AND owner_id = %s FOR UPDATE",
            self._scope() + [job_id, token, owner]).fetchone()
        return bool(slot)

    def finish_amendment(self, job_id: str, token: str, owner: str) -> dict | None:
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
        return self._update(job_id, ["state = %s", "stop_requested"], [WAITING], columns)

    @staticmethod
    def _stop_columns(outcome: str, *, error=None, exit_code=None, session_id=None,
                      resume_at=None, result_text=None, result_silent=False) -> dict:
        if outcome not in OUTCOMES:
            raise JobError("bad_outcome", f"outcome {outcome!r} is not one of "
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
        return self._land(job_id, token, owner, self._stop_columns(outcome, **details))

    def _land(self, job_id: str, token: str, owner: str, columns: dict) -> dict | None:
        with self.db.writing():
            row = self._update(job_id, ["state = %s", "attempt_token = %s", "lease_owner = %s"],
                               [RUNNING, token, owner], columns)
            if row is None:
                return None
            self.db.execute(
                "DELETE FROM telegrambot_job_slots WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
                " AND job_id = %s AND attempt_token = %s AND owner_id = %s",
                self._scope() + [job_id, token, owner])
            self._release_amendments(job_id, token)
        return row

    def resume(self, job_id: str, *, actor_id: str | None = None) -> dict | None:
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
            "stop_requested": False, "error": None, "exit_code": None, "finished_at": None,
            "execution_finished_at": None, "attempt_token": None, "lease_owner": None,
            "lease_expires_at": None, "resume_at": None, "result_text": None,
            "result_silent": False, "delivery_state": None, "delivery_error": None,
            "delivered_at": None})

    @staticmethod
    def _refuse_while_delivering(row: dict, action: str) -> None:
        if row.get("delivery_state") == "pending":
            raise JobError("result_delivery_pending",
                           f"cannot {action} job {row['id']} until its result is delivered",
                           "wait for the service to deliver it, then try again")

    # -- the runner's claim on an attempt -----------------------------------------

    def claim_next(self, *, owner_id: str, host: str, node: str | None, max_parallel: int,
                   lease_seconds: float = LEASE_SECONDS) -> dict | None:
        max_parallel = max(1, int(max_parallel))
        token = str(uuid.uuid4())
        try:
            with self.db.writing():
                used = {int(r["slot"]) for r in self.db.execute(
                    "SELECT slot FROM telegrambot_job_slots WHERE agent_id = %s"
                    " AND coalesce(connection_id, file_connection) = %s", self._scope()).fetchall()}
                slot = next((n for n in range(max_parallel) if n not in used), None)
                if slot is None:
                    return None
                clause, scope = self._where("state = %s")
                candidate = self.db.execute(
                    "SELECT id FROM telegrambot_jobs" + clause
                    + " ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    scope + [WAITING]).fetchone()
                if candidate is None:
                    return None
                job_id = candidate["id"]
                taken = self.db.execute(
                    "INSERT INTO telegrambot_job_slots (agent_id, connection_id, file_connection,"
                    " slot, job_id, owner_id, attempt_token, lease_expires_at) VALUES (%s,"
                    f" {CONNECTION_VALUES}, %s, %s, %s, %s,"
                    " now() + make_interval(secs => %s)) ON CONFLICT DO NOTHING"
                    " RETURNING slot",
                    self._written() + [slot, job_id, owner_id, token,
                                     float(lease_seconds)]).fetchone()
                if taken is None:
                    raise _ClaimLost()
                clause, scope = self._where("id = %s", "state = %s")
                row = self.db.execute(
                    "UPDATE telegrambot_jobs SET state = %s, outcome = NULL,"
                    " attempt = attempt + 1, attempt_token = %s, lease_owner = %s,"
                    " lease_expires_at = now() + make_interval(secs => %s), host = %s,"
                    " node = %s, pid = NULL, pgid = NULL,"
                    " started_at = COALESCE(started_at, now()), updated_at = now()"
                    + clause + " RETURNING *",
                    [RUNNING, token, owner_id, float(lease_seconds), host, node]
                    + scope + [job_id, WAITING]).fetchone()
                if row is None:
                    raise _ClaimLost()
        except _ClaimLost:
            return None
        return row

    def attach_process(self, job_id: str, token: str, owner: str, *, pid, session_id=None,
                       engine=None, model=None) -> bool:
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
        with self.db.writing():
            row = self._update(job_id, ["state = %s", "attempt_token = %s", "lease_owner = %s"],
                               [RUNNING, token, owner], {"lease_expires_at": until})
            if row is None:
                return False
            self.db.execute(
                "UPDATE telegrambot_job_slots SET lease_expires_at = %s WHERE agent_id = %s"
                " AND coalesce(connection_id, file_connection) = %s AND job_id = %s AND attempt_token = %s"
                " AND owner_id = %s", [until] + self._scope() + [job_id, token, owner])
        return True

    def abandoned(self, *, owner_id: str, host: str) -> list[dict]:
        now = time.time()
        out = []
        for row in self.running():
            if row.get("lease_owner") == owner_id:
                continue
            expired = (_epoch(row.get("lease_expires_at")) or 0) <= now
            dead = row.get("host") == host and not pid_alive(owner_pid(row.get("lease_owner")))
            if expired or dead:
                out.append({**row, "_dead_owner": dead})
        return out

    def fence(self, row: dict, reason: str, *, before_release=None,
              result_text: str | None = None) -> dict | None:
        token, owner = row.get("attempt_token"), row.get("lease_owner")
        if not token or not owner:
            return None
        predicates = ["state = %s", "attempt_token = %s", "lease_owner = %s"]
        params = [RUNNING, token, owner]
        if not row.get("_dead_owner"):
            predicates.append("lease_expires_at <= now()")
        columns = self._stop_columns(INTERRUPTED, error=reason, result_text=result_text)
        with self.db.writing():
            stopped = self._update(row["id"], predicates, params, columns)
            if stopped is None:
                return None
            if before_release is not None:
                before_release()
            self.db.execute(
                "DELETE FROM telegrambot_job_slots WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
                " AND job_id = %s AND attempt_token = %s AND owner_id = %s",
                self._scope() + [row["id"], token, owner])
            self._release_amendments(row["id"], token)
        return stopped

    # -- delivery -----------------------------------------------------------------

    def take_delivery(self, job_id: str) -> dict | None:
        """Mark one pending result delivered, before it is sent: the row this
        delivery owns, or None when another took it."""
        return self._update(job_id, ["delivery_state = 'pending'"], [],
                            {"delivery_state": "delivered",
                             "delivered_at": datetime.datetime.now(datetime.timezone.utc),
                             "delivery_attempts": 0, "delivery_error": None})

    def delivered(self, job_id: str, message_id: int | None) -> None:
        self._update(job_id, [], [], {"delivered_message_id": message_id})

    def delivery_failed(self, job_id: str, error: str, *, retry: bool) -> None:
        """A send Telegram definitely refused goes back to pending (`retry`); one
        whose outcome is unknown stays delivered with the error named."""
        clause, scope = self._where("id = %s")
        state = "'pending'" if retry else "delivery_state"
        self.db.execute(f"UPDATE telegrambot_jobs SET delivery_state = {state},"
                        " delivery_attempts = delivery_attempts + 1, delivery_error = %s,"
                        " updated_at = now()" + clause, [str(error)[:500]] + scope + [job_id])


# ── The runner ──────────────────────────────────────────────────────────────


class LiveJob:
    def __init__(self, row: dict):
        self.row = row
        self.cancel = threading.Event()
        self.reason: str | None = None
        self.thread: threading.Thread | None = None
        self.pid = None
        self.outbox = None

    def end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.cancel.set()


def _failure_kind(result) -> str:
    kind = getattr(getattr(result, "failure", None), "kind", None)
    return str(getattr(kind, "value", kind) or "error")


class JobRunner:
    """One bot's job runner: renews its leases, fences abandoned ones, honours
    stops and amendments, keeps the quota pause, delivers results, and claims
    the oldest waiting job while a slot is free. Each job runs on its own thread
    through the harness runner. `dialogue` answers what a job runs with."""

    def __init__(self, *, dialogue, db: Db, owner_id: str | None = None,
                 host: str | None = None, node: str | None = None, run=None,
                 sessions=None, kill_group=None, poll: float = POLL_INTERVAL,
                 lease: float = LEASE_SECONDS, now=time.time):
        self.dialogue = dialogue
        self.db = db
        self.register = JobRegister(db, dialogue.agent_id, dialogue.connection)
        self.owner_id = owner_id or new_owner_id()
        self.host = host or HOST
        self.node = node
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

    def max_parallel(self) -> int:
        return max(1, int(self.dialogue.policy.default("max_parallel_jobs") or 1))

    def recovery(self) -> str:
        value = self.dialogue.policy.default("job_recovery")
        return value if value in JOB_RECOVERY else "requeue"

    def reachable(self) -> bool:
        try:
            self.db.execute("SELECT 1 AS one FROM telegrambot_jobs LIMIT 1")
            return True
        except Exception:
            return False

    def paused(self) -> bool:
        return bool(self.paused_until and self.now() < self.paused_until)

    def pause_notice(self) -> str | None:
        if not self.paused():
            return None
        resumes = datetime.datetime.fromtimestamp(self.paused_until, datetime.timezone.utc)
        return (f"{self.pause_reason or 'The quota is spent.'} Queued work waits; the queue "
                f"resumes at {resumes:%H:%M} UTC.")

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True,
                                           name=f"jobs-{self.dialogue.connection}")
            self.thread.start()

    def _loop(self) -> None:
        while not self.stopping.is_set():
            self.tick()
            self.stopping.wait(self.poll)

    def shutdown(self, wait: float = 20.0) -> None:
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

    def tick(self) -> None:
        try:
            self.renew()
            self.reconcile()
            self.honour_asks()
            self.keep_pause()
            self.deliver()
            self.relay()
            if not self.stopping.is_set():
                self.claim()
            self.store_error = None
        except Exception as exc:
            line = f"{type(exc).__name__}: {exc}"
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
        fenced = []
        for row in self.register.abandoned(owner_id=self.owner_id, host=self.host):
            local = row.get("host") == self.host
            pgid = row.get("pgid") or row.get("pid")

            def stop_group(pgid=pgid, local=local):
                if local and pgid:
                    self.kill_group(int(pgid))

            requeue = self.recovery() == "requeue" and local and bool(row.get("session_id"))
            stopped = self.register.fence(
                row, "the service running it went away", before_release=stop_group,
                result_text=None if requeue else interrupted_notice(row))
            if stopped is None:
                continue
            fenced.append(stopped)
            self.stats["interrupted"] += 1
            if requeue:
                self.register.resume(row["id"])
                self.log(f"jobs: {row['id']} was interrupted; continuing session "
                         f"{row['session_id']}")
            else:
                self.log(f"jobs: {row['id']} was interrupted; stopped and reported")
        return fenced

    def honour_asks(self) -> None:
        for row in self.register.stop_pending():
            if row["state"] == WAITING:
                if self.register.cancel_waiting(row["id"],
                                                result_text=stopped_notice(row)) is not None:
                    self.stats["cancelled"] += 1
                    self.log(f"jobs: {row['id']} stopped before it started")
            elif row.get("lease_owner") == self.owner_id and row["id"] in self.live:
                self.live[row["id"]].end("stop")
        for row in self.register.amend_pending():
            if row.get("lease_owner") == self.owner_id and row["id"] in self.live:
                self.live[row["id"]].end("amend")

    def enter_pause(self, reason: str) -> datetime.datetime:
        self.paused_until = self.now() + QUOTA_RETRY_SECONDS
        self.pause_reason = str(reason or "The quota is spent.")[:200]
        until = datetime.datetime.fromtimestamp(self.paused_until, datetime.timezone.utc)
        self.log(f"jobs: queue paused until {until:%H:%M:%S}Z: {self.pause_reason}")
        return until

    def keep_pause(self) -> None:
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
            with contextlib.suppress(JobError):
                self.register.resume(row["id"])
                self.log(f"jobs: {row['id']} waiting again after the quota pause")

    def deliver(self) -> None:
        for row in self.register.pending_deliveries():
            taken = self.register.take_delivery(row["id"])
            if taken is None:
                continue
            try:
                sent = self.dialogue.deliver_result(taken)
            except Exception as exc:
                retry = getattr(exc, "code", None) != "delivery_unknown"
                self.register.delivery_failed(row["id"], f"{type(exc).__name__}: {exc}",
                                              retry=retry)
                self.log(f"jobs: {row['id']} result not delivered "
                         f"({'will retry' if retry else 'outcome unknown, not repeated'}): "
                         f"{exc}")
                continue
            self.register.delivered(row["id"], sent[0] if sent else None)
            self.stats["delivered"] += 1
            self.log(f"jobs: {row['id']} result delivered to {row['chat_id']}")

    def relay(self) -> None:
        """Progress lines and messages a running job wrote to its outbox."""
        with self.lock:
            live = list(self.live.values())
        for job in live:
            if job.outbox is not None:
                with contextlib.suppress(Exception):
                    job.outbox.relay()

    def claim(self) -> None:
        while not self.paused() and not self.stopping.is_set():
            with self.lock:
                if len(self.live) >= self.max_parallel():
                    return
            row = self.register.claim_next(owner_id=self.owner_id, host=self.host,
                                           node=self.node, max_parallel=self.max_parallel(),
                                           lease_seconds=self.lease)
            if row is None:
                return
            self.stats["claimed"] += 1
            job = LiveJob(row)
            with self.lock:
                self.live[row["id"]] = job
            job.thread = threading.Thread(target=self.run_job, args=(job,), daemon=True,
                                          name=f"job-{row['id'][:8]}")
            job.thread.start()

    def stop_channel(self, chat_id: int, topic_id: int) -> bool:
        """`/stop`: ask the channel's waiting and running jobs to stop."""
        stopped = False
        for row in self.register.open_jobs(chat_id, topic_id):
            if self.register.request_stop(row["id"]) is None:
                continue
            stopped = True
            if row["id"] in self.live:
                self.live[row["id"]].end("stop")
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
        return self._run if self._run is not None else self.dialogue.profiles.runner().run

    def session_for(self, session_id):
        if self._sessions is not None:
            fresh, resume = self._sessions
            return resume(session_id) if session_id else fresh()
        lib = self.dialogue.profiles.runner()
        return lib.Session.resume(session_id) if session_id else lib.Session.fresh()

    def run_job(self, job: LiveJob) -> None:
        row = job.row
        job_id, token, owner = row["id"], row["attempt_token"], self.owner_id
        reg = self.register
        call = None
        try:
            amendments = reg.claim_amendments(job_id, token, owner)
            call = self.dialogue.job_call(row, [a["text"] for a in amendments])
            job.outbox = call.get("outbox")
            harness = call["origin"].get("harness")
            if row.get("engine") and harness and row["engine"] != harness:
                self._fail(job, MODEL_REFUSED,
                           f"the job's session belongs to {row['engine']} and its profile "
                           f"{call['origin']['name']} now runs {harness}")
                return
            current = reg.get(job_id)
            if current is not None and current.get("stop_requested"):
                job.end("stop")
            if job.cancel.is_set():
                self._land_early(job)
                return
            self.log(f"jobs: {job_id} attempt {row['attempt']} started on "
                     f"{call['origin']['name']} ({harness})"
                     + (f", continuing session {row['session_id']}" if row.get("session_id")
                        else ""))

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
                session=self.session_for(row.get("session_id")), environ=call["environ"],
                extra_env=call["extra_env"], cancel=job.cancel, on_start=on_start,
                on_event=call.get("on_event"))
            if job.outbox is not None:
                with contextlib.suppress(Exception):
                    job.outbox.relay(final=True)
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
                reg.finish(job_id, token, owner, SUCCEEDED, exit_code=0, session_id=session_id,
                           result_text=answer or None, result_silent=not answer)
                self.stats["succeeded"] += 1
                self.log(f"jobs: {job_id} succeeded" + ("" if answer else " silently"))
                return
            kind = _failure_kind(result)
            message = str(getattr(getattr(result, "failure", None), "message", "") or kind)
            if kind == QUOTA:
                until = self.enter_pause(message)
                reg.finish(job_id, token, owner, QUOTA, error=message, session_id=session_id,
                           resume_at=until, result_text=self.pause_notice())
                self.stats["quota"] += 1
                return
            self._fail(job, MODEL_REFUSED if kind == MODEL_REFUSED else FAILED,
                       f"{kind}: {message}", session_id=session_id)
        except Exception as exc:
            self.log(f"jobs: {job_id} error: {type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                self._fail(job, FAILED, f"error: {type(exc).__name__}: {exc}")
        finally:
            if call is not None:
                self.dialogue.release_call(call)
            with self.lock:
                self.live.pop(job_id, None)

    def _fail(self, job: LiveJob, outcome: str, detail: str, *, session_id=None) -> None:
        row = job.row
        text = (f"Job failed: «{row['description']}»\n{detail}"
                if self.dialogue.job_role(row) == "supervisor"
                else f"«{row['description']}» could not be completed. Please tell an "
                     "administrator.")
        self.register.finish(row["id"], row["attempt_token"], self.owner_id, outcome,
                             error=detail, session_id=session_id, result_text=text)
        self.stats[outcome] = self.stats.get(outcome, 0) + 1
        self.log(f"jobs: {row['id']} {outcome}: {detail}"[:600])

    def _land_early(self, job: LiveJob, *, session_id=None) -> None:
        row, reg = job.row, self.register
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
            requeue = self.recovery() == "requeue" and bool(session_id or
                                                            current.get("session_id"))
            stopped = reg.finish(row["id"], row["attempt_token"], self.owner_id, INTERRUPTED,
                                 error="the service stopped", session_id=session_id,
                                 result_text=None if requeue else interrupted_notice(row))
            if stopped is not None:
                self.stats["interrupted"] += 1
                if requeue:
                    reg.resume(row["id"])
                self.log(f"jobs: {row['id']} interrupted by the service stopping; "
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
