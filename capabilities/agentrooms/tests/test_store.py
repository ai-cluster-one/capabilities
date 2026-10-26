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
    store.save_session("p", "alpha", "!s", harness="codex", session_id="s2", node="n1", seen_ts=1)
    store.save_session("p", "beta", "!r", harness="claude", session_id="s3", node="n1", seen_ts=1)
    mapped = store.sessions("p", "alpha")
    assert [(r["room_id"], r["session_id"], r["turns"]) for r in mapped] == [("!r", "s1", 2),
                                                                            ("!s", "s2", 1)]
    assert mapped[0]["updated_at"]
    assert [r["session_id"] for r in store.sessions("p", "alpha", "!s")] == ["s2"]
    assert store.sessions("p", "alpha", "!none") == []
    assert store.pause_of("p") is None
    store.pause("p", "me@host", "testing")
    assert store.pause_of("p")["reason"] == "testing"
    assert store.resume("p") is True and store.pause_of("p") is None


def test_workers_are_versions_and_history_is_never_edited(store):
    first, created = store.save_worker("p", "w", {"accepts_from": ["*"]}, "Role.", base=None, by="me")
    assert created and first["active"]
    same, created = store.save_worker("p", "w", {"accepts_from": ["*"]}, "Role.", base=None, by="me")
    assert not created and same["id"] == first["id"]
    with pytest.raises(engine.WorkerConflict):
        store.save_worker("p", "w", {"accepts_from": []}, "", base="nope", by="me")
    second, _ = store.save_worker("p", "w", {"accepts_from": ["@a:x"]}, "", base=first["id"], by="me")
    assert store.worker("p", "w")["id"] == second["id"]
    assert store.worker("p", "w", first["id"])["body"] == "Role."
    assert store.worker("other", "w") is None
    assert [v["active"] for v in store.worker_history("p", "w")] == [False, True]
    with pytest.raises(Exception, match="never edited or deleted"):
        store.execute("UPDATE agentrooms_workers SET body = 'x' WHERE id = %s", (first["id"],))
    with pytest.raises(Exception, match="never edited or deleted"):
        store.execute("DELETE FROM agentrooms_workers WHERE id = %s", (first["id"],))
    assert store.remove_worker("p", "w", base=None) == second["id"]
    assert store.worker("p", "w") is None and store.remove_worker("p", "w", base=None) is None
    assert store.workers("p") == []


def test_turn_counts_reset_a_room_after_quiet_and_skip_refusals(store):
    for n, ago in enumerate((10, 20, 2000)):
        store.start_turn(f"t{n}", "p", "alpha", "!r", "w", None, ["@b:x"], ["mention"])
        store.execute("UPDATE agentrooms_turns SET started_at = now() - make_interval(secs => %s) "
                      "WHERE id = %s", (ago, f"t{n}"))
    # A refusal, and one an earlier release recorded as the default worker's.
    store.start_turn("refusal:t9:@s:x", "p", "alpha", "!r", "refusal", None, ["@s:x"], ["direct"])
    store.start_turn("default:t8:@o:x", "p", "alpha", "!r", "default", None, ["@o:x"], ["direct"])
    counts = store.turn_counts("p", "alpha", "!r", ["@b:x", "@s:x"], window=3600, quiet=900,
                               room_cap=10)
    assert counts == {"agent": 3, "room": 2, "senders": {"@b:x": 3, "@s:x": 0}}
    assert store.turn_counts("p", "alpha", "!r", [], window=15, quiet=5, room_cap=10)["room"] == 0
    assert store.refused("p", "alpha", "!r", "@s:x", 60)
    assert store.refused("p", "alpha", "!r", "@o:x", 60)
    assert not store.refused("p", "alpha", "!other", "@s:x", 60)
    # A turn the default worker ran is a turn, and counts.
    store.start_turn("t7", "p", "alpha", "!r", "default", None, ["@s:x"], ["mention"])
    assert store.turn_counts("p", "alpha", "!r", ["@s:x"], window=3600, quiet=900,
                             room_cap=10)["senders"] == {"@s:x": 1}
    assert not store.refused("p", "alpha", "!r", "@b:x", 60)
    # So is one a project worker named `refusal` ran: it is held to its limits.
    store.start_turn("t6", "p", "alpha", "!r", "refusal", None, ["@q:x"], ["mention"])
    assert store.turn_counts("p", "alpha", "!r", ["@q:x"], window=3600, quiet=900,
                             room_cap=10)["senders"] == {"@q:x": 1}
    assert not store.refused("p", "alpha", "!r", "@q:x", 60)
    store.end_turn("t0", "answered")
    assert store.execute("SELECT outcome FROM agentrooms_turns WHERE id = 't0'", fetch="one") == ("answered",)
    assert store.answered_by("p", "alpha", "$a") is None
    store.end_turn("t1", "answered", event_id="$a", harness="claude", session_id="s1", node="n1")
    found = store.answered_by("p", "alpha", "$a")
    assert {k: found[k] for k in ("turn", "room_id", "worker", "harness", "session_id", "node",
                                  "outcome")} == {
        "turn": "t1", "room_id": "!r", "worker": "w", "harness": "claude", "session_id": "s1",
        "node": "n1", "outcome": "answered"}
    assert found["started_at"]
    assert store.answered_by("p", "beta", "$a") is None


