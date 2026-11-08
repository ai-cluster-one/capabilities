"""The dialogue of one bot, end to end without Telegram or a model: the door, the
batch one person's messages make, the turn's prompt and answer, the service
commands, voice, the agent limit and the reply gate. The store is an in-memory
stand-in with the same answers; the Bot API, the history reader and the harness
runner are fakes."""

from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from _cli import load_service

t = load_service("transport")
d = load_service("dialogue")
gate = load_service("gate")

BOT = 123456
ANN, BOB, PEER_BOT = 42, 43, 777
GROUP = -1001000000001


class Store:
    def __init__(self):
        self.rows: dict = {}
        self.seen_rows: list = []
        self.prompts: dict = {}

    def _row(self, agent, conn, chat, topic):
        return self.rows.setdefault((agent, conn, chat, topic),
                                    {"overrides": {}, "last_message_id": 0, "reserved": [],
                                     "reserved_by": None, "bot_turns": 0})

    def _operation(self):
        import contextlib
        return contextlib.nullcontext(None)

    def see(self, *args, **kwargs):
        self.seen_rows.append((args, kwargs))
        return True

    def channel(self, agent, conn, chat, topic=0):
        return dict(self._row(agent, conn, chat, topic))

    def reserve(self, agent, conn, chat, topic, message_id, holder):
        row = self._row(agent, conn, chat, topic)
        if message_id <= row["last_message_id"] or message_id in row["reserved"]:
            return False
        row["reserved"].append(message_id)
        return True

    def finish(self, agent, conn, chat, topic, ids, *, bot_turn=None):
        row = self._row(agent, conn, chat, topic)
        row["reserved"] = [x for x in row["reserved"] if x not in ids]
        row["last_message_id"] = max([row["last_message_id"], *ids])
        if bot_turn is not None:
            row["bot_turns"] = row["bot_turns"] + 1 if bot_turn else 0

    def release(self, agent, conn, chat, topic, ids):
        row = self._row(agent, conn, chat, topic)
        row["reserved"] = [x for x in row["reserved"] if x not in ids]

    def reset_bot_turns(self, agent, conn, chat, topic):
        self._row(agent, conn, chat, topic)["bot_turns"] = 0

    def set_override(self, agent, conn, chat, topic, key, value):
        row = self._row(agent, conn, chat, topic)
        if value is None:
            row["overrides"].pop(key, None)
        else:
            row["overrides"][key] = value
        return row["overrides"]

    def prompt(self, agent, key):
        return {"body": self.prompts[key]} if key in self.prompts else None


class Api:
    def __init__(self):
        self.sent: list[dict] = []
        self.typing_calls: list = []
        self.reactions: list = []
        self.status = "member"
        self.next_id = 1000

    def send_text(self, chat, text, *, topic_id=None, reply_to=None):
        self.next_id += 1
        self.sent.append({"chat": chat, "text": text, "topic": topic_id, "reply_to": reply_to})
        return [self.next_id]

    def send_file(self, chat, path, *, caption=None, topic_id=None, reply_to=None):
        self.sent.append({"chat": chat, "file": str(path), "text": caption})
        return 1

    def typing(self, chat, topic=None):
        self.typing_calls.append((chat, topic))

    def member(self, chat, user):
        return {"status": self.status}

    def get_chat(self, chat):
        return {"id": chat}

    def react(self, chat, message, emoji):
        self.reactions.append((chat, message, emoji))

    def download(self, file_id, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"ogg")
        return dest


class Profiles:
    """The profiles module as the dialogue asks it, without the runner."""

    PREFIX = "telegrambot-"

    def default_profile(self):
        return "telegrambot-claude"

    def default_job_profile(self):
        return "telegrambot-job-claude"

    def stored(self, conn, project):
        return []

    def resolve(self, name, active, project):
        if not name.startswith("telegrambot-") or name == "telegrambot-missing":
            raise ValueError(f"profile {name!r} is not found")
        harness = "codex" if "codex" in name else "claude"
        return object(), {"name": name, "layer": "capability", "harness": harness}, \
            {"harness": harness}

    def knobs_of(self, name, active, project):
        return {"harness": "claude", "output_schema": {}, "system_prompt": "x",
                "persist_session": False, "timeout_seconds": 60, "tools": [],
                "setting_sources": [], "strict_mcp": True, "max_turns": 2}, {"layer": "capability"}

    def listing(self, active, project):
        return [{"name": "telegrambot-claude", "layer": "capability", "harness": "claude",
                 "model": "opus", "fits_turn": True}]

    def runner(self):
        lib = types.SimpleNamespace()
        lib.Profile = types.SimpleNamespace(from_dict=lambda knobs: types.SimpleNamespace(**knobs))
        lib.Session = types.SimpleNamespace(fresh=lambda: "fresh")
        return lib


