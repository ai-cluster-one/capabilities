"""Which connection each link holds, and a scheduler that starts while its store is away.

No link listens: every one is a transaction connection, and the scheduler's follows
changed definitions by polling the store's change versions. A saved definition still
notifies, for a scheduler of an earlier release that listens. A scheduler whose store does not
answer when it starts waits for it on a doubling interval instead of exiting. The
store-backed checks read AUTOMATIONS_TEST_DSN and skip when it is unset."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "service" / "runtime.py"
DSN = os.environ.get("AUTOMATIONS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="AUTOMATIONS_TEST_DSN is unset")


@pytest.fixture(scope="module")
def runtime():
    spec = importlib.util.spec_from_file_location("automations_runtime_links", RUNTIME_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_scheduler_started_while_the_store_is_away_waits_for_it(runtime, monkeypatch):
    slept, attempts = [], []

    def make():
        attempts.append(True)
        if len(attempts) < 5:
            raise runtime.StoreUnavailable("store_unreachable", "cannot reach the store", None)
        return "the scheduler"

    monkeypatch.setattr(runtime.time, "sleep", slept.append)
    assert runtime._daemon_when_the_store_answers(make) == "the scheduler"
    assert slept == [1.0, 2.0, 4.0, 8.0]


def test_the_wait_for_the_store_stops_growing(runtime, monkeypatch):
    slept = []

    def make():
        if len(slept) < 8:
            raise runtime.StoreUnavailable("store_unreachable", "cannot reach the store", None)
        return "the scheduler"

    monkeypatch.setattr(runtime.time, "sleep", slept.append)
    runtime._daemon_when_the_store_answers(make)
    assert max(slept) == runtime.RECONNECT_LONGEST_SECONDS == 30.0


def test_a_store_setting_that_cannot_be_used_still_ends_the_scheduler(runtime, monkeypatch):
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: pytest.fail("it waited"))

    def make():
        raise runtime.StoreUnavailable("store_not_configured", "no database is configured", None)

    with pytest.raises(runtime.StoreUnavailable):
        runtime._daemon_when_the_store_answers(make)


def _setting(schema: str):
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(DSN)
    return db.Setting(schema=schema, level="machine", sources=("AUTOMATIONS_TEST_DSN",),
                      host=info.get("host") or "127.0.0.1", port=int(info.get("port") or 5432),
                      database=info.get("dbname"), user=info.get("user"),
                      password=info.get("password"), sslmode=info.get("sslmode") or "disable")


@needs_store
def test_no_link_listens_and_the_schedulers_follows_saved_definitions(runtime):
    import psycopg
    schema = "automations_links_" + uuid.uuid4().hex[:8]
    command = runtime.StoreLink(_setting(schema)).open()
    scheduler = runtime.StoreLink(_setting(schema), follow=True).open()
    try:
        for link in (command, scheduler):
            assert link.conn.execute("SELECT pg_listening_channels()").fetchall() == []
            # A link prepares nothing and binds its schema per transaction.
            for _ in range(8):
                link.conn.execute("SELECT count(*) FROM automations_runs").fetchone()
            assert link.conn.execute(
                "SELECT count(*) FROM pg_prepared_statements").fetchone() == (0,)
            assert link.conn.execute("SHOW search_path").fetchone() == (schema,)
            with pytest.raises(Exception) as refused:
                link.conn.execute("LISTEN automations_changed")
            assert getattr(refused.value, "slug", None) == "session_state"

        # What a scheduler of an earlier release does: listen on a session.
        with psycopg.connect(DSN, autocommit=True) as earlier:
            earlier.execute("LISTEN automations_changed")
            with psycopg.connect(DSN) as writer:
                writer.execute(f'SET search_path TO "{schema}"')
                writer.execute("INSERT INTO automations_definition_versions (agent_id, slug, "
                               "script_path) VALUES ('prj_a', 'kept', 'job.py')")
                writer.commit()
                writer.execute("INSERT INTO automations_definition_versions (agent_id, slug, "
                               "script_path) VALUES ('prj_b', 'undone', 'job.py')")
                writer.rollback()
            said = [json.loads(n.payload) for n in earlier.notifies(timeout=1, stop_after=1)]
        assert said == [{"schema": schema, "agent_id": "prj_a", "slug": "kept"}]

        scheduler._polled_at = None
        assert scheduler.take_changed("prj_a") is True
        assert scheduler.take_changed("prj_a") is False
        assert scheduler.take_changed("prj_b") is False
    finally:
        command.close()
        scheduler.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@needs_store
def test_a_scheduler_that_follows_holds_one_connection_to_its_store(runtime):
    import psycopg
    from capabilities_contract import db
    schema = "automations_links_" + uuid.uuid4().hex[:8]
    db.disconnect()
    scheduler = runtime.StoreLink(_setting(schema), follow=True).open()
    try:
        for _ in range(3):
            scheduler._polled_at = None
            assert scheduler.take_changed("prj_a") is False
        with scheduler.conn.transaction():
            assert scheduler.behind("prj_a") is False
        with psycopg.connect(DSN, autocommit=True) as admin:
            held = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND backend_type = 'client backend' AND pid <> pg_backend_pid()").fetchone()
        assert held == (1,)
    finally:
        scheduler.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@needs_store
def test_a_saved_definition_cuts_the_schedulers_sleep_short(runtime):
    """Where the store names its two addresses apart, the scheduler's sleep
    between ticks ends as soon as the store notifies a saved definition, and
    the link polls at its next ask; the listener goes with the link."""
    import dataclasses
    import threading
    import time
    import psycopg
    schema = "automations_links_" + uuid.uuid4().hex[:8]
    setting = _setting(schema)
    apart = dataclasses.replace(setting, transaction_port=setting.port)
    scheduler = runtime.StoreLink(apart, follow=True).open()
    try:
        assert runtime.wait_for_changes([scheduler], 0.2) is False  # opens the listener
        scheduler._polled_at = time.monotonic()

        def save() -> None:
            time.sleep(0.5)
            with psycopg.connect(DSN, autocommit=True) as writer:
                writer.execute(f'SET search_path TO "{schema}"')
                writer.execute("INSERT INTO automations_definition_versions (agent_id, slug, "
                               "script_path) VALUES ('prj_a', 'kept', 'job.py')")

        writer = threading.Thread(target=save)
        began = time.monotonic()
        writer.start()
        assert runtime.wait_for_changes([scheduler], 20.0) is True
        assert time.monotonic() - began < 10.0
        writer.join()
        assert scheduler._polled_at is None
        assert scheduler.take_changed("prj_a") is True
    finally:
        scheduler.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            deadline = time.monotonic() + 5.0
            while True:
                listening = admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = %s AND query ILIKE 'LISTEN%%'",
                    (runtime.LEDGER_OWNER,)).fetchone()
                if listening == (0,) or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    assert listening == (0,)


@needs_store
def test_a_store_at_major_1_keeps_every_row_under_the_agent_names(runtime):
    """A store whose automations tables are at 1.4, with runs and definition
    versions in them, is migrated in place when a link opens: every row stays
    under `agent_id` and `automations_definition_versions`, nothing in the
    tables is still named for the old table or for project_id, and the
    recreated triggers still refuse an edit and announce a save by agent_id."""
    import psycopg
    from capabilities_contract import db
    schema = "automations_m1_" + uuid.uuid4().hex[:8]
    setting = _setting(schema)
    conn = db.connect(application_name="automations-test", setting=setting, transaction=True)
    try:
        db.migrate(conn, runtime.LEDGER_OWNER, runtime.LEDGER_STEPS[:7], major=1, minor=4)
        with conn.transaction():
            conn.execute("INSERT INTO automations_definitions (id, project_id, slug, script_path, "
                         "active, created_by) VALUES "
                         "('00000000-0000-0000-0000-00000000000a', 'prj_a', 'job', 'a.py', "
                         "false, 'u@n'), "
                         "('00000000-0000-0000-0000-00000000000b', 'prj_a', 'job', 'b.py', "
                         "true, 'u@n')")
            conn.execute("INSERT INTO automations_runs (id, project_id, automation_slug, "
                         "environment, trigger, dedupe_key, status, queued_at, log_path, host, "
                         "node, definition_id) VALUES ('r1', 'prj_a', 'job', 'n1', 'manual', "
                         "'d1', 'succeeded', now(), '/tmp/r1.log', 'h', 'n1', "
                         "'00000000-0000-0000-0000-00000000000b')")
    finally:
        conn.close()
    link = runtime.StoreLink(setting).open()
    try:
        ledger = runtime.RunLedger("prj_a", link=link)
        assert [(row["id"], row["project_id"], row["definition_id"])
                for row in ledger._rows()] == [
            ("r1", "prj_a", "00000000-0000-0000-0000-00000000000b")]
        assert [(row["script_path"], row["active"], row["project_id"])
                for row in ledger.definition_history("job")] == [
            ("a.py", False, "prj_a"), ("b.py", True, "prj_a")]
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'SET search_path TO "{schema}"')
            assert admin.execute("SELECT major, minor FROM schema_version "
                                 "WHERE owner = 'automations'").fetchone() == (2, 0)
            assert admin.execute("SELECT to_regclass('automations_definitions')").fetchone() == (
                None,)
            names = [r[0] for r in admin.execute(
                "SELECT relname::text FROM pg_class WHERE relnamespace = %s::regnamespace "
                "UNION ALL SELECT conname::text FROM pg_constraint "
                "WHERE connamespace = %s::regnamespace "
                "UNION ALL SELECT tgname::text FROM pg_trigger t JOIN pg_class c "
                "ON c.oid = t.tgrelid WHERE c.relnamespace = %s::regnamespace "
                "UNION ALL SELECT proname::text FROM pg_proc WHERE pronamespace = %s::regnamespace "
                "UNION ALL SELECT attname::text FROM pg_attribute a JOIN pg_class c "
                "ON c.oid = a.attrelid WHERE c.relnamespace = %s::regnamespace "
                "AND c.relname LIKE 'automations\\_%%' AND NOT c.relname LIKE '%%premig%%' "
                "AND c.relkind = 'r' AND a.attnum > 0", [schema] * 5).fetchall()]
            assert not [n for n in names if "automations_definitions" in n
                        or "project_id" in n], names
            said = [r[0] for r in admin.execute(
                "SELECT coalesce(obj_description(p.oid, 'pg_proc'), '') || "
                "pg_get_functiondef(p.oid) FROM pg_proc p "
                "WHERE pronamespace = %s::regnamespace AND proname LIKE 'automations\\_%%'",
                [schema]).fetchall()]
            said += [r[0] for r in admin.execute(
                "SELECT description FROM pg_description d JOIN pg_class c ON c.oid = d.objoid "
                "WHERE c.relnamespace = %s::regnamespace AND c.relname LIKE 'automations\\_%%' "
                "AND NOT c.relname LIKE '%%premig%%'", [schema]).fetchall()]
            assert not [text for text in said if "project" in text], said
            with pytest.raises(psycopg.errors.RestrictViolation,
                               match="automations_definition_versions"):
                admin.execute("UPDATE automations_definition_versions SET slug = 'other'")
            admin.execute("LISTEN automations_changed")
            ledger.save_definition("job", {**ledger.definition("job"), "script_path": "c.py"},
                                   by="u@n")
            said = [json.loads(n.payload) for n in admin.notifies(timeout=2, stop_after=2)]
        assert {"schema": schema, "agent_id": "prj_a", "slug": "job"} in said
        assert [row["script_path"] for row in ledger.definition_history("job")] == [
            "a.py", "b.py", "c.py"]
    finally:
        link.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