def test_only_the_named_wakes_are_handed_back(store):
    store.acquire("p", "alpha", "h1", "n1", 1)
    wakes = [{"room_id": "!r", "key": k, "kind": "mention", "event_id": k, "sender": "@b:x"}
             for k in ("$1", "$2")]
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b1", wakes)
    store.claim("p", "alpha", "!r", "t1")
    store.restore("p", "alpha", "!r", "t1", ["$2"])
    store.finish("p", "alpha", "!r", "t1")
    assert [w["key"] for w in store.claim("p", "alpha", "!r", "t2")] == ["$2"]


def test_held_wakes_wait_for_the_operator_and_a_grant_releases_them(store):
    import json

    import psycopg

    listener = psycopg.connect(DSN, autocommit=True)
    listener.execute(f"LISTEN {engine.HELD_CHANNEL}")
    store.acquire("p", "alpha", "h1", "n1", 1)

    def wake(key, sender, held):
        return {"room_id": "!r", "key": key, "kind": "mention", "event_id": key, "sender": sender,
                "held": held}

    store.save_sync("p", "alpha", "@alpha:x", "h1", "b1",
                    [wake("$1", "@c:x", True), wake("$2", "@b:x", False), wake("$3", "@d:x", True)])
    # A held wake is never claimed; the rest of the room is.
    assert [w["key"] for w in store.claim("p", "alpha", "!r", "t1")] == ["$2"]
    store.finish("p", "alpha", "!r", "t1")
    assert store.ready("p") == []
    assert store.held_rooms("p", "alpha") == {"!r": {"count": 2, "senders": ["@c:x", "@d:x"]}}
    assert [(w["event_id"], w["sender"]) for w in store.held("p", "alpha")] == [
        ("$1", "@c:x"), ("$3", "@d:x")]
    assert store.held("p", "alpha", "!other") == [] and store.held_rooms("p", "beta") == {}
    # A grant the sync brings releases what it now admits, and only that.
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b2", [],
                    {"!r": {"everyone": False, "users": ["@c:x"]}})
    assert [w["key"] for w in store.claim("p", "alpha", "!r", "t2")] == ["$1"]
    store.finish("p", "alpha", "!r", "t2")
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b3", [wake("$4", "@d:x", True),
                                                           wake("$5", "@e:x", True)])
    # Answered once: released into a turn, the rest still held.
    assert [w["event_id"] for w in store.answer_held("p", "alpha", "!r", "@d:x")] == ["$3", "$4"]
    assert [w["key"] for w in store.claim("p", "alpha", "!r", "t3")] == ["$3", "$4"]
    store.finish("p", "alpha", "!r", "t3")
    assert [w["event_id"] for w in store.dismiss_held("p", "alpha", "!r")] == ["$5"]
    assert store.held_rooms("p", "alpha") == {} and store.ready("p") == []
    # No grant, or everyone on, releases every held wake in the room.
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b4", [wake("$6", "@f:x", True)])
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b5", [], {"!r": None})
    assert [w["key"] for w in store.claim("p", "alpha", "!r", "t4")] == ["$6"]
    store.finish("p", "alpha", "!r", "t4")
    # Every change to what is held was announced, with ids only, and raised
    # the agent's version once for each of the ten held rows it changed; a
    # transaction's identical notifications fold into one.
    said = [json.loads(n.payload) for n in listener.notifies(timeout=1.0)]
    version = listener.execute(
        f'SELECT version FROM "{store.setting.schema}".schema_channel '
        "WHERE channel = %s AND scope = %s",
        (engine.HELD_CHANNEL, engine.held_scope("p", "alpha"))).fetchone()
    listener.close()
    assert said and all(n == {"schema": store.setting.schema, "project": "p", "agent": "alpha",
                              "room": "!r"} for n in said)
    assert version == (10,) and len(said) <= 10


