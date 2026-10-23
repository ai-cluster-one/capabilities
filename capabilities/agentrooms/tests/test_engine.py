"""The service engine's decisions with fakes for the store, the homeserver and
the harness: what wakes an agent, how a turn is built, which session it
resumes, what is posted, and that a room has one turn at a time."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from callva.harness_runner import Profile
from callva.harness_runner import claude as claude_harness
from callva.harness_runner import codex as codex_harness

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
    # An invite wakes nothing: it waits for the operator.
    assert found == {("!group", "$2", "mention"), ("!group", "$3", "mention"),
                     ("!direct", "$5", "direct"), ("!marked", "$6", "marked")}
    assert not any(w["held"] for w in wakes)  # no room here has a grant: everyone
    assert any("!secret" in n for n in notes)


def test_history_before_the_first_sync_and_invites_wake_nothing():
    response = sync(join={"!direct": {"summary": {"m.joined_member_count": 2},
                                      "timeline": {"events": [message("$1", "@b:example.org", "old")]}}},
                    invite={"!invited": {"invite_state": {"events": []}}})
    wakes, _notes = engine.read_sync(response, ME, {}, initial=True, member_count=lambda r: 2)
    assert wakes == []
    wakes, _notes = engine.read_sync(response, ME, {}, initial=False, member_count=lambda r: 2)
    assert [w["kind"] for w in wakes] == ["direct"]


def test_nothing_in_the_directory_wakes_anyone():
    create = {"type": "m.room.create", "state_key": "", "sender": "@b:example.org",
              "content": {"room_version": "12", "type": engine.DIRECTORY_TYPE}}
    card = {"type": "agentrooms.card", "state_key": "@b:example.org", "sender": "@b:example.org",
            "content": {"name": "Bee"}}
    rooms: dict = {}
    asked: list = []

    def room_type(room_id):
        asked.append(room_id)
        return engine.DIRECTORY_TYPE if room_id == "!known" else None

    joined = sync(join={"!dir": {"summary": {"m.joined_member_count": 2},
                                 "state": {"events": [create]},
                                 "timeline": {"events": [
                                     {"type": "m.room.member", "state_key": ME, "sender": ME,
                                      "content": {"membership": "join"}}, card]}}})
    wakes, _ = engine.read_sync(joined, ME, rooms, initial=False, member_count=lambda r: 2,
                                room_type=room_type)
    assert wakes == [] and rooms["!dir"].directory is True
    # A mention, a two-member room and a mark there wake nothing either.
    later = sync(join={
        "!dir": {"account_data": {"events": [{"type": engine.MARK_TYPE,
                                              "content": {"marked": True}}]},
                 "timeline": {"events": [message("$1", "@b:example.org", f"hey {ME}"),
                                         message("$2", "@b:example.org", "hi")]}},
        "!known": {"summary": {"m.joined_member_count": 2},
                   "timeline": {"events": [message("$3", "@b:example.org", f"hey {ME}")]}},
        "!plain": {"summary": {"m.joined_member_count": 2},
                   "timeline": {"events": [message("$4", "@b:example.org", "dm"),
                                           message("$5", "@b:example.org", "dm again")]}},
    })
    wakes, _ = engine.read_sync(later, ME, rooms, initial=False, member_count=lambda r: 2,
                                room_type=room_type)
    assert [(w["room_id"], w["kind"]) for w in wakes] == [("!plain", "direct"), ("!plain", "direct")]
    # A room whose create event no sync brought is asked once, and only it.
    assert sorted(asked) == ["!known", "!plain"]
    # Where it cannot be read, the message is judged as before and asked again next time.
    rooms.clear()
    wakes, _ = engine.read_sync(sync(join={"!plain": {"summary": {"m.joined_member_count": 2},
                                                      "timeline": {"events": [
                                                          message("$6", "@b:example.org", "x")]}}}),
                                ME, rooms, initial=False, member_count=lambda r: 2,
                                room_type=lambda r: engine.UNREAD)
    assert [w["kind"] for w in wakes] == ["direct"] and rooms["!plain"].directory is None


def grant_data(**content):
    return {"account_data": {"events": [{"type": engine.GRANT_TYPE, "content": content}]}}


def test_a_rooms_grant_holds_the_senders_it_does_not_name():
    rooms: dict = {}
    grants: dict = {}
    asked: list = []
    response = sync(join={
        "!granted": {"summary": {"m.joined_member_count": 4},
                     **grant_data(everyone=False, users=["@b:example.org"]),
                     "timeline": {"events": [message("$1", "@b:example.org", f"{ME} hi"),
                                             message("$2", "@c:example.org", f"{ME} me too")]}},
        "!open": {"summary": {"m.joined_member_count": 4},
                  **grant_data(everyone=True, users=[]),
                  "timeline": {"events": [message("$3", "@c:example.org", f"{ME} hello")]}},
        "!unread": {"summary": {"m.joined_member_count": 2},
                    "timeline": {"events": [message("$4", "@c:example.org", "dm"),
                                            message("$5", "@c:example.org", "dm again")]}},
        "!older": {"summary": {"m.joined_member_count": 2},
                   "timeline": {"events": [message("$6", "@c:example.org", "dm")]}},
    })

    def grant_of(room_id):
        asked.append(room_id)
        return {"everyone": False, "users": []} if room_id == "!unread" else None

    wakes, _notes = engine.read_sync(response, ME, rooms, initial=False,
                                     member_count=lambda r: None, grant_of=grant_of,
                                     grants=grants)
    held = {w["event_id"]: w["held"] for w in wakes}
    assert held == {"$1": False, "$2": True, "$3": False, "$4": True, "$5": True, "$6": False}
    # A grant the sync brought is reported; a room whose grant was never seen is
    # asked once - a room with none reads as everyone, never one not yet read.
    assert grants == {"!granted": {"everyone": False, "users": ["@b:example.org"]},
                      "!open": {"everyone": True, "users": []}}
    assert sorted(asked) == ["!older", "!unread"]
    more = sync(join={"!unread": {"timeline": {"events": [message("$7", "@c:example.org", "x")]}}})
    wakes, _ = engine.read_sync(more, ME, rooms, initial=False, member_count=lambda r: None,
                                grant_of=grant_of)
    assert [w["held"] for w in wakes] == [True] and sorted(asked) == ["!older", "!unread"]
    # A record that names nobody admits nobody; a malformed one is read the same.
    assert engine.read_grant({}) == {"everyone": False, "users": []}
    assert engine.read_grant("x") == {"everyone": False, "users": []}
    assert not engine.granted(engine.read_grant({}), "@b:example.org")
    assert engine.granted(None, "@anyone:example.org")


def test_prompt_marks_what_woke_the_agent():
    agent = {"user_id": ME, "connection": "alpha"}
    events = [message("$1", "@b:example.org", "first"),
              message("$2", "@b:example.org", "second", **{"m.relates_to": {
                  "rel_type": "m.thread", "event_id": "$1"}})]
    rules = engine.room_rules(TEMPLATE, agent=agent, room_id="!r", room_name="lab")
    assert rules == f"You are {ME} in lab; stay quiet with [silent]; use --connection alpha."
    prompt = engine.build_prompt(room_id="!r", room_name="lab",
                                 wakes=[{"kind": "mention", "event_id": "$2"}], events=events,
                                 primed=True)
    assert prompt.startswith("=== This turn ===\n") and "You are" not in prompt
    assert "=== Room history ===" in prompt
    assert "in thread $1, WOKE YOU" in prompt


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


WORKER = {"description": "Answers in rooms.", "accepts_from": ["@b:example.org"],
          "profile": "agentrooms-claude"}


class FakeStore:
    def __init__(self, workers=None):
        self.wakes: list[dict] = []
        self.sessions: dict = {}
        self.lock = threading.Lock()
        self.workers = {"w": (WORKER, "")} if workers is None else workers
        self.turn_log: list[dict] = []

    def add(self, room, event_id, kind="mention", sender="@b:example.org", received_at=None):
        with self.lock:
            self.wakes.append({"agent": "alpha", "room_id": room, "key": event_id or "invite",
                               "kind": kind, "event_id": event_id, "sender": sender,
                               "received_at": received_at or time.time(), "claimed_by": None})

    def worker(self, project, name, version=None):
        if name not in self.workers:
            return None
        spec, body = self.workers[name]
        return {"id": f"v-{name}", "name": name, "spec": spec, "body": body, "active": True}

    def start_turn(self, turn, project, agent, room, worker, worker_id, senders, kinds):
        self.turn_log.append({"id": turn, "room": room, "worker": worker, "senders": senders,
                              "kinds": kinds, "at": time.time(), "outcome": None,
                              "seen_ts": None, "taken": []})

    def row(self, turn):
        return next(r for r in self.turn_log if r["id"] == turn)

    def mark_turn(self, turn, seen_ts):
        row = self.row(turn)
        row["seen_ts"] = max(row["seen_ts"] or 0, seen_ts or 0)

    def turn_seen(self, turn):
        return self.row(turn)["seen_ts"]

    def catch_up(self, turn, events, admitted):
        """What `Store.catch_up` does, for the engine's side of it."""
        row = self.row(turn)
        ids = [e["event_id"] for e in events]
        with self.lock:
            took = [w for w in self.wakes if w["claimed_by"] is None and w["event_id"] in ids
                    and admitted(w)]
            for w in took:
                w["claimed_by"] = turn
        row["seen_ts"] = max([row["seen_ts"] or 0] + [e["origin_server_ts"] for e in events])
        row["taken"] += ids
        row["senders"] = sorted(set(row["senders"]) | {w["sender"] for w in took})
        return took

    def untake(self, project, agent, room, turn):
        row = self.row(turn)
        self.restore(project, agent, room, turn, row["taken"])
        row["taken"] = []

    def taken_before(self, project, agent, room, wakes):
        out = []
        for wake in wakes:
            rows = [r for r in self.turn_log if r["room"] == room and wake["event_id"] in r["taken"]]
            if rows:
                rows[-1]["senders"] = sorted(set(rows[-1]["senders"]) | {wake["sender"]})
                out.append({**wake, "taken_by": rows[-1]["id"]})
        return out

    def end_turn(self, turn, outcome, *, event_id=None, harness=None, session_id=None, node=None):
        for row in self.turn_log:
            if row["id"] == turn:
                row.update(outcome=outcome, event_id=event_id, harness=harness,
                           session_id=session_id, node=node)

    def turn_counts(self, project, agent, room, senders, *, window, quiet, room_cap):
        ran = [r for r in self.turn_log if r["worker"] != engine.REFUSAL]
        room_n, since = 0, time.time()
        for row in sorted((r for r in ran if r["room"] == room), key=lambda r: -r["at"]):
            if since - row["at"] >= quiet:
                break
            room_n, since = room_n + 1, row["at"]
        return {"agent": len(ran), "room": room_n,
                "senders": {s: sum(1 for r in ran if s in r["senders"]) for s in senders}}

    def refused(self, project, agent, room, sender, window):
        return any(r["worker"] == engine.REFUSAL and r["room"] == room and sender in r["senders"]
                   for r in self.turn_log)

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

    def restore(self, project, agent, room, turn, keys=None):
        with self.lock:
            for w in self.wakes:
                if w["claimed_by"] == turn and (keys is None or w["key"] in keys):
                    w["claimed_by"] = None

    def session(self, project, agent, room):
        return self.sessions.get(room)

    def save_session(self, project, agent, room, *, harness, session_id, node, seen_ts):
        self.sessions[room] = {"harness": harness, "session_id": session_id, "node": node,
                               "seen_ts": seen_ts, "turns": 1}


