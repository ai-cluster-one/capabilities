"""The job register on a real PostgreSQL: a draft until submitted, one job per
origin message, one owner per attempt, a stop and an amendment as asks, and a
result delivered once. Needs TELEGRAMBOT_TEST_DSN; skipped without it."""

from __future__ import annotations

import os
import uuid

import pytest

from _agents import register_agents, register_nodes
from _cli import load_service

DSN = os.environ.get("TELEGRAMBOT_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TELEGRAMBOT_TEST_DSN is not set")

s = load_service("store")
jobs = load_service("jobs")


@pytest.fixture()
def register():
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(DSN)
    schema = f"tgb_jobs_{uuid.uuid4().hex[:8]}"
    setting = db.Setting(schema=schema, level="machine", sources=("t",),
                         host=info.get("host"), port=int(info.get("port") or 5432),
                         database=info.get("dbname"), user=info.get("user"),
                         password=info.get("password"), sslmode="disable")
    store = s.Store(setting).open()
    register_agents(store, "prj_a")
    register_nodes(store, "n")
    yield jobs.JobRegister(jobs.Db(store), "prj_a", "helper"), store
    store.execute(f"DROP SCHEMA {schema} CASCADE")
    store.close()


def test_a_draft_is_one_per_message_and_runs_once_submitted(register):
    reg, _store = register
    first = reg.register(chat_id=42, requested_by="42", description="make the report",
                         origin_message_id=7)
    again = reg.register(chat_id=42, requested_by="42", description="other words",
                         origin_message_id=7)
    assert again["id"] == first["id"] and first["state"] == "draft"
    assert reg.claim_next(owner_id="o1", host="h", node="n", max_parallel=1) is None
    reg.submit(first["id"])
    claimed = reg.claim_next(owner_id="o1", host="h", node="n", max_parallel=1)
    assert claimed["id"] == first["id"] and claimed["state"] == "running"
    other = reg.register(chat_id=42, requested_by="42", description="second", origin_message_id=8)
    reg.submit(other["id"])
    assert reg.claim_next(owner_id="o2", host="h", node="n", max_parallel=1) is None  # one slot
    done = reg.finish(first["id"], claimed["attempt_token"], "o1", jobs.SUCCEEDED,
                      result_text="the report")
    assert done["delivery_state"] == "pending"
    assert reg.finish(first["id"], claimed["attempt_token"], "o1", jobs.FAILED) is None
    taken = reg.take_delivery(first["id"])
    assert taken["result_text"] == "the report" and reg.take_delivery(first["id"]) is None
    assert reg.submitted_from(42, 0, 7)["id"] == first["id"]


def test_stop_amend_and_resume_are_asks(register):
    reg, _store = register
    row = reg.register(chat_id=-100, topic_id=5, requested_by="42", description="work")
    reg.submit(row["id"])
    claimed = reg.claim_next(owner_id="o1", host="h", node="n", max_parallel=2)
    assert reg.request_stop(row["id"])["stop_requested"] is True
    assert [r["id"] for r in reg.stop_pending()] == [row["id"]]
    reg.attach_process(row["id"], claimed["attempt_token"], "o1", pid=None, session_id="s1")
    reg.stage_amendment(row["id"], "also the totals")
    assert reg.pending_amendment(row["id"]) == ["also the totals"]
    reg.finish(row["id"], claimed["attempt_token"], "o1", jobs.CANCELLED,
               result_text=jobs.stopped_notice(row))
    with pytest.raises(jobs.JobError):
        reg.resume(row["id"])                       # its notice is not delivered yet
    reg.take_delivery(row["id"])
    resumed = reg.resume(row["id"])
    assert resumed["state"] == "waiting" and resumed["session_id"] == "s1"
    assert reg.open_jobs(-100, 5)[0]["id"] == row["id"] and reg.open_jobs(-100, 6) == []
    assert reg.find(row["id"][:8])["id"] == row["id"]
    # Another bot's register sees none of it.
    other = jobs.JobRegister(reg.db, "prj_a", "other")
    assert other.get(row["id"]) is None


def test_no_job_is_entered_for_an_archived_agent(register):
    """Step 0009 refuses entering a job of an archived agent, SQLSTATE 23R01,
    which `archived` reads as nothing to start rather than a failure; a job
    already entered still moves, and a restore lets it in again."""
    import psycopg
    reg, store = register
    first = reg.register(chat_id=42, requested_by="42", description="make the report",
                         origin_message_id=7)

    def mark(on: bool) -> None:
        store.execute("UPDATE agents SET archived_at = CASE WHEN %s THEN now() END, "
                      "archived_by = CASE WHEN %s THEN 'ops@n1' END WHERE id = 'prj_a'",
                      (on, on))

    mark(True)
    with pytest.raises(psycopg.IntegrityError) as refused:
        reg.register(chat_id=42, requested_by="42", description="second", origin_message_id=8)
    assert refused.value.sqlstate == "23R01" and s.archived(refused.value)
    assert not s.archived(RuntimeError("anything else"))
    assert reg.submit(first["id"])["state"] == "waiting"
    mark(False)
    assert reg.register(chat_id=42, requested_by="42", description="second",
                        origin_message_id=8)["state"] == "draft"
