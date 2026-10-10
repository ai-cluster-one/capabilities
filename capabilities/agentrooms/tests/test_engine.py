"""The service engine's decisions with fakes for the store, the homeserver and
the harness: what wakes an agent, how a turn is built, which session it
resumes, what is posted, and that a room has one turn at a time."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from _cli import load_engine

engine = load_engine()

ME = "@alpha:example.org"
TEMPLATE = "You are $user_id in $room_name; stay quiet with $silent; use --connection $connection."


def sync(join=None, invite=None):
    return {"next_batch": "b2", "rooms": {"join": join or {}, "invite": invite or {}}}


def message(event_id, sender, body, **content):
    return {"type": "m.room.message", "event_id": event_id, "sender": sender,
            "origin_server_ts": 1_700_000_000_000, "content": {"msgtype": "m.text", "body": body,
                                                                **content}}


def test_what_wakes_an_agent():
    rooms: dict = {}
    response = sync(join={
        "!group": {"summary": {"m.joined_member_count": 3},
                   "timeline": {"events": [
                       message("$1", "@b:example.org", "chatter"),
                       message("$2", "@b:example.org", f"hey {ME}"),
                       message("$3", "@b:example.org", "x", **{"m.mentions": {"user_ids": [ME]}}),
                       message("$4", ME, f"my own {ME}")]}},
        "!direct": {"summary": {"m.joined_member_count": 2},
                    "timeline": {"events": [message("$5", "@b:example.org", "hi")]}},
        "!marked": {"summary": {"m.joined_member_count": 5},
                    "account_data": {"events": [{"type": engine.MARK_TYPE,
                                                 "content": {"marked": True}}]},
                    "timeline": {"events": [message("$6", "@b:example.org", "anything")]}},
        "!secret": {"summary": {"m.joined_member_count": 2},
                    "state": {"events": [{"type": "m.room.encryption", "state_key": "",
                                          "content": {}}]},
                    "timeline": {"events": [message("$7", "@b:example.org", "hidden")]}},
    }, invite={
        "!invited": {"invite_state": {"events": [
            {"type": "m.room.member", "state_key": ME, "sender": "@b:example.org",
             "content": {"membership": "invite"}}]}},
        "!locked": {"invite_state": {"events": [
            {"type": "m.room.encryption", "state_key": "", "content": {}},
            {"type": "m.room.member", "state_key": ME, "sender": "@b:example.org"}]}},
    })
    wakes, notes = engine.read_sync(response, ME, rooms, initial=False, member_count=lambda r: None)
    found = {(w["room_id"], w["event_id"], w["kind"]) for w in wakes}
    assert found == {("!group", "$2", "mention"), ("!group", "$3", "mention"),
                     ("!direct", "$5", "direct"), ("!marked", "$6", "marked"),
                     ("!invited", None, "invite")}
    assert any("!secret" in n for n in notes) and any("!locked" in n for n in notes)


def test_history_before_the_first_sync_wakes_nothing_but_invites():
    response = sync(join={"!direct": {"summary": {"m.joined_member_count": 2},
                                      "timeline": {"events": [message("$1", "@b:example.org", "old")]}}},
                    invite={"!invited": {"invite_state": {"events": []}}})
    wakes, _notes = engine.read_sync(response, ME, {}, initial=True, member_count=lambda r: 2)
    assert [w["kind"] for w in wakes] == ["invite"]


def test_prompt_marks_what_woke_the_agent():
    agent = {"user_id": ME, "connection": "alpha"}
    events = [message("$1", "@b:example.org", "first"),
              message("$2", "@b:example.org", "second", **{"m.relates_to": {
                  "rel_type": "m.thread", "event_id": "$1"}})]
    prompt = engine.build_prompt(TEMPLATE, agent=agent, room_id="!r", room_name="lab",
                                 wakes=[{"kind": "mention", "event_id": "$2"}], events=events,
                                 primed=True)
    assert f"You are {ME} in lab" in prompt and "[silent]" in prompt and "--connection alpha" in prompt
    assert "=== Room history ===" in prompt
    assert "in thread $1, WOKE YOU" in prompt
    resumed = engine.build_prompt(TEMPLATE, agent=agent, room_id="!r", room_name=None,
                                  wakes=[{"kind": "invite", "event_id": None,
                                          "sender": "@b:example.org"}], events=[], primed=False)
    assert "invited to this room by @b:example.org" in resumed


def test_answers_cut_and_endings_judged():
    assert engine.cut_answer("  [silent] ") == ""
    assert engine.cut_answer("thinking...\n[silent]") == ""
    assert engine.cut_answer("Blue") == "Blue"
    ran = SimpleNamespace(ok=False, failure=SimpleNamespace(kind="timeout", message=""))
    never = SimpleNamespace(ok=False, failure=SimpleNamespace(kind="not_authenticated", message=""))
    assert engine.harness_ran(SimpleNamespace(ok=True)) and engine.harness_ran(ran)
    assert not engine.harness_ran(never)
    gone = SimpleNamespace(ok=False, stderr_tail="", failure=SimpleNamespace(
        kind="error", message="No conversation found with session ID: abc"))
    assert engine.missing_session(gone) and not engine.missing_session(ran)


# ── one turn, with fakes ─────────────────────────────────────────────────────


class FakeStore:
    def __init__(self):
        self.wakes: list[dict] = []
        self.sessions: dict = {}
        self.lock = threading.Lock()

    def add(self, room, event_id, kind="mention"):
        with self.lock:
            self.wakes.append({"agent": "alpha", "room_id": room, "key": event_id or "invite",
                               "kind": kind, "event_id": event_id, "sender": "@b:example.org",
                               "received_at": time.time(), "claimed_by": None})

    def ready(self, project):
        with self.lock:
            pairs = {}
            for w in self.wakes:
                if w["claimed_by"] is None:
                    pairs.setdefault((w["agent"], w["room_id"]), w["received_at"])
            return [(a, r, t) for (a, r), t in pairs.items()]

    def claim(self, project, agent, room, turn):
        with self.lock:
            out = [w for w in self.wakes if w["room_id"] == room and w["claimed_by"] is None]
            for w in out:
                w["claimed_by"] = turn
            return [dict(w) for w in out]

    def finish(self, project, agent, room, turn):
        with self.lock:
            self.wakes = [w for w in self.wakes if w["claimed_by"] != turn]

    def restore(self, project, agent, room, turn):
        with self.lock:
            for w in self.wakes:
                if w["claimed_by"] == turn:
                    w["claimed_by"] = None

    def session(self, project, agent, room):
        return self.sessions.get(room)

    def save_session(self, project, agent, room, *, harness, session_id, node, seen_ts):
        self.sessions[room] = {"harness": harness, "session_id": session_id, "node": node,
                               "seen_ts": seen_ts, "turns": 1}


class NotFound(Exception):
    exit_code = 3


class FakeMatrix:
    def __init__(self, room_events):
        self.room_events = room_events
        self.posted: list[tuple[str, dict]] = []

    def call(self, method, path, params=None, body=None):
        if path.endswith("/state/m.room.encryption/"):
            raise NotFound("none")
        if path.endswith("/state/m.room.name/"):
            return {"name": "lab"}
        if path.endswith("/messages"):
            return {"chunk": list(reversed(self.room_events))}
        if "/send/m.room.message/" in path:
            from urllib.parse import unquote
            self.posted.append((unquote(path.split("/rooms/")[1].split("/")[0]), body))
            return {"event_id": f"$posted{len(self.posted)}"}
        raise AssertionError(path)

    def close(self):
        pass


class FakeRunner:
    class Session:
        def __init__(self, kind, id=None):
            self.kind, self.id = kind, id

        @classmethod
        def fresh(cls):
            return cls("fresh")

        @classmethod
        def resume(cls, session_id):
            return cls("resume", session_id)

    def __init__(self, answers, hold=None):
        self.calls: list[dict] = []
        self.answers = list(answers)
        self.hold = hold

    def run(self, prompt, profile, cwd, *, session, environ, extra_env, cancel):
        self.calls.append({"prompt": prompt, "session": session, "cwd": cwd,
                           "extra_env": extra_env})
        if self.hold is not None:
            self.hold.wait(5)
        answer = self.answers.pop(0)
        if isinstance(answer, SimpleNamespace):
            return answer
        sid = session.id or f"sess{len(self.calls)}"
        return SimpleNamespace(ok=True, answer=answer, session_id=sid, failure=None)


class FakeHost:
    def __init__(self, matrix, runner, refusal=None):
        self._matrix, self._runner, self.refusal = matrix, runner, refusal

    def node(self):
        return "node-1"

    def tag(self):
        return "project"

    def check(self, entry, cid):
        return self.refusal

    def matrix(self, homeserver, token, timeout=60.0):
        return self._matrix

    def runner(self):
        return self._runner

    def template(self, entry):
        return TEMPLATE

    def turn_env(self, entry):
        return {}


def make_engine(tmp_path, matrix, runner, refusal=None, max_turns=2):
    host = FakeHost(matrix, runner, refusal)
    eng = engine.Engine(host, mode="project", state_dir=tmp_path, echo=False)
    eng.settings = {"max_turns": max_turns}
    slot = engine.Slot(eng, "prj", {"root": str(tmp_path), "slug": "project"})
    slot.store = FakeStore()
    slot.state = "served"
    slot.plan = {"agents": {"alpha": {
        "connection": "alpha", "homeserver": "https://matrix.example.org", "user_id": ME,
        "token": "t", "profile": object(), "profile_name": "agentrooms-claude",
        "harness": "claude", "turn_timeout": 30, "history": 30}}}
    slot.agents["alpha"] = SimpleNamespace(lease=True, row=lambda: {})
    eng.slots["prj"] = slot
    return eng, slot


def test_first_turn_is_primed_and_the_next_resumes(tmp_path):
    events = [message("$1", "@b:example.org", "hello"),
              message("$2", "@b:example.org", f"{ME} question", **{"m.relates_to": {
                  "rel_type": "m.thread", "event_id": "$1"}})]
    matrix = FakeMatrix(events)
    runner = FakeRunner(["first answer", "second answer"])
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.add("!r", "$2")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls[0]["session"].kind == "fresh"
    assert "=== Room history ===" in runner.calls[0]["prompt"]
    assert runner.calls[0]["cwd"] == str(tmp_path)
    assert runner.calls[0]["extra_env"] == {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    room, content = matrix.posted[0]
    assert room == "!r" and content["body"] == "first answer"
    assert content["m.relates_to"]["rel_type"] == "m.thread" and content["m.relates_to"]["event_id"] == "$1"
    assert slot.store.wakes == []
    # The next wake in the room resumes the recorded session.
    events.append({**message("$3", "@b:example.org", f"{ME} again"), "origin_server_ts": 1_800_000_000_000})
    slot.store.add("!r", "$3")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    second = runner.calls[1]
    assert second["session"].kind == "resume" and second["session"].id == "sess1"
    assert "=== New in the room since your last turn ===" in second["prompt"]
    assert "again" in second["prompt"] and "hello" not in second["prompt"]


def test_a_session_from_another_node_starts_fresh_primed(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["answer"])
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.sessions["!r"] = {"harness": "claude", "session_id": "elsewhere",
                                 "node": "node-2", "seen_ts": 1}
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls[0]["session"].kind == "fresh"
    assert slot.store.sessions["!r"]["node"] == "node-1"


def test_a_missing_session_falls_back_to_a_fresh_primed_one(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    gone = SimpleNamespace(ok=False, answer="", session_id="stale", stderr_tail="",
                           failure=SimpleNamespace(kind="error",
                                                   message="No conversation found with session ID"))
    runner = FakeRunner([gone, "answer"])
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.sessions["!r"] = {"harness": "claude", "session_id": "stale", "node": "node-1",
                                 "seen_ts": 0}
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert [c["session"].kind for c in runner.calls] == ["resume", "fresh"]
    assert "=== Room history ===" in runner.calls[1]["prompt"]
    assert matrix.posted[0][1]["body"] == "answer"


def test_silence_posts_nothing_and_a_failed_start_records_no_session(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} thanks!")])
    never = SimpleNamespace(ok=False, answer="", session_id="pinned", stderr_tail="",
                            failure=SimpleNamespace(kind="not_authenticated", message="login"))
    runner = FakeRunner(["[silent]", never])
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert matrix.posted == []
    recorded = dict(slot.store.sessions)
    slot.store.add("!r", "$1b")
    eng.turn(engine.Turn(slot, "alpha", "!s"))  # nothing waits in !s: no turn at all
    assert len(runner.calls) == 1
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert slot.store.sessions == recorded


def test_a_refused_agent_leaves_its_wakes_for_later(tmp_path):
    matrix = FakeMatrix([])
    runner = FakeRunner([])
    eng, slot = make_engine(tmp_path, matrix, runner, refusal=("refused", "no write grant"))
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls == [] and slot.store.wakes[0]["claimed_by"] is None


def test_one_turn_per_room_and_what_arrives_waits_for_the_next(tmp_path):
    hold = threading.Event()
    events = [message("$1", "@b:example.org", f"{ME} one")]
    matrix = FakeMatrix(events)
    runner = FakeRunner(["a1", "a2"], hold=hold)
    eng, slot = make_engine(tmp_path, matrix, runner, max_turns=4)
    slot.store.add("!r", "$1")
    eng.schedule()
    assert len(eng.turns) == 1
    deadline = time.time() + 5
    while not runner.calls and time.time() < deadline:
        time.sleep(0.01)
    # A message arrives mid-turn: it is not claimed by the running turn and no
    # second turn starts for the same room.
    events.append({**message("$2", "@b:example.org", f"{ME} two"), "origin_server_ts": 1_800_000_000_000})
    slot.store.add("!r", "$2")
    eng.schedule()
    assert len(eng.turns) == 1 and len(runner.calls) == 1
    hold.set()
    deadline = time.time() + 5
    while len(runner.calls) < 2 and time.time() < deadline:
        eng.reap()
        eng.schedule()
        time.sleep(0.02)
    for turn in list(eng.turns.values()):
        turn.thread.join(5)
    assert len(runner.calls) == 2
    assert "two" in runner.calls[1]["prompt"] and runner.calls[1]["session"].kind == "resume"
    assert [b["body"] for _r, b in matrix.posted] == ["a1", "a2"]


def test_the_cap_bounds_turns_across_rooms(tmp_path):
    hold = threading.Event()
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["a", "b", "c"], hold=hold)
    eng, slot = make_engine(tmp_path, matrix, runner, max_turns=2)
    for room in ("!a", "!b", "!c"):
        slot.store.add(room, f"$in{room}")
    eng.schedule()
    assert len(eng.turns) == 2
    hold.set()
    for turn in list(eng.turns.values()):
        turn.thread.join(5)