class NotFound(Exception):
    exit_code = 3


class FakeMatrix:
    def __init__(self, room_events, grant=None):
        self.room_events = room_events
        self.posted: list[tuple[str, dict]] = []
        self.left: list[tuple[str, dict]] = []
        # Each room's switch, and the grant every room records (None: none).
        self.served: dict[str, dict] = {}
        self.grant = {"everyone": True, "users": []} if grant is None else grant

    def call(self, method, path, params=None, body=None):
        from urllib.parse import unquote
        if f"/account_data/{engine.SERVED_TYPE}" in path:
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            if room not in self.served:
                raise NotFound("no switch")
            return self.served[room]
        if f"/account_data/{engine.GRANT_TYPE}" in path:
            if not self.grant:
                raise NotFound("no grant")
            return self.grant
        if path.endswith("/leave"):
            self.left.append((unquote(path.split("/rooms/")[1].split("/")[0]), body))
            return {}
        if "/event/" in path:
            wanted = unquote(path.rsplit("/", 1)[1])
            return next((e for e in self.room_events if e["event_id"] == wanted), {})
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
        self.calls.append({"prompt": prompt, "profile": profile, "session": session, "cwd": cwd,
                           "extra_env": extra_env})
        if self.hold is not None:
            self.hold.wait(5)
        answer = self.answers.pop(0)
        if isinstance(answer, SimpleNamespace):
            return answer
        sid = session.id or f"sess{len(self.calls)}"
        return SimpleNamespace(ok=True, answer=answer, session_id=sid, failure=None)