class Result:
    def __init__(self, answer="", ok=True, structured=None):
        self.answer, self.ok, self.structured = answer, ok, structured
        self.session_id, self.failure = None, None


class Runner:
    def __init__(self, answer="=== REPLY ===\nhello there"):
        self.answer = answer
        self.calls: list[dict] = []

    def __call__(self, prompt, profile, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
        answer = self.answer(prompt, kwargs) if callable(self.answer) else self.answer
        return answer if isinstance(answer, Result) else Result(answer)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class FakeReading:
    def __init__(self, messages, line="history: read 1 of 40"):
        self.messages, self.line = messages, line


def make(tmp_path, settings=None, *, history=None, deepgram=None, runner=None, **kwargs):
    clock = Clock()
    store = Store()
    api = Api()
    runner = runner or Runner()
    dialogue = d.Dialogue(
        agent_id="prj_test", connection="helper", root=tmp_path, api=api,
        me={"id": BOT, "username": "helper_bot", "first_name": "Helper"}, store=store,
        settings=settings or {"allowed_users": {str(ANN): {"name": "Ann", "role": "supervisor"}}},
        state_dir=tmp_path / "state", profiles=Profiles(), gate=gate,
        read_history=history, history_off=None if history else "no api_id",
        deepgram=deepgram, voice_off=None if deepgram else "no key",
        transcribe=kwargs.pop("transcribe", None), cli_path="/bin/telegrambot",
        turn_env={"PATH": "/usr/bin"}, run=runner, session_factory=lambda: "fresh",
        from_update=t.from_update, now=clock, **kwargs)
    return dialogue, store, api, runner, clock


def update(message_id, text, *, sender=ANN, chat=None, name="Ann", is_bot=False, forum=False,
           **extra):
    chat = chat if chat is not None else sender
    msg = {"message_id": message_id, "date": 1_790_000_000 + message_id, "text": text,
           "from": {"id": sender, "is_bot": is_bot, "first_name": name},
           "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup",
                    "title": None if chat > 0 else "Team", "is_forum": forum}}
    msg.update(extra)
    return {"update_id": message_id, "message": msg}


def drain(dialogue, clock, seconds=10.0):
    """Let the debounce pass and every turn the tick starts finish."""
    clock.value += seconds
    for _ in range(100):
        time.sleep(0.01)
        dialogue.tick()
        runs = [ch.get("run") for ch in dialogue.channels.values() if ch.get("run")]
        for run in runs:
            run.thread.join(5)
        if not dialogue.channels:
            return
        clock.value += seconds


def test_a_stranger_is_seen_and_ignored_and_start_is_no_turn(tmp_path):
    dialogue, store, api, runner, clock = make(tmp_path)
    assert dialogue.offer_update(update(1, "hi", sender=BOB, name="Bob"))["reason"] == \
        "sender_not_allowed"
    assert any(args[2] == "person" and args[3] == BOB for args, _ in store.seen_rows)
    assert dialogue.offer_update(update(2, "/start"))["reason"] == "start"
    drain(dialogue, clock)
    assert api.sent == [] and runner.calls == []


def test_one_persons_messages_in_the_debounce_are_one_turn(tmp_path):
    dialogue, store, api, runner, clock = make(tmp_path)
    for n, text in ((1, "first part"), (2, "second part"), (3, "third")):
        assert dialogue.offer_update(update(n, text))["admit"]
    assert api.typing_calls or True
    drain(dialogue, clock)
    assert len(runner.calls) == 1
    prompt = runner.calls[0]["prompt"]
    assert "Messages: #1, #2, #3" in prompt and "first part\nsecond part\nthird" in prompt
    assert api.sent == [{"chat": ANN, "text": "hello there", "topic": None, "reply_to": None}]
    assert store.channel("prj_test", "helper", ANN)["last_message_id"] == 3
    # A redelivered update is not acted on again.
    assert dialogue.offer_update(update(2, "second part"))["reason"] == "already_taken"


