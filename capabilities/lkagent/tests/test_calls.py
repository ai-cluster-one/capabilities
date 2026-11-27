"""The call record: a row of lkagent_calls built from callva-livekit's call.ended body, and
the table's migration on a throwaway PostgreSQL.

The store-backed case reads LKAGENT_TEST_DSN, a throwaway database's URL, and skips when it
is unset; it works in a schema of its own, so it never meets another run's rows.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = CAPABILITY / "bin" / "lkagent"
sys.path.insert(0, str(CAPABILITY / "service"))

import calls  # noqa: E402

STARTED, ENDED = 1_790_000_000.0, 1_790_000_042.5


def load_cli():
    spec = importlib.util.spec_from_loader("lkagent_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI)
    source = CLI.read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI), "exec"), module.__dict__)
    return module


class Ctx:
    """A finished job, as far as the body builder reads one."""

    def __init__(self, metadata: str = ""):
        self.job = SimpleNamespace(metadata=metadata, room=SimpleNamespace(sid="RM_x"))
        self.room = SimpleNamespace(name="call-room", metadata="")
        self.tagger = SimpleNamespace(tags={"booked"}, outcome="success", outcome_reason=None)


def ended_body(direction: str = "inbound", **state) -> dict:
    """A call.ended body built by callva-livekit's own builder."""
    pytest.importorskip("livekit.agents")
    from callva.livekit.core import envelope, state as callva_state
    from callva.livekit.webhook import payload

    st = callva_state.CallState(ctx=Ctx(), envelope=envelope.parse(""))
    caller = SimpleNamespace(identity="sip_caller", name="Caller Name", kind=None, metadata="",
                             attributes={"sip.phoneNumber": "+15550123",
                                         "sip.trunkPhoneNumber": "+15550100"})
    callva_state.ensure_identity(st, participant=caller, direction=direction)
    st.started_at, st.ended_at = state.pop("started_at", STARTED), ENDED
    st.ended_by = state.pop("ended_by", "user")
    for key, value in state.items():
        setattr(st, key, value)
    report = {"chat_history": {"items": [
        {"type": "message", "role": "assistant", "content": ["Hello, how can I help?"],
         "created_at": STARTED + 1.0},
        {"type": "function_call", "name": "x", "created_at": STARTED + 1.5},
        {"type": "message", "role": "user", "content": ["Book me ", "for nine."],
         "created_at": STARTED + 3.25},
        {"type": "message", "role": "system", "content": ["not said"],
         "created_at": STARTED + 4.0}]}}
    return payload.build(st, event=payload.ENDED, key="key-1",
                         status=state.get("status", "completed"), participant=caller,
                         session_report=report)


def test_a_row_is_read_from_the_call_ended_body():
    body = ended_body()
    row = calls.row(body, agent_id="prj_0a1b2c3d4e5f", backend_session_id="sess-1",
                    task="Confirm the slot")
    assert row["agent_id"] == "prj_0a1b2c3d4e5f"
    assert row["call_id"] == body["call"]["id"] and row["direction"] == "inbound"
    assert (row["from_number"], row["caller_name"], row["to_number"]) == (
        "+15550123", "Caller Name", "+15550100")
    assert row["started_at"] == "2026-09-21T14:13:20.000Z"
    assert row["ended_at"] == "2026-09-21T14:14:02.500Z"
    assert row["duration_seconds"] == 42.5
    assert row["status"] == "completed" and row["ended_by"] == "user"
    assert row["failure_reason"] is None
    assert row["transcript"] == [
        {"role": "assistant", "text": "Hello, how can I help?", "at": "2026-09-21T14:13:21.000Z"},
        {"role": "user", "text": "Book me for nine.", "at": "2026-09-21T14:13:23.250Z"}]
    assert row["outcome"] == "success" and row["tags"] == ["booked"]
    assert row["backend_session_id"] == "sess-1" and row["task"] == "Confirm the slot"
    assert row["scheduled_at"] is None and row["recording_url"] is None
    assert row["audio_key"] is None and row["session_report_key"] is None
    assert row["body"] is body
    assert set(row) == set(calls.COLUMNS)


def test_a_recorded_call_keeps_where_its_files_are():
    body = ended_body()
    body["recording"] = {"delivery": "storage", "audio_key": "calls/lkagent/c-1.ogg",
                         "session_report_key": "calls/lkagent/c-1.session.json"}
    row = calls.row(body, agent_id="a")
    assert (row["audio_key"], row["session_report_key"]) == (
        "calls/lkagent/c-1.ogg", "calls/lkagent/c-1.session.json")
    body["recording"]["audio_key"] = None   # a session that made no audio
    assert calls.recording_keys(body) == (None, "calls/lkagent/c-1.session.json")