class FakeHost:
    def __init__(self, matrix, runner, refusal=None, template=TEMPLATE, profile=None,
                 harness="claude", binding=None):
        self._matrix, self._runner, self.refusal = matrix, runner, refusal
        self._template = template
        self._profile = profile or CLAUDE_PROFILE
        self._harness = harness
        self._binding = {"worker": "w"} if binding is None else binding
        self.profiles_asked: list[str] = []

    def binding(self, entry, cid):
        return dict(self._binding)

    def profile(self, entry, store, name):
        self.profiles_asked.append(name)
        return self._profile, self._harness, {"name": name}

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
        return self._template

    def turn_env(self, entry):
        return {}


CLAUDE_PROFILE = Profile(harness="claude", model="opus", permission_mode="bypassPermissions")
RULES = f"You are {ME} in lab; stay quiet with [silent]; use --connection alpha."


def make_engine(tmp_path, matrix, runner, refusal=None, max_turns=2, profile=CLAUDE_PROFILE,
                harness="claude", host=None, workers=None, binding=None):
    host = host or FakeHost(matrix, runner, refusal, profile=profile, harness=harness,
                            binding=binding)
    eng = engine.Engine(host, mode="project", state_dir=tmp_path, echo=False)
    eng.settings = {"max_turns": max_turns}
    slot = engine.Slot(eng, "prj", {"id": "prj", "root": str(tmp_path), "slug": "project"})
    slot.store = FakeStore(workers)
    slot.state = "served"
    slot.plan = {"agents": {"alpha": {
        "connection": "alpha", "homeserver": "https://matrix.example.org", "user_id": ME,
        "token": "t"}}}
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
    turn_id = runner.calls[0]["extra_env"]["AGENTROOMS_TURN"]
    assert runner.calls[0]["extra_env"] == {"CLAUDE_PROJECT_DIR": str(tmp_path),
                                            "AGENTROOMS_TURN": turn_id}
    assert slot.store.turn_log[0]["id"] == turn_id and slot.store.turn_log[0]["worker"] == "w"
    assert slot.store.turn_log[0]["outcome"] == "answered"
    # The turn names the message it posted and the harness session that wrote it.
    assert {k: slot.store.turn_log[0][k] for k in ("event_id", "harness", "session_id")} == \
        {"event_id": "$posted1", "harness": "claude", "session_id": "sess1"}
    assert slot.store.turn_log[0]["node"] == eng.node
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


def test_the_room_rules_ride_in_the_system_prompt_of_every_turn(tmp_path):
    events = [message("$1", "@b:example.org", "hello"),
              message("$2", "@b:example.org", f"{ME} question")]
    matrix = FakeMatrix(events)
    runner = FakeRunner(["first answer", "second answer"])
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.add("!r", "$2")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    first = runner.calls[0]
    assert first["session"].kind == "fresh"
    assert first["profile"].append_system_prompt == RULES
    assert first["profile"] == engine.dataclasses.replace(CLAUDE_PROFILE, append_system_prompt=RULES)
    assert first["prompt"].startswith("=== This turn ===\n")
    assert "You are" not in first["prompt"] and "stay quiet" not in first["prompt"]
    assert "=== Room history ===" in first["prompt"] and "hello" in first["prompt"]
    # A resumed turn carries the same rules in its system prompt, and its message
    # only the turn block and what is new.
    events.append({**message("$3", "@b:example.org", f"{ME} again"),
                   "origin_server_ts": 1_800_000_000_000})
    slot.store.add("!r", "$3")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    second = runner.calls[1]
    assert second["session"].kind == "resume"
    assert second["profile"].append_system_prompt == RULES
    assert second["prompt"] == engine.build_prompt(
        room_id="!r", room_name="lab", wakes=[{"kind": "mention", "event_id": "$3"}],
        events=[events[2]], primed=False)
    assert second["prompt"].startswith("=== This turn ===\n")
    assert "You are" not in second["prompt"] and "hello" not in second["prompt"]
    assert "=== New in the room since your last turn ===" in second["prompt"]


def test_a_profiles_own_system_prompt_append_comes_first(tmp_path):
    claude = Profile(harness="claude", permission_mode="bypassPermissions",
                     append_system_prompt="Claude profile text.")
    codex = Profile(harness="codex", approval_policy="never",
                    codex_config={"sandbox_mode": "danger-full-access"},
                    append_system_prompt="Codex profile text.")
    for profile, own in ((claude, "Claude profile text."), (codex, "Codex profile text.")):
        matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
        runner = FakeRunner(["answer"])
        eng, slot = make_engine(tmp_path, matrix, runner, profile=profile,
                                harness=profile.harness)
        slot.store.add("!r", "$1")
        eng.turn(engine.Turn(slot, "alpha", "!r"))
        sent = runner.calls[0]["profile"]
        assert sent.append_system_prompt == f"{own}\n\n{RULES}"
        assert sent == engine.dataclasses.replace(profile, append_system_prompt=f"{own}\n\n{RULES}")
        assert "You are" not in runner.calls[0]["prompt"]
    # What each harness is handed: claude's system-prompt append, codex's
    # developer instructions.
    sent_claude = engine.with_rules(claude, RULES)
    options = claude_harness.options(sent_claude, cli_path="claude", cwd=str(tmp_path), env={},
                                     session_id=None, resume_id=None, stderr=lambda line: None)
    assert options.system_prompt == {"type": "preset", "preset": "claude_code",
                                     "append": f"Claude profile text.\n\n{RULES}"}
    sent_codex = engine.with_rules(codex, RULES)
    assert codex_harness.thread_options(sent_codex, cwd=str(tmp_path))[
        "developer_instructions"] == f"Codex profile text.\n\n{RULES}"


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