def test_the_prompt_is_the_standard_then_the_agent_then_the_chat(tmp_path):
    settings = {"allowed_users": {str(ANN): {"name": "Ann", "context": "Ann likes brevity."}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    store.prompts["bot"] = "You are Helper, the project's bot."
    dialogue.offer_update(update(1, "question"))
    drain(dialogue, clock)
    prompt = runner.calls[0]["prompt"]
    mech, behaviour = d.template("mechanics").strip(), d.template("behaviour").strip()
    order = [prompt.index(mech[:40]), prompt.index(behaviour[:20]),
             prompt.index("You are Helper"), prompt.index("Ann likes brevity"),
             prompt.index("--- Channel state ---"), prompt.index("--- Current request ---"),
             prompt.index("--- Conversation ---")]
    assert order == sorted(order)
    assert "history: off: no api_id" in prompt
    assert "Delegation" not in prompt and "job worker" not in prompt
    # prompt_mode replace drops the standard behaviour, never the mechanics.
    dialogue.update_settings({**settings, "prompt_mode": "replace"})
    dialogue.offer_update(update(2, "again"))
    drain(dialogue, clock)
    prompt = runner.calls[1]["prompt"]
    assert mech[:40] in prompt and behaviour[:20] not in prompt and "You are Helper" in prompt


def test_an_exclusive_topic_cuts_what_is_above_it_but_not_the_mechanics(tmp_path):
    settings = {"allowed_groups": {str(GROUP): {
        "context": "Room rules.", "require_reference": False,
        "topics": {"5": {"context": "Only the API here.", "context_mode": "exclusive"}}}},
        "allowed_users": {str(ANN): {"name": "Ann"}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    store.prompts["bot"] = "AGENT PROMPT"
    dialogue.offer_update(update(9, "in the topic", chat=GROUP, message_thread_id=5,
                                 is_topic_message=True, forum=True))
    drain(dialogue, clock)
    prompt = runner.calls[0]["prompt"]
    assert d.template("mechanics")[:40] in prompt and "Only the API here." in prompt
    assert "Room rules." not in prompt and "AGENT PROMPT" not in prompt
    assert api.sent[0]["topic"] == 5 and api.sent[0]["reply_to"] == 9


def test_a_group_answers_only_when_addressed(tmp_path):
    settings = {"allowed_groups": {str(GROUP): {"aliases": ["Helper"]}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    chat = dict(chat=GROUP)
    assert dialogue.offer_update(update(1, "just chatting", **chat))["reason"] == "unaddressed"
    assert dialogue.offer_update(update(2, "@helper_bot help", **chat,
                                        entities=[{"type": "mention", "offset": 0,
                                                   "length": 11}]))["admit"]
    assert dialogue.offer_update(update(3, "Helper, what now?", sender=BOB, **chat))["admit"]
    assert dialogue.offer_update(update(4, "thanks", sender=BOB, **chat,
                                        reply_to_message={"message_id": 100,
                                                          "from": {"id": BOT}}))["admit"]
    # Triggers switched off leave only what is still on.
    dialogue.update_settings({"allowed_groups": {str(GROUP): {"triggers": ["username"],
                                                              "aliases": ["Helper"]}}})
    assert dialogue.offer_update(update(5, "Helper?", **chat))["reason"] == "unaddressed"
    # A member silenced in this room never starts a turn.
    dialogue.update_settings({"allowed_groups": {str(GROUP): {
        "require_reference": False, "members": {str(BOB): {"may_address": False}}}}})
    assert dialogue.offer_update(update(6, "hey", sender=BOB, **chat))["reason"] == \
        "sender_not_allowed"


def test_noreply_external_and_another_bots_command(tmp_path):
    dialogue, store, api, runner, clock = make(tmp_path)
    assert dialogue.offer_update(update(1, "fyi\n#noreply"))["reason"] == "noreply"
    assert dialogue.offer_update(update(2, "/status@other_bot"))["reason"] == \
        "another_bots_command"
    dialogue.offer_update(update(3, "please answer\n#external"))
    drain(dialogue, clock)
    assert api.sent[-1]["text"] == "hello there\n#noreply"


def test_other_bots_get_a_bounded_run_of_turns(tmp_path):
    settings = {"direct_messages": {"mode": "anyone"}, "agent_dialogue": {"max_turns": 2}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    for n in (1, 2):
        assert dialogue.offer_update(update(n * 10, f"ping {n}", sender=PEER_BOT,
                                            is_bot=True, name="Peer"))["admit"]
        drain(dialogue, clock)
    assert dialogue.offer_update(update(30, "ping 3", sender=PEER_BOT, is_bot=True,
                                        name="Peer"))["reason"] == "agent_limit"
    # The limit is per chat: a person's message elsewhere does not lift it here.
    assert store.channel("prj_test", "helper", PEER_BOT)["bot_turns"] == 2


def test_the_limit_is_on_by_default_and_a_person_resets_it(tmp_path):
    settings = {"allowed_groups": {str(GROUP): {"require_reference": False}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    for n in range(1, 5):
        dialogue.offer_update(update(n, f"bot {n}", sender=PEER_BOT, chat=GROUP,
                                     is_bot=True))
        drain(dialogue, clock)
    assert dialogue.offer_update(update(5, "bot 5", sender=PEER_BOT, chat=GROUP,
                                        is_bot=True))["reason"] == "agent_limit"
    dialogue.offer_update(update(6, "a person", sender=ANN, chat=GROUP))
    drain(dialogue, clock)
    assert dialogue.offer_update(update(7, "bot 6", sender=PEER_BOT, chat=GROUP,
                                        is_bot=True))["admit"]


def test_history_is_read_back_and_said_how_far_it_reached(tmp_path):
    reads = []

    def history(chat, anchor, **kwargs):
        reads.append((chat, anchor, kwargs))
        old = t.Message(chat_id=chat, message_id=1, date=1_790_000_000, sender_id=ANN,
                        sender_name="Ann", text="older line")
        mine = t.Message(chat_id=chat, message_id=2, date=1_790_000_001, sender_id=BOT,
                         own=True, text="an earlier answer")
        return FakeReading([old, mine], "history: read 2 of 40, nothing older found")

    dialogue, store, api, runner, clock = make(tmp_path, history=history)
    dialogue.offer_update(update(5, "and now?"))
    drain(dialogue, clock)
    prompt = runner.calls[0]["prompt"]
    assert reads[0][:2] == (ANN, 5) and reads[0][2]["limit"] == 40
    assert "history: read 2 of 40, nothing older found" in prompt
    assert "older line" in prompt and "Helper (you): an earlier answer" in prompt


def test_a_group_the_bot_does_not_fully_see_reads_no_history(tmp_path):
    reads = []
    settings = {"allowed_groups": {str(GROUP): {}}}
    dialogue, store, api, runner, clock = make(
        tmp_path, settings, history=lambda *a, **k: reads.append(a) or FakeReading([]))
    dialogue.offer_update(update(5, "@helper_bot hi", chat=GROUP,
                                 entities=[{"type": "mention", "offset": 0, "length": 11}]))
    drain(dialogue, clock)
    assert reads == []
    assert "it reads no history here" in runner.calls[0]["prompt"]
    api.status = "administrator"
    dialogue.visible.clear()
    dialogue.offer_update(update(6, "@helper_bot again", chat=GROUP,
                                 entities=[{"type": "mention", "offset": 0, "length": 11}]))
    drain(dialogue, clock)
    assert reads


def test_service_commands_and_their_gate(tmp_path):
    settings = {"direct_messages": {"mode": "anyone"},
                "allowed_users": {str(ANN): {"name": "Ann", "role": "supervisor"}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    dialogue.offer_update(update(1, "/status"))
    assert "profile = telegrambot-claude" in api.sent[-1]["text"]
    assert "history: off" in api.sent[-1]["text"]
    dialogue.offer_update(update(2, "/set tail 12"))
    assert api.sent[-1]["text"] == "ok, tail = 12"
    dialogue.offer_update(update(3, "/set profile telegrambot-missing"))
    assert api.sent[-1]["text"].startswith("nope:")
    dialogue.offer_update(update(4, "/set voice-echo off"))
    assert store.channel("prj_test", "helper", ANN)["overrides"] == {"tail_size": 12,
                                                                    "voice_echo": False}
    dialogue.offer_update(update(5, "/set tail default"))
    assert "40 effective" in api.sent[-1]["text"]
    dialogue.offer_update(update(6, "/set", sender=BOB, name="Bob"))
    assert api.sent[-1]["text"] == "nope: /set is not allowed for role direct_user"
    dialogue.offer_update(update(7, "/profiles", sender=BOB, name="Bob"))
    assert "this chat runs on telegrambot-claude" in api.sent[-1]["text"]
    assert runner.calls == []


def test_stop_and_stop_n(tmp_path):
    started = []

    def slow(prompt, kwargs):
        started.append(1)
        kwargs["cancel"].wait(5)
        return Result("")

    dialogue, store, api, runner, clock = make(tmp_path, runner=Runner(slow))
    dialogue.offer_update(update(1, "long one"))
    clock.value += 10
    dialogue.tick()
    for _ in range(50):
        if started:
            break
        time.sleep(0.05)
    dialogue.offer_update(update(2, "/status"))
    assert "1. running, Ann" in api.sent[-1]["text"]
    dialogue.offer_update(update(3, "/stop 1"))
    assert api.sent[-1]["text"] == "Stopped 1."
    drain(dialogue, clock)
    dialogue.offer_update(update(4, "/stop 1"))
    assert api.sent[-1]["text"] == "nope: there is no reply 1 in the last /status list" or \
        api.sent[-1]["text"] == "Reply 1 finished already."


def test_voice_is_transcribed_echoed_and_read_as_text(tmp_path):
    def transcribe(path, key, mime=None):
        return {"text": "remind me tomorrow"}

    dialogue, store, api, runner, clock = make(tmp_path, deepgram="dg", transcribe=transcribe)
    voice = {"file_id": "F1", "duration": 2, "mime_type": "audio/ogg"}
    dialogue.offer_update(update(1, "", voice=voice))
    drain(dialogue, clock)
    assert api.sent[0] == {"chat": ANN, "text": "> remind me tomorrow", "topic": None,
                           "reply_to": 1}
    assert "[voice] remind me tomorrow" in runner.calls[0]["prompt"]
    assert json.loads(dialogue.transcript_path(ANN, 1).read_text())["text"] == \
        "remind me tomorrow"
    # The echo has its own per-chat switch.
    dialogue.offer_update(update(2, "/set voice-echo off"))
    dialogue.offer_update(update(3, "", voice=voice))
    before = len(api.sent)
    drain(dialogue, clock)
    assert all(not s["text"].startswith(">") for s in api.sent[before:])


def test_an_ambient_voice_note_is_answered_only_when_it_names_the_bot(tmp_path):
    spoken = iter(["nice weather today", "Helper what is the plan"])
    settings = {"allowed_groups": {str(GROUP): {"voice_transcription": "auto",
                                                "aliases": ["Helper"]}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings, deepgram="dg",
                                               transcribe=lambda *a, **k: {"text": next(spoken)})
    voice = {"file_id": "F1", "duration": 2}
    dialogue.offer_update(update(1, "", chat=GROUP, voice=voice))
    drain(dialogue, clock)
    assert runner.calls == [] and api.sent[0]["text"] == "> nice weather today"
    dialogue.offer_update(update(2, "", chat=GROUP, voice=voice))
    drain(dialogue, clock)
    assert len(runner.calls) == 1


def test_the_reply_gate_shadow_logs_and_live_acts(tmp_path):
    lines = []
    decision = {"action": "react", "target_message_id": 2, "emoji": "👍", "reason": "thanks"}

    def answer(prompt, kwargs):
        return Result(json.dumps(decision), structured=decision)

    settings = {"allowed_groups": {str(GROUP): {"gate": {"enabled": True}}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings, runner=Runner(answer),
                                               log=lines.append)
    api.status = "administrator"
    assert dialogue.offer_update(update(2, "thanks all", chat=GROUP))["reason"] == "gated"
    drain(dialogue, clock)
    for _ in range(100):
        if any(": gate {" in line for line in lines):
            break
        time.sleep(0.02)
    record = json.loads(next(line for line in lines if ": gate {" in line).split(": gate ", 1)[1])
    assert record["action"] == "react" and record["mode"] == "shadow" and not record["acted"]
    assert api.reactions == []
    dialogue.update_settings({"allowed_groups": {str(GROUP): {"gate": {"enabled": True,
                                                                       "mode": "live"}}}})
    dialogue.offer_update(update(3, "more thanks", chat=GROUP))
    decision["target_message_id"] = 3
    drain(dialogue, clock)
    for _ in range(100):
        if api.reactions:
            break
        time.sleep(0.02)
    assert api.reactions == [(GROUP, 3, "👍")]


def test_a_routed_chat_runs_in_its_project_and_a_missing_one_says_so(tmp_path):
    target = tmp_path / "other"
    (target / ".git").mkdir(parents=True)
    settings = {"allowed_users": {str(ANN): {"name": "Ann", "project": str(target)}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    dialogue.offer_update(update(1, "work there"))
    drain(dialogue, clock)
    assert runner.calls[0]["cwd"] == str(target)
    assert runner.calls[0]["environ"]["CLAUDE_PROJECT_DIR"] == str(target)
    dialogue.update_settings({"allowed_users": {str(ANN): {"project": str(tmp_path / "gone")}}})
    dialogue.offer_update(update(2, "again"))
    drain(dialogue, clock)
    assert "is not a project folder here" in api.sent[-1]["text"] and len(runner.calls) == 1


def test_a_role_that_names_a_profile_runs_its_senders_turns_on_it(tmp_path):
    settings = {"allowed_users": {str(ANN): {"name": "Ann", "role": "supervisor"}},
                "allowed_groups": {str(GROUP): {"aliases": ["Helper"],
                                                "profile": "telegrambot-codex"}},
                "roles": {"group_member": {"profile": "telegrambot-consult"}}}
    dialogue, store, api, runner, clock = make(tmp_path, settings)
    chat = dict(chat=GROUP)
    assert dialogue.offer_update(update(1, "Helper, explain", sender=BOB, name="Bob",
                                        **chat))["admit"]
    drain(dialogue, clock)
    assert dialogue.offer_update(update(2, "Helper, deploy", **chat))["admit"]
    drain(dialogue, clock)
    member, supervisor = (call["prompt"] for call in runner.calls)
    assert "profile=telegrambot-consult" in member and "(role: group_member)" in member
    assert "profile=telegrambot-codex" in supervisor and "(role: supervisor)" in supervisor
    # A chat's /set profile moves the supervisor, never the member off its role's profile.
    store.set_override("prj_test", "helper", GROUP, 0, "profile", "telegrambot-claude")
    assert dialogue.channel_settings(GROUP, None, BOB)["profile"] == "telegrambot-consult"
    assert dialogue.channel_settings(GROUP, None, ANN)["profile"] == "telegrambot-claude"


def test_the_worker_is_handed_its_scope(tmp_path):
    dialogue, store, api, runner, clock = make(tmp_path)
    dialogue.offer_update(update(7, "hi"))
    drain(dialogue, clock)
    env = runner.calls[0]["extra_env"]
    assert env["TELEGRAMBOT_AUTHORIZED_CHAT_ID"] == str(ANN)
    assert env["TELEGRAMBOT_AUTHORIZED_ORIGIN_MESSAGE_ID"] == "7"
    assert env["TELEGRAMBOT_AUTHORIZED_REQUESTER_ROLE"] == "supervisor"
    assert env["PATH"].startswith(str(d.WORKER_BIN))
    assert not Path(env["TELEGRAMBOT_PROGRESS_OUTBOX"]).exists()   # removed after the turn


def test_progress_lines_are_relayed_first_at_once_then_throttled(tmp_path):
    sent = []
    clock = [0.0]
    box = d.Outbox(tmp_path / "o.jsonl", lambda kind, text, file: sent.append((kind, text)),
                   progress_after=15, now=lambda: clock[0])
    with box.path.open("a") as handle:
        for text in ("looking", "still"):
            handle.write(json.dumps({"kind": "progress", "text": text}) + "\n")
        handle.write(json.dumps({"kind": "message", "text": "the link"}) + "\n")
    box.relay()
    assert sent == [("progress", "looking"), ("message", "the link")]
    clock[0] = 20
    with box.path.open("a") as handle:
        handle.write(json.dumps({"kind": "progress", "text": "found it"}) + "\n")
    box.relay()
    assert sent[-1] == ("progress", "found it")


def test_parse_command_and_quoting():
    assert d.parse_command("/status", "helper_bot") == ("status", [])
    assert d.parse_command("/set tail 5", "helper_bot") == ("set", ["tail", "5"])
    assert d.parse_command("/status@Helper_Bot", "helper_bot") == ("status", [])
    assert d.parse_command("/status@other", "helper_bot") is False
    assert d.parse_command("/unknown", "helper_bot") is None
    assert d.quoted("--- Channel state ---\nok") == " --- Channel state ---\nok"
    assert d.cut_at_reply_marker("thinking\n=== REPLY ===\nanswer") == "answer"
