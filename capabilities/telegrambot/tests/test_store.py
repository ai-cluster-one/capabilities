"""The telegrambot tables on a real PostgreSQL: a message reserved once before
anything is done with it, the watermark and the bot-turn count moving in one
commit, `/set` overrides, the seen list, and prompt versions.

Needs TELEGRAMBOT_TEST_DSN, a libpq URL for a throwaway database; each case
binds a schema of its own. Skipped without it."""

from __future__ import annotations

import os
import uuid

import pytest

from _agents import register_agents
from _cli import load_service

DSN = os.environ.get("TELEGRAMBOT_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TELEGRAMBOT_TEST_DSN is not set")

s = load_service("store")
A, C = "prj_test", "helper"


@pytest.fixture()
def store():
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = f"tgb_test_{uuid.uuid4().hex[:8]}"
    setting = db.Setting(schema=schema, level="machine", sources=("TELEGRAMBOT_TEST_DSN",),
                         host=info.get("host") or "127.0.0.1", port=int(info.get("port") or 5432),
                         database=info.get("dbname"), user=info.get("user"),
                         password=info.get("password"), sslmode=info.get("sslmode") or "disable")
    opened = s.Store(setting).open()
    register_agents(opened, A)
    yield opened
    opened.execute(f"DROP SCHEMA {schema} CASCADE")
    opened.close()


def test_a_message_is_reserved_once_and_finished_with_the_watermark(store):
    assert store.reserve(A, C, 42, 0, 7, "h1") is True
    assert store.reserve(A, C, 42, 0, 7, "h2") is False       # taken
    assert store.reserve(A, C, 42, 0, 8, "h1") is True
    assert store.channel(A, C, 42)["reserved"] == [7, 8]
    store.finish(A, C, 42, 0, [7, 8], bot_turn=True)
    row = store.channel(A, C, 42)
    assert row["reserved"] == [] and row["last_message_id"] == 8 and row["bot_turns"] == 1
    assert store.reserve(A, C, 42, 0, 8, "h1") is False       # answered
    assert store.reserve(A, C, 42, 0, 6, "h1") is False       # older than the watermark
    store.reserve(A, C, 42, 0, 9, "h1")
    store.finish(A, C, 42, 0, [9], bot_turn=False)
    assert store.channel(A, C, 42)["bot_turns"] == 0
    # A topic is a channel of its own.
    assert store.reserve(A, C, 42, 5, 7, "h1") is True
    assert store.stale_reservations(A, C) == [{"chat_id": 42, "topic_id": 5, "reserved": [7]}]
    store.release(A, C, 42, 5, [7])
    assert store.stale_reservations(A, C) == []


def test_overrides_are_set_and_cleared_one_key_at_a_time(store):
    assert store.set_override(A, C, -100, 0, "tail", 20) == {"tail": 20}
    assert store.set_override(A, C, -100, 0, "voice_echo", False) == {"tail": 20,
                                                                      "voice_echo": False}
    assert store.set_override(A, C, -100, 0, "tail", None) == {"voice_echo": False}


def test_the_seen_list_writes_only_what_moved(store):
    assert store.see(A, C, "chat", -100, title="G", chat_type="supergroup") is True
    assert store.see(A, C, "chat", -100, title="G", chat_type="supergroup") is False
    assert store.see(A, C, "chat", -100, title="Renamed") is True
    assert store.see(A, C, "person", 42, title="Ann", username="ann") is True
    rows = store.seen(A, C)
    assert [(r["kind"], r["peer_id"], r["title"]) for r in rows] == \
        [("chat", -100, "Renamed"), ("person", 42, "Ann")]
    assert rows[0]["chat_type"] == "supergroup"
    assert [r["kind"] for r in store.seen(A, C, "person")] == ["person"]


def test_prompts_keep_every_version_and_check_the_base(store):
    first = store.save_prompt(A, "bot", "one", "me@node")
    assert first["replaced"] is None
    with pytest.raises(s.PromptConflict):
        store.save_prompt(A, "bot", "two", "me@node", base="ffffff")
    second = store.save_prompt(A, "bot", "two", "me@node", base=first["id"][:8])
    assert second["replaced"] == first["id"]
    assert store.prompt(A, "bot")["body"] == "two"
    assert [v["active"] for v in store.prompt_history(A, "bot")] == [True, False]
    with pytest.raises(Exception):
        store.execute("UPDATE telegrambot_prompts SET body = 'x' WHERE id = %s",
                      (first["id"],))
    assert store.retire_prompt(A, "bot") == second["id"]
    assert store.prompt(A, "bot") is None and len(store.prompt_history(A, "bot")) == 2
    with pytest.raises(Exception):
        store.save_prompt(A, "Bad Key", "x", "me@node")



def _setting(schema: str):
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    return db.Setting(schema=schema, level="machine", sources=("TELEGRAMBOT_TEST_DSN",),
                      host=info.get("host") or "127.0.0.1", port=int(info.get("port") or 5432),
                      database=info.get("dbname"), user=info.get("user"),
                      password=info.get("password"), sslmode=info.get("sslmode") or "disable")