class CatchingUpRunner(FakeRunner):
    """A harness whose first turn runs `catch-up` midway: `during(turn_id)`
    posts and takes what it wants, then the turn ends as `ended` says."""

    def __init__(self, answers, during):
        super().__init__(answers)
        self.during = during

    def run(self, prompt, profile, cwd, *, session, environ, extra_env, cancel):
        if not self.calls:
            self.during(extra_env["AGENTROOMS_TURN"])
        return super().run(prompt, profile, cwd, session=session, environ=environ,
                           extra_env=extra_env, cancel=cancel)


def later(event_id, sender, body, n):
    return {**message(event_id, sender, body), "origin_server_ts": 1_700_000_000_000 + n}


def test_what_a_turn_took_with_catch_up_is_neither_new_nor_a_wake_next_turn(tmp_path):
    events = [message("$1", "@b:example.org", f"{ME} one")]
    matrix = FakeMatrix(events)

    def during(turn_id):
        # Mid-turn: b mentions the agent twice and a stranger once; the agent
        # catches up after the first wake and the stranger's are recorded, and
        # before the second's.
        arrived = [later("$2", "@b:example.org", f"{ME} two", 2),
                   later("$3", STRANGER, f"{ME} me too", 3),
                   later("$4", "@b:example.org", f"{ME} four", 4)]
        events.extend(arrived)
        slot.store.add("!r", "$2")
        slot.store.add("!r", "$3", sender=STRANGER)
        mark = slot.store.turn_seen(turn_id)
        new = [e for e in events if e["origin_server_ts"] > mark]
        took = slot.store.catch_up(turn_id, new, engine.turn_admits(
            slot.store, "prj", {"worker": "w", "worker_id": "v-w"}))
        assert [w["event_id"] for w in took] == ["$2"]
        slot.store.add("!r", "$4")

    runner = CatchingUpRunner(["a1", "a5"], during)
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    first = slot.store.turn_log[0]
    assert first["senders"] == ["@b:example.org"]
    # The next pass starts no turn for the wakes b's messages left, the one
    # recorded after catch-up ran included; the stranger gets the refusal.
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1 and slot.store.wakes == []
    assert [b["body"] for _r, b in matrix.posted][0] == "a1"
    assert "the person who runs it" in matrix.posted[1][1]["body"]
    assert "starts no turn" in (tmp_path / engine.LOG_FILE).read_text()
    # The limits count b on the turn that took its wakes.
    assert first["senders"] == ["@b:example.org"]
    # What arrives next is all the next turn carries as new.
    events.append(later("$5", "@b:example.org", f"{ME} five", 5))
    slot.store.add("!r", "$5")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    prompt = runner.calls[1]["prompt"]
    assert "five" in prompt and "two" not in prompt and "four" not in prompt
    assert "me too" not in prompt


def test_a_turn_whose_harness_did_not_run_took_nothing(tmp_path):
    events = [message("$1", "@b:example.org", f"{ME} one")]
    matrix = FakeMatrix(events)

    def during(turn_id):
        events.append(later("$2", "@b:example.org", f"{ME} two", 2))
        slot.store.add("!r", "$2")
        slot.store.catch_up(turn_id, [events[-1]], lambda wake: True)

    never = SimpleNamespace(ok=False, answer="", session_id="pinned", stderr_tail="",
                            failure=SimpleNamespace(kind="not_authenticated", message="login"))
    runner = CatchingUpRunner([never, "a2"], during)
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert slot.store.sessions == {}
    assert [(w["event_id"], w["claimed_by"]) for w in slot.store.wakes] == [("$2", None)]
    assert slot.store.turn_log[0]["taken"] == []
    # Its wake starts the next turn, which carries it.
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 2 and "two" in runner.calls[1]["prompt"]


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


# ── a message posted between an invite and the join ──────────────────────────
#
# The shapes below are what the homeserver answered an invitee in a recorded
# run, ids anonymized: while the agent is invited, a sync brings the invite and
# then nothing of the room; the first sync after the join holds the room with a
# timeline limited to the agent's own join, and what was posted while it was
# invited lies behind the timeline's prev_batch.

INVITER = "@inviter:example.org"
ROOM = "!pre-join:example.org"


def member(event_id, state_key, membership, sender, ts, prev=None):
    event = {"type": "m.room.member", "event_id": event_id, "sender": sender,
             "state_key": state_key, "origin_server_ts": ts,
             "content": {"membership": membership}, "unsigned": {}}
    if prev:
        event["unsigned"]["prev_content"] = {"membership": prev}
    return event


def said(event_id, body, ts, sender=INVITER, **content):
    return {**message(event_id, sender, body, **content), "origin_server_ts": ts}


INVITE_SYNC = {"next_batch": "s1", "rooms": {"invite": {ROOM: {"invite_state": {"events": [
    {"type": "m.room.create", "state_key": "", "sender": INVITER, "content": {}},
    {"type": "m.room.member", "state_key": ME, "sender": INVITER,
     "content": {"membership": "invite"}}]}}}}}
QUIET_SYNC = {"next_batch": "s2"}
JOIN_SYNC = {"next_batch": "s3", "rooms": {"join": {ROOM: {
    "summary": {"m.heroes": [INVITER], "m.joined_member_count": 2, "m.invited_member_count": 0},
    "state": {"events": [
        {"type": "m.room.create", "state_key": "", "sender": INVITER, "content": {}},
        {"type": "m.room.history_visibility", "state_key": "", "sender": INVITER,
         "content": {"history_visibility": "shared"}},
        member("$inv", ME, "invite", INVITER, 1_000),
        member("$creator", INVITER, "join", INVITER, 100)]},
    "timeline": {"events": [member("$join", ME, "join", ME, 8_000, prev="invite")],
                 "limited": True, "prev_batch": "p646"},
    "account_data": {"events": []}, "ephemeral": {"events": []}}}}}
NEXT_SYNC = {"next_batch": "s4"}


class SyncMatrix:
    """Answers the agent's syncs from a list, and its reads of the room from
    pages keyed by their `from` token, as the homeserver did."""

    def __init__(self, syncs, pages, grant=None, served=None):
        self.syncs, self.pages = list(syncs), pages
        self.reads: list[dict] = []
        self.grant_reads: list[str] = []
        self.served_reads: list[str] = []
        self.grant = grant
        self.served = served

    def call(self, method, path, params=None, body=None):
        if path == "/_matrix/client/v3/sync":
            return self.syncs.pop(0)
        if path.endswith("/messages"):
            self.reads.append(dict(params))
            return self.pages[params["from"]]
        if path.endswith("/joined_members"):
            return {"joined": {INVITER: {}, ME: {}}}
        if path.endswith(f"/account_data/{engine.GRANT_TYPE}"):
            self.grant_reads.append(path)
            if self.grant is None:
                raise NotFound("no grant")
            return self.grant
        if path.endswith(f"/account_data/{engine.SERVED_TYPE}"):
            self.served_reads.append(path)
            if self.served is None:
                raise NotFound("no switch")
            return self.served
        raise AssertionError(path)


