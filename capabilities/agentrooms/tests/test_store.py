"""The service's tables on a real PostgreSQL: the lease that keeps a second
process off an agent, the sync record it guards, wakes claimed one turn at a
time, the room-to-session map and a project's pause.

Needs AGENTROOMS_TEST_DSN, a libpq URL for a throwaway database; each case
binds a schema of its own. Skipped without it."""

from __future__ import annotations

import os
import uuid

import pytest

from _cli import load_engine

DSN = os.environ.get("AGENTROOMS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="AGENTROOMS_TEST_DSN is not set")

engine = load_engine()


@pytest.fixture()
def store():
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = f"ar_test_{uuid.uuid4().hex[:8]}"
    setting = db.Setting(schema=schema, level="machine", sources=("AGENTROOMS_TEST_DSN",),
                         host=info.get("host") or "127.0.0.1", port=int(info.get("port") or 5432),
                         database=info.get("dbname"), user=info.get("user"),
                         password=info.get("password"), sslmode=info.get("sslmode") or "disable")
    opened = engine.Store(setting).open()
    yield opened
    opened.execute(f"DROP SCHEMA {schema} CASCADE")
    opened.close()


def test_one_holder_per_agent(store):
    assert store.acquire("p", "alpha", "h1", "n1", 1) is None
    assert store.acquire("p", "alpha", "h1", "n1", 1) is None  # renewal
    held = store.acquire("p", "alpha", "h2", "n2", 2)
    assert held["holder"] == "h1"
    assert store.acquire("p", "beta", "h2", "n2", 2) is None  # another agent is free
    store.release("p", "alpha", "h1")
    assert store.acquire("p", "alpha", "h2", "n2", 2) is None


def test_a_sync_is_recorded_only_by_the_holder(store):
    wake = {"room_id": "!r", "key": "$1", "kind": "mention", "event_id": "$1", "sender": "@b:x"}
    assert store.acquire("p", "alpha", "h1", "n1", 1) is None
    assert store.save_sync("p", "alpha", "@alpha:x", "h1", "b1", [wake, wake]) is True
    assert store.next_batch("p", "alpha", "@alpha:x") == "b1"
    assert store.next_batch("p", "alpha", "@other:x") is None
    assert store.save_sync("p", "alpha", "@alpha:x", "h2", "b2", []) is False
    assert store.next_batch("p", "alpha", "@alpha:x") == "b1"
    assert [(a, r) for a, r, _ in store.ready("p")] == [("alpha", "!r")]


def test_wakes_are_claimed_once_and_new_ones_wait(store):
    store.acquire("p", "alpha", "h1", "n1", 1)
    first = {"room_id": "!r", "key": "$1", "kind": "mention", "event_id": "$1", "sender": "@b:x"}
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b1", [first])
    claimed = store.claim("p", "alpha", "!r", "t1")
    assert [w["event_id"] for w in claimed] == ["$1"]
    assert store.claim("p", "alpha", "!r", "t2") == []
    second = {**first, "key": "$2", "event_id": "$2"}
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b2", [second])
    store.finish("p", "alpha", "!r", "t1")
    assert [w["event_id"] for w in store.claim("p", "alpha", "!r", "t3")] == ["$2"]
    store.restore("p", "alpha", "!r", "t3")
    assert store.unclaim("p", "alpha") == 0
    store.claim("p", "alpha", "!r", "t4")
    assert store.unclaim("p", "alpha") == 1


def test_sessions_and_pause(store):
    assert store.session("p", "alpha", "!r") is None
    store.save_session("p", "alpha", "!r", harness="claude", session_id="s1", node="n1", seen_ts=5)
    store.save_session("p", "alpha", "!r", harness="claude", session_id="s1", node="n1", seen_ts=3)
    found = store.session("p", "alpha", "!r")
    assert found == {"harness": "claude", "session_id": "s1", "node": "n1", "seen_ts": 5, "turns": 2}
    assert store.pause_of("p") is None
    store.pause("p", "me@host", "testing")
    assert store.pause_of("p")["reason"] == "testing"
    assert store.resume("p") is True and store.pause_of("p") is None