def test_catch_up_takes_the_admitted_wakes_and_a_later_one_is_known_taken(store):
    store.acquire("p", "alpha", "h1", "n1", 1)
    wake = {"room_id": "!r", "kind": "mention", "sender": "@b:x"}
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b1", [{**wake, "key": "$1", "event_id": "$1"}])
    store.claim("p", "alpha", "!r", "t1")
    store.start_turn("t1", "p", "alpha", "!r", "w", None, ["@b:x"], ["mention"])
    store.mark_turn("t1", 100)
    assert store.running_turn("t1")["seen_ts"] == 100
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b2", [
        {**wake, "key": "$2", "event_id": "$2"},
        {**wake, "key": "$3", "event_id": "$3", "sender": "@s:x"}])
    events = [{"event_id": f"${n}", "origin_server_ts": 100 + n} for n in (2, 3, 4)]
    took = store.catch_up("t1", events, lambda w: w["sender"] == "@b:x")
    assert [w["event_id"] for w in took] == ["$2"]
    turn = store.execute("SELECT seen_ts, taken, senders FROM agentrooms_turns WHERE id = 't1'",
                         fetch="one")
    assert turn[0] == 104 and sorted(turn[1]) == ["$2", "$3", "$4"] and turn[2] == ["@b:x"]
    assert store.turn_seen("t1") == 104
    # A wake for a message the turn took, recorded after catch-up ran.
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b3", [
        {**wake, "key": "$4", "event_id": "$4", "sender": "@c:x"}])
    store.end_turn("t1", "answered")
    store.finish("p", "alpha", "!r", "t1")
    assert store.catch_up("t1", [], lambda w: True) is None
    pending = store.claim("p", "alpha", "!r", "t2")
    assert sorted(w["event_id"] for w in pending) == ["$3", "$4"]
    known = store.taken_before("p", "alpha", "!r", pending)
    assert [(w["event_id"], w["taken_by"]) for w in known] == [("$3", "t1"), ("$4", "t1")]
    assert sorted(store.execute("SELECT senders FROM agentrooms_turns WHERE id = 't1'",
                                fetch="one")[0]) == ["@b:x", "@c:x", "@s:x"]


def test_a_turn_that_took_nothing_hands_its_taken_wakes_back(store):
    store.acquire("p", "alpha", "h1", "n1", 1)
    wake = {"room_id": "!r", "kind": "mention", "sender": "@b:x"}
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b1", [{**wake, "key": "$1", "event_id": "$1"}])
    store.claim("p", "alpha", "!r", "t1")
    store.start_turn("t1", "p", "alpha", "!r", "w", None, ["@b:x"], ["mention"])
    store.save_sync("p", "alpha", "@alpha:x", "h1", "b2", [{**wake, "key": "$2", "event_id": "$2"}])
    store.catch_up("t1", [{"event_id": "$2", "origin_server_ts": 5}], lambda w: True)
    store.untake("p", "alpha", "!r", "t1")
    store.finish("p", "alpha", "!r", "t1")
    assert [w["event_id"] for w in store.claim("p", "alpha", "!r", "t2")] == ["$2"]
    assert store.taken_before("p", "alpha", "!r", [{"key": "$2", "kind": "mention",
                                                     "event_id": "$2", "sender": "@b:x"}]) == []


def test_the_store_keeps_one_working_connection_between_operations(store):
    """Each statement and each transaction borrows the process's working
    connection and gives it back after, so the store holds no connection of
    its own between operations and every operation runs on the same one."""
    import psycopg
    assert store.conn is None
    assert store.acquire("p", "alpha", "h1", "n1", 1) is None
    assert store.conn is None
    with store.transaction() as cur:
        cur.execute("SELECT pg_backend_pid()")
        inside = cur.fetchone()[0]
        assert store.execute("SELECT pg_backend_pid()", fetch="one")[0] == inside
    assert store.conn is None
    assert store.execute("SELECT pg_backend_pid()", fetch="one")[0] == inside
    assert store.conn is None
    with psycopg.connect(DSN, autocommit=True) as admin:
        assert admin.execute("SELECT state FROM pg_stat_activity WHERE pid = %s",
                             (inside,)).fetchone() == ("idle",)