class SyncStore:
    def __init__(self):
        self.position = None
        self.wakes: dict = {}
        self.grants: list = []
        self.unserved: list = []

    def next_batch(self, project, agent, user_id):
        return self.position

    def save_sync(self, project, agent, user_id, holder, next_batch, wakes, grants=None,
                  unserved=None):
        for room_id in unserved or ():  # what no turn claimed in a room switched off
            self.wakes = {k: w for k, w in self.wakes.items()
                          if k[0] != room_id or w.get("held")}
        for wake in wakes:  # the table's key, ON CONFLICT DO NOTHING
            self.wakes.setdefault((wake["room_id"], wake["key"]), wake)
        self.grants.append(dict(grants or {}))
        self.unserved.append(sorted(unserved or ()))
        self.position = next_batch
        return True

    def execute(self, *args, **kwargs):
        return 1


def serve(tmp_path, pages, syncs=None):
    eng, slot = make_engine(tmp_path, None, FakeRunner([]))
    slot.store = SyncStore()
    agent = engine.Agent(slot, slot.plan["agents"]["alpha"])
    matrix = SyncMatrix(syncs or [{"next_batch": "s0"}, INVITE_SYNC, QUIET_SYNC, JOIN_SYNC,
                                  NEXT_SYNC], pages)
    seen = []
    for _ in range(len(matrix.syncs)):
        agent.sync_once(matrix)
        seen.append(sorted((k[1], w["kind"]) for k, w in slot.store.wakes.items()))
    return seen, matrix


def test_a_mention_posted_before_the_join_wakes_the_agent_once_it_joins(tmp_path):
    mention = said("$mention", f"{ME} are you there?", 6_000, **{"m.mentions": {"user_ids": [ME]}})
    pages = {"p646": {"start": "p646", "end": "p636", "chunk": [
        mention, member("$inv", ME, "invite", INVITER, 1_000),
        member("$creator", INVITER, "join", INVITER, 100)]}}
    seen, matrix = serve(tmp_path, pages)
    # Nothing until the join - the invite waits for the operator; the mention
    # once the join is seen, and nothing new after it.
    assert seen == [[], [], [], [("$mention", "mention")], [("$mention", "mention")]]
    assert [r["from"] for r in matrix.reads] == ["p646"] and matrix.reads[0]["dir"] == "b"


def test_history_from_before_the_invite_wakes_nothing_after_the_join(tmp_path):
    pages = {"p646": {"start": "p646", "end": "p640", "chunk": [
        said("$after", f"{ME} now that you are invited", 6_000),
        member("$inv", ME, "invite", INVITER, 5_000),
        said("$old-mention", f"{ME} said before the invite", 3_000),
        said("$old", "chatter before the invite", 2_000)]}}
    seen, _matrix = serve(tmp_path, pages)
    assert seen[3] == [("$after", "mention")]
    # The invite not found behind the timeline (another membership of the
    # agent's own comes first): nothing from before the join wakes.
    rejoined = {"p646": {"start": "p646", "end": "p640", "chunk": [
        said("$x", f"{ME} while you were away", 6_000),
        member("$left", ME, "leave", ME, 5_000)]}}
    seen, _matrix = serve(tmp_path / "rejoined", rejoined)
    assert seen[3] == []


def test_a_pre_join_mention_also_in_the_timeline_wakes_once(tmp_path):
    mention = said("$mention", f"hey {ME}", 6_000)
    join = JOIN_SYNC["rooms"]["join"][ROOM]
    unlimited = {"next_batch": "s3", "rooms": {"join": {ROOM: {
        **join, "timeline": {"events": [member("$inv2", ME, "invite", INVITER, 1_000), mention,
                                        member("$join", ME, "join", ME, 8_000, prev="invite")],
                             "limited": False, "prev_batch": "p600"}}}}}
    seen, matrix = serve(tmp_path, {}, [{"next_batch": "s0"}, INVITE_SYNC, QUIET_SYNC,
                                        unlimited, NEXT_SYNC])
    assert seen[3] == [("$mention", "mention")] and matrix.reads == []
    limited_overlap = {"next_batch": "s3", "rooms": {"join": {ROOM: {
        **join, "timeline": {"events": [mention,
                                        member("$join", ME, "join", ME, 8_000, prev="invite")],
                             "limited": True, "prev_batch": "p646"}}}}}
    pages = {"p646": {"start": "p646", "end": "p636", "chunk": [
        mention, member("$inv", ME, "invite", INVITER, 1_000)]}}
    reads = []
    wakes, _notes = engine.read_sync(limited_overlap, ME, {}, initial=False,
                                     member_count=lambda r: 2,
                                     before_join=lambda room, token: engine.since_invite(
                                         lambda t: reads.append(t) or pages[t], token, ME))
    assert [(w["event_id"], w["kind"]) for w in wakes] == [("$mention", "mention")]


def test_a_profile_change_reads_nothing_back(tmp_path):
    renamed = {"next_batch": "s9", "rooms": {"join": {ROOM: {
        "summary": {"m.joined_member_count": 3},
        "timeline": {"events": [member("$rename", ME, "join", ME, 9_000, prev="join")],
                     "limited": True, "prev_batch": "p700"}}}}}
    asked = []
    wakes, _notes = engine.read_sync(renamed, ME, {}, initial=False, member_count=lambda r: 3,
                                     before_join=lambda room, token: asked.append(token) or [])
    assert wakes == [] and asked == []


