"""The line's call record: one row of `lkagent_calls` per call, written when it ends.

The row is read from the `call.ended` body callva-livekit builds for the webhook - the same
body, kept whole in `body` - with what only this line knows beside it: the agent, the
backend session the call ran, and the task an outbound call was placed with. Where the
project keeps no store the call is not recorded, said once on the log, and the call itself
is never failed over its record.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from typing import Any

logger = logging.getLogger("lkagent")

TABLE = "lkagent_calls"

STATUSES = ("completed", "no_answer", "failed")
ENDED_BY = ("agent", "user", "silence", "duration", "no_answer")
NOT_ANSWERED = frozenset({"no_answer", "rejected", "canceled"})
"""callva-livekit's outcomes of a call nobody took, all recorded as no_answer."""

COLUMNS = ("agent_id", "call_id", "direction", "from_number", "caller_name", "to_number",
           "started_at", "ended_at", "duration_seconds", "status", "ended_by",
           "failure_reason", "transcript", "outcome", "tags", "backend_session_id", "task",
           "scheduled_at", "recording_url", "audio_key", "session_report_key", "body")
JSON_COLUMNS = frozenset({"transcript", "body"})


def iso(seconds: Any) -> str | None:
    """Epoch seconds as ISO 8601 UTC with Z, or None."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    moment = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def status(call: dict) -> str:
    """The row's status: completed, no_answer for a call nobody took, failed otherwise."""
    said = call.get("status")
    if said in ("completed", "failed"):
        return said
    if said in NOT_ANSWERED:
        return "no_answer"
    return "failed"


def transcript(body: dict) -> list:
    """Both sides of the call in the order they spoke, each with when, from the session
    report's chat history: [{role, text, at}], role `user` (the caller) or `assistant`."""
    report = ((body.get("livekit") or {}).get("session_report")) or {}
    items = ((report.get("chat_history") or {}).get("items")) or []
    said = []
    for item in items:
        if not isinstance(item, dict) or item.get("type", "message") != "message":
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        text = " ".join(part for part in (item.get("content") or []) if isinstance(part, str))
        text = " ".join(text.split())
        if text:
            said.append({"role": role, "text": text, "at": iso(item.get("created_at"))})
    return said


def recording_keys(body: dict) -> tuple[str | None, str | None]:
    """Where callva-livekit put the call's audio and session report: the keys its
    `call.ended` body names under `recording`, or (None, None) for a call not recorded."""
    recording = body.get("recording")
    if not isinstance(recording, dict) or recording.get("delivery") != "storage":
        return None, None
    keys = (recording.get("audio_key"), recording.get("session_report_key"))
    return tuple(k if isinstance(k, str) and k else None for k in keys)


def row(body: dict, *, agent_id: str, backend_session_id: str | None = None,
        task: str | None = None) -> dict:
    """The `lkagent_calls` row for a finished call, from its `call.ended` body."""
    call = body.get("call") or {}
    caller = call.get("from") or {}
    called = call.get("to") or {}
    tagged = body.get("tags") or {}
    ended_by = call.get("ended_by")
    tags = [t for t in (tagged.get("tags") or []) if isinstance(t, str)]
    audio_key, session_report_key = recording_keys(body)
    return {
        "agent_id": agent_id,
        "call_id": call.get("id") or body.get("id"),
        "direction": call.get("direction"),
        "from_number": caller.get("number"),
        "caller_name": caller.get("name"),
        "to_number": called.get("number"),
        "started_at": iso(call.get("started_at")),
        "ended_at": iso(call.get("ended_at")),
        "duration_seconds": call.get("duration"),
        "status": status(call),
        "ended_by": ended_by if ended_by in ENDED_BY else None,
        "failure_reason": call.get("reason"),
        "transcript": transcript(body),
        "outcome": tagged.get("outcome") or None,
        "tags": tags or None,
        "backend_session_id": backend_session_id,
        "task": task,
        "scheduled_at": None,
        "recording_url": None,
        "audio_key": audio_key,
        "session_report_key": session_report_key,
        "body": body,
    }


INSERT = (f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ("
          + ", ".join("%s::jsonb" if c in JSON_COLUMNS else "%s" for c in COLUMNS)
          + ") ON CONFLICT (agent_id, call_id) DO NOTHING")


class Recorder:
    """Writes each finished call's row into the project's store, through the shared
    database library, on the setting the project resolves to. A project with no store, or
    no agent id the store could key the row to, records nothing and says so once."""

    def __init__(self, agent_root: str, agent_id: str | None) -> None:
        self.agent_root, self.agent_id = agent_root, agent_id
        self.told = False
        self.lock = threading.Lock()

    def unrecorded(self, why: str) -> None:
        if not self.told:
            self.told = True
            logger.warning("calls are not recorded: %s", why)

    def record(self, body: dict, *, backend_session_id: str | None = None,
               task: str | None = None) -> dict | None:
        """Write the row for `body`; the row written, or None. Never raises."""
        if not self.agent_id:
            self.unrecorded("the project has no agent id a store row could name "
                            "(`capabilities init` binds one)")
            return None
        try:
            from capabilities_contract import db
        except ImportError:
            self.unrecorded("the shared database library is not installed")
            return None
        try:
            setting = db.resolve_setting(self.agent_root)
        except db.DbError as exc:
            self.unrecorded(f"{exc.slug}: {exc.message}")
            return None
        made = row(body, agent_id=self.agent_id, backend_session_id=backend_session_id,
                   task=task)
        values = [json.dumps(made[c], ensure_ascii=False, default=str)
                  if c in JSON_COLUMNS else made[c] for c in COLUMNS]
        with self.lock:
            conn = None
            try:
                conn = db.connect(application_name="lkagent", setting=setting,
                                  transaction=True)
                with conn.cursor() as cur:
                    cur.execute(INSERT, values)
                conn.commit()
            except Exception as exc:
                logger.error("the call %s was not recorded: %s", made["call_id"], exc)
                return None
            finally:
                if conn is not None:
                    conn.close()
        logger.info("call %s recorded: %s, %s", made["call_id"], made["direction"],
                    made["status"])
        return made
