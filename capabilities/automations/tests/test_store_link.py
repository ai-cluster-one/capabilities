"""Which connection each link holds, and a scheduler that starts while its store is away.

Only the scheduler's link listens, on a session connection; a command's link is a
transaction connection that listens to nothing. A scheduler whose store does not
answer when it starts waits for it on a doubling interval instead of exiting. The
store-backed checks read AUTOMATIONS_TEST_DSN and skip when it is unset."""

from __future__ import annotations

import importlib.util
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
def test_only_the_schedulers_link_listens(runtime):
    import psycopg
    schema = "automations_links_" + uuid.uuid4().hex[:8]
    command = runtime.StoreLink(_setting(schema)).open()
    scheduler = runtime.StoreLink(_setting(schema), listen=True).open()
    try:
        assert command.conn.execute("SELECT pg_listening_channels()").fetchall() == []
        assert scheduler.conn.execute("SELECT pg_listening_channels()").fetchall() == [
            ("automations_changed",)]
        # A command's link prepares nothing and binds its schema per transaction.
        for _ in range(8):
            command.conn.execute("SELECT count(*) FROM automations_runs").fetchone()
        assert command.conn.execute(
            "SELECT count(*) FROM pg_prepared_statements").fetchone() == (0,)
        assert command.conn.execute("SHOW search_path").fetchone() == (schema,)
        with pytest.raises(Exception) as refused:
            command.conn.execute("LISTEN automations_changed")
        assert getattr(refused.value, "slug", None) == "session_state"
    finally:
        command.close()
        scheduler.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