def test_a_restarted_agent_reads_a_rooms_grant_before_it_judges_a_wake(tmp_path):
    eng, slot = make_engine(tmp_path, None, FakeRunner([]))
    slot.store = SyncStore()
    slot.store.position = "s0"  # served before: account data is not sent again
    agent = engine.Agent(slot, slot.plan["agents"]["alpha"])
    stranger = "@c:example.org"
    direct = {"next_batch": "s1", "rooms": {"join": {ROOM: {
        "summary": {"m.joined_member_count": 2},
        "timeline": {"events": [said("$dm", "hello", 9_000, sender=stranger)]}}}}}
    regrant = {"next_batch": "s2", "rooms": {"join": {ROOM: grant_data(
        everyone=False, users=[INVITER, stranger])}}}
    matrix = SyncMatrix([direct, regrant], {}, grant={"everyone": False, "users": [INVITER]})
    agent.sync_once(matrix)
    agent.sync_once(matrix)
    # The grant was asked once, held the stranger, and the grant that names him
    # reached the store, which releases what it held.
    assert slot.store.wakes[(ROOM, "$dm")]["held"] is True
    assert len(matrix.grant_reads) == 1
    assert slot.store.grants == [{}, {ROOM: {"everyone": False,
                                             "users": sorted([INVITER, stranger])}}]
    log = (tmp_path / engine.LOG_FILE).read_text()
    assert "held: the room's grant does not name the sender" in log and "the grant changed" in log


# ── workers: who is admitted, what the refusal answers, the limits ───────────

STRANGER = "@stranger:example.org"


def test_worker_documents_are_checked_whole():
    spec, body = engine.split_document(
        "---\naccepts_from: ['@*:example.org']\nlimits:\n  debounce_seconds: 5\n---\nBe brief.\n",
        "w.md")
    worker = engine.read_worker("w", spec, body, "w.md")
    assert worker["enabled"] and worker["profile"] == "agentrooms-claude"
    assert worker["invites_from"] == ["@*:example.org"] and worker["wakes_on"] == list(engine.WAKE_KINDS)
    assert worker["limits"]["debounce_seconds"] == 5 and worker["limits"]["turns_per_room"] == 30
    assert worker["body"] == "Be brief." and worker["history"] == 30
    assert engine.read_worker("off", {"enabled": False}, "", "off")["enabled"] is False
    for bad, said in (({"accepts_from": ["*"], "nope": 1}, "'nope'"),
                      ({}, "admits nobody"),
                      ({"accepts_from": "*"}, "accepts_from"),
                      ({"accepts_from": ["*"], "wakes_on": ["shout"]}, "wakes_on"),
                      ({"accepts_from": ["*"], "limits": {"turn_timeout": 5}}, "turn_timeout"),
                      ({"accepts_from": ["*"], "limits": {"later": 1}}, "'later'"),
                      ({"accepts_from": ["*"], "hooks": {"during": "x"}}, "'during'"),
                      ({"accepts_from": ["*"], "hooks": {"before": "'open"}}, "hooks.before")):
        with pytest.raises(engine.WorkerInvalid, match=said):
            engine.read_worker("w", bad, "", "w")
    with pytest.raises(engine.WorkerInvalid):
        engine.split_document("no front matter", "x")
    text = engine.worker_text({"accepts_from": ["*"]}, "Role.")
    assert engine.split_document(text, "t") == ({"accepts_from": ["*"]}, "Role.")
    refusal = engine.load_refusal()
    assert refusal["limits"]["window_seconds"] == 86400 and "$supervisor" in refusal["body"]
    # The shipped default is a whole worker that answers whoever the grant admits.
    default = engine.load_default()
    assert default["enabled"] and default["accepts_from"] == ["*"]
    assert default["profile"] == "agentrooms-claude" and default["history"] == 30
    assert default["limits"]["turn_timeout"] == 900 and default["body"] == ""


def test_a_default_answer_never_wakes_an_agent():
    marked = message("$1", "@other:example.org", f"{ME} not yet",
                     **{engine.DATA_KEY: dict(engine.DEFAULT_MARK)})
    plain = message("$2", "@other:example.org", f"{ME} hi")
    wakes, _ = engine.read_sync(sync(join={"!r": {"summary": {"m.joined_member_count": 2},
                                                  "timeline": {"events": [marked, plain]}}}),
                                ME, {}, initial=False, member_count=lambda r: 2)
    assert [w["event_id"] for w in wakes] == ["$2"]


def test_an_agent_with_no_worker_is_answered_by_the_shipped_default(tmp_path):
    matrix = FakeMatrix([message("$1", STRANGER, f"{ME} hello")])
    runner = FakeRunner(["hello back"])
    eng, slot = make_engine(tmp_path, matrix, runner, workers={}, binding={})
    slot.store.add("!r", "$1", sender=STRANGER)
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1 and eng.host.profiles_asked == ["agentrooms-claude"]
    assert matrix.posted[0][1]["body"] == "hello back"
    assert engine.DATA_KEY not in matrix.posted[0][1]
    assert slot.store.turn_log[0]["worker"] == "default"
    assert slot.store.turn_log[0]["outcome"] == "answered"


def test_a_project_saved_default_replaces_the_shipped_one(tmp_path):
    matrix = FakeMatrix([message("$1", STRANGER, f"{ME} hello")])
    runner = FakeRunner(["from the project's default"])
    workers = {"default": ({**WORKER, "accepts_from": ["*"], "profile": "agentrooms-codex"},
                           "Project role.")}
    eng, slot = make_engine(tmp_path, matrix, runner, workers=workers, binding={})
    slot.store.add("!r", "$1", sender=STRANGER)
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert eng.host.profiles_asked == ["agentrooms-codex"]
    assert runner.calls[0]["profile"].append_system_prompt.endswith("Project role.")
    # Saved switched off, it answers nobody: the refusal answers the sender.
    workers["default"] = ({"enabled": False}, "")
    slot.store.add("!r", "$2", sender="@other:example.org")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1 and matrix.posted[-1][1][engine.DATA_KEY] == engine.DEFAULT_MARK
    assert "the project's worker 'default' is switched off" in (
        tmp_path / engine.LOG_FILE).read_text()


def test_a_room_without_a_recorded_grant_gets_only_the_refusal_once(tmp_path):
    events = [message("$1", STRANGER, f"{ME} hello"), message("$2", STRANGER, f"{ME} again")]
    matrix = FakeMatrix(events, grant={})
    runner = FakeRunner([])
    eng, slot = make_engine(tmp_path, matrix, runner, workers={},
                            binding={"supervisor": "@boss:example.org"})
    slot.store.add("!r", "$1", sender=STRANGER)
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls == []
    room, content = matrix.posted[0]
    assert room == "!r" and content[engine.DATA_KEY] == engine.DEFAULT_MARK
    assert ME in content["body"] and STRANGER in content["body"]
    assert "its supervisor, @boss:example.org," in content["body"]
    assert slot.store.wakes == []
    slot.store.add("!r", "$2", sender=STRANGER)
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(matrix.posted) == 1 and slot.store.wakes == []
    log = (tmp_path / engine.LOG_FILE).read_text()
    assert "the room has no recorded grant" in log and "already answered" in log
    assert [r["worker"] for r in slot.store.turn_log] == [engine.REFUSAL]


