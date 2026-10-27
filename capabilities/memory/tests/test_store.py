"""The tables and what the store itself refuses: immutable text, an append-only
trail, locked status moves, the one hard delete, the notification, and a
grooming pass applied and undone."""

from __future__ import annotations

import json

import psycopg
import pytest

from _cli import drop_schema, engine, needs_store, new_schema, setting

E = engine()
P = "prj_000000000001"
OTHER = "prj_000000000002"

pytestmark = needs_store


@pytest.fixture
def store():
    schema = new_schema()
    opened = E.Store(setting(schema)).open()
    try:
        yield opened
    finally:
        opened.close()
        drop_schema(schema)


def add(store, title, body="text", project=P, pinned=False):
    row, created = store.add(project, title, body, scope=None, pinned=pinned, actor="tester",
                             node="node-a")
    return row


def test_add_is_idempotent_and_leaves_one_event(store):
    first, created = store.add(P, "t", "b", scope=None, pinned=False, actor="a", node="n")
    again, created_again = store.add(P, "t", "b", scope=None, pinned=False, actor="a", node="n")
    assert created and not created_again and first["id"] == again["id"]
    rows, total = store.log(P, since=None, limit=10)
    assert total == 1 and rows[0]["kind"] == "add"


def test_text_is_immutable_and_trail_append_only(store):
    row = add(store, "t")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.one("UPDATE memory_entries SET body = 'changed' WHERE id = %s RETURNING id",
                  (row["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        store.one("DELETE FROM memory_entries WHERE id = %s RETURNING id", (row["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        store.one("UPDATE memory_events SET reason = 'x' RETURNING seq")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.one("DELETE FROM memory_events RETURNING seq")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.conn.execute("TRUNCATE memory_events")


def test_status_moves_are_locked(store):
    row = add(store, "t")
    store.drop(P, str(row["id"]), reason="gone", to="doc/x.md", purge=False, actor="a", node="n")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.one("UPDATE memory_entries SET status = 'superseded', superseded_by = id "
                  "WHERE id = %s RETURNING id", (row["id"],))
    shown = store.resolve(P, str(row["id"]))
    assert shown["status"] == "dropped" and shown["destination"] == "doc/x.md"


def test_supersede_points_old_at_new_and_carries_the_pin(store):
    old = add(store, "decision", "first wording", pinned=True)
    out = store.supersede(P, [E.short(old["id"])], [("decision v2", "second wording")],
                          reason="changed", pin=False, actor="a", node="n")
    new = out["created"][0]
    assert new["pinned"] is True
    assert out["superseded"][0]["status"] == "superseded"
    assert out["superseded"][0]["superseded_by"] == new["id"]
    with pytest.raises(E.Refusal) as caught:
        store.supersede(P, [str(old["id"])], [("again", "x")], reason=None, pin=False,
                        actor="a", node="n")
    assert caught.value.code == "not_active"


def test_a_write_to_another_projects_entry_is_refused(store):
    theirs = add(store, "theirs", project=OTHER)
    with pytest.raises(E.Refusal) as caught:
        store.drop(P, str(theirs["id"]), reason="x", to=None, purge=False, actor="a", node="n")
    assert caught.value.exit_code == 4 and caught.value.code == "foreign_project"
    assert store.resolve(P, str(theirs["id"]))["project_id"] == OTHER


def test_prefix_resolution(store):
    row = add(store, "t")
    assert store.resolve(P, E.short(row["id"]))["id"] == row["id"]
    with pytest.raises(E.Refusal) as caught:
        store.resolve(OTHER, E.short(row["id"]))
    assert caught.value.exit_code == 3
    with pytest.raises(E.Refusal):
        store.resolve(P, "zz")


def test_purge_is_the_one_hard_delete_and_keeps_the_trail(store):
    row = add(store, "leaked", "secret-value-here")
    with pytest.raises(E.Refusal) as caught:
        store.drop(P, E.short(row["id"]), reason="leak", to=None, purge=True, actor="a",
                   node="n")
    assert caught.value.code == "purge_needs_id"
    store.drop(P, str(row["id"]), reason="leak", to=None, purge=True, actor="a", node="n")
    assert store.one("SELECT count(*) AS n FROM memory_entries WHERE id = %s",
                     (row["id"],))["n"] == 0
    kinds = [e["kind"] for e in store.trail(row["id"])]
    assert kinds == ["add", "purge"]
    exported = json.dumps(store.export(P))
    assert "secret-value-here" not in exported


def test_search_matches_words_and_substrings(store):
    add(store, "Deploy lesson", "The node needs its volume mounted before boot.")
    add(store, "Другое", "Память живёт в базе данных.")
    dropped = add(store, "old", "volume notes that no longer hold")
    store.drop(P, str(dropped["id"]), reason="x", to=None, purge=False, actor="a", node="n")
    rows, total = store.search(P, "volume", everything=False, limit=10)
    assert [r["title"] for r in rows] == ["Deploy lesson"] and total == 1
    rows, total = store.search(P, "volume", everything=True, limit=10)
    assert total == 2
    rows, _ = store.search(P, "базе", everything=False, limit=10)
    assert [r["title"] for r in rows] == ["Другое"]
    rows, _ = store.search(P, "mount", everything=False, limit=10)
    assert [r["title"] for r in rows] == ["Deploy lesson"]


def test_every_change_is_announced_on_commit(store):
    from capabilities_contract import db
    listener = db.connect(application_name="memory-test", setting=store.setting)
    listener.autocommit = True
    listener.execute(f"LISTEN {E.CHANNEL}")
    try:
        row = add(store, "announced")
        notes = list(listener.notifies(timeout=5, stop_after=1))
    finally:
        listener.close()
    payload = json.loads(notes[0].payload)
    assert payload == {"schema": store.schema, "project": P, "kind": "add",
                       "entry": str(row["id"]), "event": payload["event"]}


def test_every_change_raises_its_projects_version_as_it_commits(store):
    from capabilities_contract import db
    follower = db.Follower([E.CHANNEL], application_name="memory-test", setting=store.setting)
    before = follower.read(store.conn).get((E.CHANNEL, P), 0)
    add(store, "versioned")
    assert follower.read(store.conn)[(E.CHANNEL, P)] == before + 1


def test_a_pass_applies_under_one_id_and_undoes(store):
    a = add(store, "a", "alpha")
    b = add(store, "b", "beta")
    c = add(store, "c", "gamma")
    pin = add(store, "pin", "pinned", pinned=True)
    movable, pinned = store.candidates(P, 30)
    assert {r["id"] for r in movable} == {a["id"], b["id"], c["id"]}
    assert [r["id"] for r in pinned] == [pin["id"]]
    plan = {"merges": [{"sources": [str(a["id"]), str(b["id"])], "title": "ab",
                        "body": "alpha and beta", "reason": "same"}],
            "drops": [{"id": str(c["id"]), "reason": "no longer holds"}]}
    done = store.apply_pass(P, plan, actor="memory groom", node="n", summary={"trigger": "t"})
    merged = done["merges"][0]
    assert merged["merged"] == 2 and merged["entry"]["origin"] == "groom"
    rows = {r["title"]: r for r in store.listing(P, status="all", pinned=None, scope=None,
                                                  since=None, limit=50)[0]}
    assert rows["a"]["status"] == rows["b"]["status"] == "superseded"
    assert rows["c"]["status"] == "dropped" and rows["pin"]["status"] == "active"
    assert rows["ab"]["merged"]["count"] == 2
    assert store.last_pass_at(P) is not None

    undone = store.undo_pass(P, done["pass"][:8], actor="a", node="n")
    assert len(undone["reactivated"]) == 3
    rows = {r["title"]: r for r in store.listing(P, status="all", pinned=None, scope=None,
                                                  since=None, limit=50)[0]}
    assert rows["a"]["status"] == rows["b"]["status"] == rows["c"]["status"] == "active"
    assert rows["ab"]["status"] == "dropped"
    with pytest.raises(E.Refusal) as caught:
        store.undo_pass(P, done["pass"], actor="a", node="n")
    assert caught.value.code == "already_undone"


def test_an_overtaken_pass_is_not_undone(store):
    a, b = add(store, "a", "alpha"), add(store, "b", "beta")
    done = store.apply_pass(P, {"merges": [{"sources": [str(a["id"]), str(b["id"])],
                                            "title": "ab", "body": "ab", "reason": None}],
                                "drops": []}, actor="g", node="n", summary={})
    made = done["merges"][0]["entry"]["id"]
    store.supersede(P, [made], [("ab2", "ab revised")], reason=None, pin=False, actor="a",
                    node="n")
    with pytest.raises(E.Refusal) as caught:
        store.undo_pass(P, done["pass"], actor="a", node="n")
    assert caught.value.code == "pass_overtaken"


def test_a_plan_whose_entries_changed_is_skipped(store):
    a, b = add(store, "a", "alpha"), add(store, "b", "beta")
    store.set_pin(P, str(b["id"]), True, actor="a", node="n")
    done = store.apply_pass(P, {"merges": [{"sources": [str(a["id"]), str(b["id"])],
                                            "title": "ab", "body": "ab", "reason": None}],
                                "drops": []}, actor="g", node="n", summary={})
    assert done["merges"] == [] and "changed since the plan" in done["skipped"][0]["why"]
    assert store.resolve(P, str(a["id"]))["status"] == "active"


def test_one_pass_at_a_time_per_project(store):
    """A pass holds its project's lease row, beside the session lock an earlier
    release keeps passes out by, so a second pass does not start while either
    is held; a transaction connection refuses to take the lock."""
    from capabilities_contract import db
    one = E.Store(store.setting, session=True).open()
    other = E.Store(store.setting, session=True).open()
    try:
        assert one.try_groom_lock(P, 600, node="node-a")
        [lease] = store.rows("SELECT * FROM memory_groom_leases")
        assert lease["project_id"] == P and lease["node"] == "node-a"
        assert lease["lock_pid"] == one.one("SELECT pg_backend_pid() AS p")["p"]
        assert not other.try_groom_lock(P, 600)
        assert other.try_groom_lock(OTHER, 600)
        other.groom_unlock(OTHER)
        one.groom_unlock(P)
        assert store.rows("SELECT * FROM memory_groom_leases") == []
        assert other.try_groom_lock(P, 600)
        other.groom_unlock(P)
        with pytest.raises(db.DbError) as refused:
            store.try_groom_lock(P, 600)
        assert refused.value.slug == "session_state"
        assert store.rows("SELECT * FROM memory_groom_leases") == []
    finally:
        one.close()
        other.close()


def test_the_lease_keeps_a_pass_out_without_the_session_lock(store):
    """The row alone keeps a second pass out until it expires, so the guard
    does not rest on the session that took it."""
    one = E.Store(store.setting, session=True).open()
    other = E.Store(store.setting, session=True).open()
    try:
        assert one.try_groom_lock(P, 600)
    finally:
        one.close()  # the session lock goes with it; the row stays
    try:
        assert not other.try_groom_lock(P, 600)
        store.one("UPDATE memory_groom_leases SET expires_at = now() - interval '1 second' "
                  "RETURNING project_id")
        assert other.try_groom_lock(P, 600)
    finally:
        other.close()


def test_a_pass_of_an_earlier_release_and_this_one_keep_each_other_out(store):
    """An earlier release takes only the session lock: while it holds it no
    lease is taken, and while a lease holder holds it the earlier pass is
    refused its lock."""
    old = E.Store(store.setting, session=True).open()
    new = E.Store(store.setting, session=True).open()
    take = ("SELECT pg_try_advisory_lock(hashtext('memory groom ' || %s), hashtext(%s)) "
            "AS got")
    try:
        assert old.one(take, (store.schema, P))["got"]
        assert not new.try_groom_lock(P, 600)
        assert store.rows("SELECT * FROM memory_groom_leases") == []
        old.one("SELECT pg_advisory_unlock(hashtext('memory groom ' || %s), hashtext(%s)) "
                "AS done", (store.schema, P))
        assert new.try_groom_lock(P, 600)
        assert not old.one(take, (store.schema, P))["got"]
    finally:
        old.close()
        new.close()


def test_adopt_carries_once_per_key(store):
    import datetime as dt
    when = dt.datetime(2026, 9, 14, 0, 1, tzinfo=dt.timezone.utc)
    key = E.adopt_key(P, "t", "b")
    row, created = store.adopt(P, "t", "b", created_at=when, key=key, source="f.md#t",
                               pinned=False, actor="a", node="n")
    again, created_again = store.adopt(P, "t", "b", created_at=when, key=key, source="f.md#t",
                                       pinned=False, actor="a", node="node-b")
    assert created and not created_again and str(row["id"]) == again["id"]
    assert row["created_at"] == when
    counts = store.counts(P)
    assert counts["entries"]["active"] == 1 and counts["writes"]["last_7_days"] == 0
