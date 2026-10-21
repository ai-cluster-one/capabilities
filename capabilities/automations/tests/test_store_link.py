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
                writer.execute("INSERT INTO automations_definitions (project_id, slug, "
                               "script_path) VALUES ('prj_a', 'kept', 'job.py')")
                writer.commit()
                writer.execute("INSERT INTO automations_definitions (project_id, slug, "
                               "script_path) VALUES ('prj_b', 'undone', 'job.py')")
                writer.rollback()
            said = [json.loads(n.payload) for n in earlier.notifies(timeout=1, stop_after=1)]
        assert said == [{"schema": schema, "project": "prj_a", "slug": "kept"}]

        scheduler._polled_at = None
        assert scheduler.take_changed("prj_a") is True
        assert scheduler.take_changed("prj_a") is False
        assert scheduler.take_changed("prj_b") is False
    finally:
        command.close()
        scheduler.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