def test_a_worker_admits_its_senders_and_the_refusal_answers_the_rest(tmp_path):
    events = [message("$1", "@b:example.org", f"{ME} hi"), message("$2", STRANGER, f"{ME} me too")]
    matrix = FakeMatrix(events)
    runner = FakeRunner(["for b"])
    eng, slot = make_engine(tmp_path, matrix, runner)
    slot.store.add("!r", "$1")
    slot.store.add("!r", "$2", sender=STRANGER)
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1 and "WOKE YOU" in runner.calls[0]["prompt"]
    bodies = [b for _r, b in matrix.posted]
    assert bodies[0][engine.DATA_KEY] == engine.DEFAULT_MARK and "the person who runs it" in bodies[0]["body"]
    assert bodies[1]["body"] == "for b"
    assert slot.store.turn_log[-1]["senders"] == ["@b:example.org"]
    assert f"worker 'w' does not admit {STRANGER}" in (tmp_path / engine.LOG_FILE).read_text()


def test_an_invite_recorded_before_invites_waited_is_dropped_and_never_declined(tmp_path):
    matrix = FakeMatrix([])
    runner = FakeRunner([])
    eng, slot = make_engine(tmp_path, matrix, runner, binding={})
    slot.store.add("!inv", None, kind="invite", sender=STRANGER)
    eng.turn(engine.Turn(slot, "alpha", "!inv"))
    assert runner.calls == [] and matrix.posted == [] and matrix.left == []
    assert slot.store.wakes == [] and slot.store.turn_log == []
    assert "an invite waits for the operator" in (tmp_path / engine.LOG_FILE).read_text()


def test_what_a_worker_does_not_wake_on_and_strangers_in_marked_rooms_are_only_logged(tmp_path):
    matrix = FakeMatrix([message("$1", STRANGER, "chatter"), message("$2", "@b:example.org", "x")])
    runner = FakeRunner([])
    workers = {"w": ({**WORKER, "wakes_on": ["mention", "direct"]}, "")}
    eng, slot = make_engine(tmp_path, matrix, runner, workers=workers)
    slot.store.add("!m", "$1", kind="marked", sender=STRANGER)
    slot.store.add("!m", "$2", kind="marked")
    eng.turn(engine.Turn(slot, "alpha", "!m"))
    assert runner.calls == [] and matrix.posted == [] and slot.store.wakes == []
    log = (tmp_path / engine.LOG_FILE).read_text()
    assert "does not wake on marked" in log


def test_a_bound_worker_switched_off_or_missing_leaves_the_refusal(tmp_path):
    for workers, said in (({"w": ({"enabled": False}, "")}, "is switched off"),
                          ({}, "has no active version")):
        matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
        runner = FakeRunner([])
        (tmp_path / said[:5]).mkdir()
        eng, slot = make_engine(tmp_path / said[:5], matrix, runner, workers=workers)
        slot.store.add("!r", "$1")
        eng.turn(engine.Turn(slot, "alpha", "!r"))
        assert runner.calls == [] and matrix.posted[0][1][engine.DATA_KEY] == engine.DEFAULT_MARK
        assert said in (tmp_path / said[:5] / engine.LOG_FILE).read_text()


def test_a_room_binding_overrides_the_agents_and_the_role_rides_in_the_frame(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["ok"])
    workers = {"w": (WORKER, "General role."),
               "lab": ({**WORKER, "profile": "agentrooms-codex"}, "Lab role.")}
    eng, slot = make_engine(tmp_path, matrix, runner, workers=workers,
                            binding={"worker": "w", "rooms": {"!r": "lab"}})
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert eng.host.profiles_asked == ["agentrooms-codex"]
    assert runner.calls[0]["profile"].append_system_prompt == f"{RULES}\n\n## Your role\n\nLab role."
    assert "Lab role" not in runner.calls[0]["prompt"]


def test_limits_refuse_a_room_until_it_has_been_quiet_and_a_sender_over_its_count(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["1", "2", "3"])
    workers = {"w": ({**WORKER, "accepts_from": ["*"],
                      "limits": {"turns_per_room": 2, "turns_per_sender": 5}}, "")}
    eng, slot = make_engine(tmp_path, matrix, runner, workers=workers)
    for n in range(3):
        slot.store.add("!r", f"$r{n}")
        eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 2 and slot.store.wakes == []
    assert "its turns_per_room" in (tmp_path / engine.LOG_FILE).read_text()
    for row in slot.store.turn_log:  # the room has been quiet long enough
        row["at"] -= 901
    slot.store.add("!r", "$r3")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 3
    workers["w"] = ({**WORKER, "accepts_from": ["*"], "limits": {"turns_per_sender": 3}}, "")
    slot.store.add("!s", "$s1")
    slot.store.add("!s", "$s2", sender=STRANGER)
    runner.answers.append("for the stranger")
    eng.turn(engine.Turn(slot, "alpha", "!s"))
    assert "@b:example.org in !s refused" in (tmp_path / engine.LOG_FILE).read_text()
    assert slot.store.turn_log[-1]["senders"] == [STRANGER]


def test_debounce_hands_the_wakes_back_until_the_room_settles(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["ok"])
    workers = {"w": ({**WORKER, "limits": {"debounce_seconds": 30}}, "")}
    eng, slot = make_engine(tmp_path, matrix, runner, workers=workers)
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls == [] and slot.store.wakes[0]["claimed_by"] is None
    assert eng.held("prj", "alpha", "!r") and not eng.held("prj", "alpha", "!other")
    eng.schedule()
    assert eng.turns == {}
    slot.store.wakes[0]["received_at"] -= 31
    eng.holds.clear()
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1


def hook(tmp_path, name, code, said=""):
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\ncat > {tmp_path}/{name}.stdin\nenv > {tmp_path}/{name}.env\n"
                    f"echo '{said}'\nexit {code}\n")
    path.chmod(0o755)
    return str(path)


