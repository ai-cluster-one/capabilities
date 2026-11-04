"""The telegrambot tables on a real PostgreSQL: a message reserved once before
anything is done with it, the watermark and the bot-turn count moving in one
commit, `/set` overrides, the seen list, and prompt versions.

Needs TELEGRAMBOT_TEST_DSN, a libpq URL for a throwaway database; each case
binds a schema of its own. Skipped without it."""

from __future__ import annotations

import os
import uuid

import pytest

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