def test_a_store_at_major_1_takes_connection_ids_in_place_and_keeps_its_rows(monkeypatch):
    """A store whose core is at 2.0 and whose telegrambot tables are at 1.0,
    every row naming its bot by the connection's name, takes the ids in place
    under the store upgrade: a row of a connection the store holds reads under
    its id and goes with it, and a row of a connection only files hold keeps
    its name and keeps working. Only the store upgrade crosses."""
    import psycopg
    import capabilities_contract.db._migrate as core_migrate
    from capabilities_contract import db

    schema = f"tgb_m1_{uuid.uuid4().hex[:8]}"
    setting = _setting(schema)
    old = [step for step in s.steps()
           if (step.id if hasattr(step, "id") else step[0])
           not in ("0007-connection-ids", "0008-keys")]
    conn = db.connect(application_name="telegrambot", setting=setting)
    try:
        # The store as the release before the cutover left it: the core at 2.0.
        with monkeypatch.context() as m:
            m.setattr(core_migrate, "CORE_STEPS", core_migrate.CORE_STEPS[:1])
            m.setattr(core_migrate, "CORE_VERSION", (2, 0))
            db.migrate(conn, s.OWNER, old, major=1, minor=0)
        register_agents(conn, A)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO connections (capability, name, spec) VALUES "
                        "('telegrambot', 'helper', '{}'::jsonb)")
            for bot in ("helper", "filed"):
                cur.execute("INSERT INTO telegrambot_channels (agent_id, connection, chat_id, "
                            "last_message_id) VALUES (%s, %s, 42, 9)", (A, bot))
                cur.execute("INSERT INTO telegrambot_seen (agent_id, connection, kind, "
                            "peer_id, title) VALUES (%s, %s, 'chat', 42, 'G')", (A, bot))
                cur.execute("INSERT INTO telegrambot_jobs (id, agent_id, connection, chat_id, "
                            "requested_by, description, state, created_at, updated_at) VALUES "
                            "(%s, %s, %s, 42, '42', 'from before', 'running', now(), now())",
                            (f"job-{bot}", A, bot))
                cur.execute("INSERT INTO telegrambot_job_amendments (id, agent_id, connection, "
                            "job_id, text, state, created_at) VALUES (%s, %s, %s, %s, 'more', "
                            "'pending', now())", (f"a-{bot}", A, bot, f"job-{bot}"))
                cur.execute("INSERT INTO telegrambot_job_slots (agent_id, connection, slot, "
                            "job_id, owner_id, attempt_token, lease_expires_at) VALUES "
                            "(%s, %s, 0, %s, 'o', 't', now())", (A, bot, f"job-{bot}"))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.delenv("CAPABILITIES_STORE_UPGRADE", raising=False)
    with pytest.raises(s.StoreUnavailable) as refused:
        s.Store(setting).open()
    assert refused.value.slug == "store_upgrade_needed"
    monkeypatch.setenv("CAPABILITIES_STORE_UPGRADE", "1")
    store = s.Store(setting).open()
    try:
        assert store.execute("SELECT major, minor FROM schema_version WHERE owner = %s",
                             (s.OWNER,), fetch="one") == {"major": 2, "minor": 1}
        helper = store.execute("SELECT id FROM core_premig_connection_names "
                               "WHERE name = 'helper'", fetch="one")["id"]
        jobs = load_service("jobs")
        tables = ("channels", "seen", "jobs", "job_amendments", "job_slots")
        for bot, held, filed in ((helper, helper, None), ("filed", None, "filed")):
            assert store.channel(A, bot, 42)["last_message_id"] == 9
            assert [r["title"] for r in store.seen(A, bot)] == ["G"]
            reg = jobs.JobRegister(jobs.Db(store), A, bot)
            job = f"job-{'helper' if bot == helper else 'filed'}"
            assert reg.get(job)["description"] == "from before"
            assert reg.pending_amendment(job) == ["more"]
            for table in tables:
                assert store.execute(
                    f"SELECT connection_id, file_connection FROM telegrambot_{table} "
                    "WHERE coalesce(connection_id, file_connection) = %s", (bot,),
                    fetch="all") == [{"connection_id": held, "file_connection": filed}], table
        assert store.execute(
            "SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = "
            "current_schema() AND table_name LIKE 'telegrambot\\_%%' "
            "AND column_name = 'connection'", fetch="one") == {"n": 0}
        # A new row of either kind lands in its own column.
        assert store.reserve(A, helper, 43, 0, 1, "h") and store.reserve(A, "filed", 43, 0, 1, "h")
        with store.transaction() as cur:
            db.deleting(cur, "connections", helper)
            cur.execute("DELETE FROM connections WHERE id = %s", (helper,))
        for table in tables:
            assert store.execute(
                f"SELECT DISTINCT file_connection FROM telegrambot_{table}",
                fetch="all") == [{"file_connection": "filed"}], table
        raw = psycopg.connect(DSN, autocommit=True)
        try:
            with pytest.raises(db.DbError) as newer:
                db.migrate(raw, s.OWNER, old, major=1, minor=0, schema=schema)
            assert newer.value.slug == "schema_too_new"
        finally:
            raw.close()
    finally:
        store.execute(f"DROP SCHEMA {schema} CASCADE")
        store.close()