def test_hooks_hold_escalate_and_follow_a_turn(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["ok", "ok"])
    later = "2999-01-01T00:00:00+00:00"
    workers = {"w": ({**WORKER, "hooks": {"before": hook(tmp_path, "before", 75, later)}}, "")}
    eng, slot = make_engine(tmp_path, matrix, runner, workers=workers)
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls == [] and slot.store.wakes[0]["claimed_by"] is None
    assert eng.held("prj", "alpha", "!r")
    sent = json.loads((tmp_path / "before.stdin").read_text())
    assert sent["room_id"] == "!r" and sent["wakes"][0]["sender"] == "@b:example.org"
    assert "AGENTROOMS_WORKER=w" in (tmp_path / "before.env").read_text()
    eng.holds.clear()
    workers["w"] = ({**WORKER, "hooks": {"before": hook(tmp_path, "esc", 76, "ask a person")}}, "")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert runner.calls == [] and slot.store.wakes == []
    assert "escalated by worker 'w'" in (tmp_path / engine.LOG_FILE).read_text()
    workers["w"] = ({**WORKER, "hooks": {"before": hook(tmp_path, "go", 0),
                                         "after": hook(tmp_path, "after", 75)}}, "")
    slot.store.add("!r", "$2")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1
    env = (tmp_path / "after.env").read_text()
    assert "AGENTROOMS_OUTCOME=answered" in env and "AGENTROOMS_EVENT_ID=$posted1" in env
    assert eng.held("prj", "alpha", "!elsewhere")


# ── the room's switch: a room switched off is left wholly to interactive use ──


def served_data(**content):
    return {"type": engine.SERVED_TYPE, "content": content}


def test_a_room_switched_off_wakes_nothing_and_switched_on_only_what_follows():
    rooms: dict = {}
    unserved: set = set()
    grants: dict = {}
    off = sync(join={
        "!off": {"summary": {"m.joined_member_count": 2},
                 "account_data": {"events": [served_data(served=False),
                                             {"type": engine.GRANT_TYPE,
                                              "content": {"everyone": False, "users": []}}]},
                 "timeline": {"events": [message("$1", STRANGER, f"{ME} hello"),
                                         message("$2", "@b:example.org", "a direct message")]}},
        "!on": {"summary": {"m.joined_member_count": 2},
                "timeline": {"events": [message("$3", "@b:example.org", "dm")]}}})
    wakes, _ = engine.read_sync(off, ME, rooms, initial=False, member_count=lambda r: 2,
                                grants=grants, unserved=unserved, served_of=lambda r: None)
    # Nothing from the room switched off - not a turn, not a held wake.
    assert [(w["room_id"], w["event_id"]) for w in wakes] == [("!on", "$3")]
    assert unserved == {"!off"}
    # Switched on, only what arrived after the moment it was switched on wakes.
    later = sync(join={"!off": {
        "summary": {"m.joined_member_count": 2},
        "account_data": {"events": [served_data(served=True, since=1_700_000_000_500)]},
        "timeline": {"events": [message("$4", "@b:example.org", "sent while off"),
                                {**message("$5", "@b:example.org", "sent after"),
                                 "origin_server_ts": 1_700_000_000_900}]}}})
    unserved.clear()
    wakes, _ = engine.read_sync(later, ME, rooms, initial=False, member_count=lambda r: 2,
                                grant_of=lambda r: None, unserved=unserved)
    assert [w["event_id"] for w in wakes] == ["$5"] and unserved == set()


def test_a_restarted_agent_reads_a_rooms_switch_before_it_judges_a_wake(tmp_path):
    eng, slot = make_engine(tmp_path, None, FakeRunner([]))
    slot.store = SyncStore()
    slot.store.position = "s0"  # served before: account data is not sent again
    agent = engine.Agent(slot, slot.plan["agents"]["alpha"])
    direct = {"next_batch": "s1", "rooms": {"join": {ROOM: {
        "summary": {"m.joined_member_count": 2},
        "timeline": {"events": [said("$dm", "hello", 9_000)]}}}}}
    regrant = {"next_batch": "s2", "rooms": {"join": {ROOM: {
        **grant_data(everyone=True, users=[]),
        "timeline": {"events": [said("$dm2", "hello again", 9_500)]}}}}}
    matrix = SyncMatrix([direct, regrant], {}, grant={"everyone": False, "users": []},
                        served={"served": False})
    agent.sync_once(matrix)
    agent.sync_once(matrix)
    # Asked once; nothing recorded, and a grant in the room releases nothing.
    assert slot.store.wakes == {} and len(matrix.served_reads) == 1
    assert slot.store.grants == [{}, {}]


def test_a_room_switched_off_runs_no_turn_and_posts_no_refusal(tmp_path):
    for workers, sender in (({"w": (WORKER, "")}, "@b:example.org"), ({}, STRANGER)):
        where = tmp_path / sender[1:4]
        where.mkdir()
        matrix = FakeMatrix([message("$1", sender, f"{ME} hi")])
        matrix.served["!r"] = {"served": False}
        runner = FakeRunner(["never"])
        eng, slot = make_engine(where, matrix, runner, workers=workers)
        slot.store.add("!r", "$1", sender=sender)
        eng.turn(engine.Turn(slot, "alpha", "!r"))
        assert runner.calls == [] and matrix.posted == [] and slot.store.wakes == []
        assert slot.store.turn_log == []
        assert "is switched off" in (where / engine.LOG_FILE).read_text()


def test_an_answer_is_not_posted_in_a_room_switched_off_during_the_turn(tmp_path):
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["late answer"])
    eng, slot = make_engine(tmp_path, matrix, runner)
    original = runner.run

    def switch_off_meanwhile(*args, **kwargs):
        matrix.served["!r"] = {"served": False}
        return original(*args, **kwargs)

    runner.run = switch_off_meanwhile
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    assert len(runner.calls) == 1 and matrix.posted == []
    assert slot.store.turn_log[0]["outcome"] == "not_posted"
    assert "switched off during the turn" in (tmp_path / engine.LOG_FILE).read_text()


def test_the_agent_is_taken_up_once(tmp_path):
    eng, slot = make_engine(tmp_path, None, FakeRunner([]))
    asked = []
    eng.host.take_up = lambda entry, cid: asked.append(cid) or ["its card moved"]
    agent = engine.Agent(slot, slot.plan["agents"]["alpha"])
    agent.take_up()
    assert asked == ["alpha"] and agent.taken_up
    assert "alpha: its card moved" in (tmp_path / engine.LOG_FILE).read_text()