def test_a_call_nobody_took_is_no_answer_and_says_why():
    body = ended_body(direction="outbound", started_at=0.0, ended_by="no_answer",
                      unanswered_reason="the callee did not answer", status="rejected")
    row = calls.row(body, agent_id="a")
    assert row["status"] == "no_answer" and row["ended_by"] == "no_answer"
    assert row["duration_seconds"] is None and row["started_at"] == calls.iso(
        body["call"]["started_at"])
    assert row["failure_reason"] == "the callee did not answer"
    assert calls.status({"status": "failed"}) == "failed"
    assert calls.status({"status": "canceled"}) == "no_answer"
    assert calls.status({"status": "something new"}) == "failed"
    assert calls.row({"call": {"id": "c", "ended_by": "transfer"}}, agent_id="a")["ended_by"] is None


def test_without_a_store_no_row_is_written_and_it_is_said_once(tmp_path, monkeypatch, caplog):
    for key in list(os.environ):
        if key.startswith("AGENTKIT_DB_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    body = {"call": {"id": "c1", "direction": "inbound", "status": "completed"}}
    caplog.set_level(logging.WARNING, logger="lkagent")
    recorder = calls.Recorder(str(tmp_path), "prj_0a1b2c3d4e5f")
    assert recorder.record(body) is None and recorder.record(body) is None
    said = [r for r in caplog.records if "calls are not recorded" in r.getMessage()]
    assert len(said) == 1 and "store_not_configured" in said[0].getMessage()
    nobody = calls.Recorder(str(tmp_path), None)
    assert nobody.record(body) is None and nobody.record(body) is None
    assert len([r for r in caplog.records if "agent id" in r.getMessage()]) == 1


def test_the_table_migrates_and_takes_a_row_once(tmp_path, monkeypatch):
    dsn = os.environ.get("LKAGENT_TEST_DSN")
    if not dsn:
        pytest.skip("LKAGENT_TEST_DSN names no throwaway database")
    psycopg = pytest.importorskip("psycopg")
    from capabilities_contract import db

    schema = "lk_" + uuid.uuid4().hex[:10]
    for key in list(os.environ):
        if key.startswith("AGENTKIT_DB_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AGENTKIT_DB_URL", dsn)
    monkeypatch.setenv("AGENTKIT_DB_SCHEMA", schema)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cli = load_cli()
    steps, major, minor = cli._tables()
    conn = db.connect(application_name="lkagent-test", project_root=tmp_path, transaction=True)
    try:
        done = db.migrate(conn, "lkagent", steps, major=major, minor=minor)
        assert done.applied == ["0001-calls", "0002-call-recording-keys"]
        assert (done.major, done.minor) == (1, 1)
        again = db.migrate(conn, "lkagent", steps, major=major, minor=minor)
        assert again.applied == []
    finally:
        conn.close()
    agent = "prj_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f"INSERT INTO {schema}.agents (id, kind) VALUES (%s, 'agent')", (agent,))
    body = ended_body()
    body["recording"] = {"delivery": "storage", "audio_key": "calls/lkagent/c-1.ogg",
                         "session_report_key": "calls/lkagent/c-1.session.json"}
    recorder = calls.Recorder(str(tmp_path), agent)
    assert recorder.record(body, backend_session_id="sess-1", task=None)["call_id"]
    assert recorder.record(body) is not None   # the same call again changes nothing
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f"SET search_path TO {schema}")
        rows = admin.execute(
            "SELECT agent_id, call_id, direction, status, ended_by, duration_seconds, "
            "started_at, transcript, body->'call'->>'id', backend_session_id, audio_key, "
            "session_report_key FROM lkagent_calls").fetchall()
        assert len(rows) == 1
        got = rows[0]
        assert got[0] == agent and got[1] == body["call"]["id"] and got[8] == got[1]
        assert (got[2], got[3], got[4], float(got[5])) == ("inbound", "completed", "user", 42.5)
        assert got[6].astimezone(datetime.timezone.utc).isoformat() == "2026-09-21T14:13:20+00:00"
        assert [t["role"] for t in got[7]] == ["assistant", "user"] and got[9] == "sess-1"
        assert got[10:] == ("calls/lkagent/c-1.ogg", "calls/lkagent/c-1.session.json")
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute("INSERT INTO lkagent_calls (agent_id, call_id, direction, status, body)"
                          " VALUES (%s, 'c2', 'inbound', 'busy', '{}')", (agent,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute("INSERT INTO lkagent_calls (agent_id, call_id, direction, status, body)"
                          " VALUES ('prj_unregistered', 'c3', 'inbound', 'failed', '{}')")
        comment = admin.execute(
            "SELECT obj_description(%s::regclass, 'pg_class')",
            (f"{schema}.lkagent_calls",)).fetchone()[0]
        assert comment and "One row per call" in comment
        first = admin.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
            "AND NOT attisdropped ORDER BY attnum LIMIT 2", (f"{schema}.lkagent_calls",)
        ).fetchall()
        assert [c[0] for c in first] == ["id", "agent_id"]
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
    json.dumps(body, default=str)
