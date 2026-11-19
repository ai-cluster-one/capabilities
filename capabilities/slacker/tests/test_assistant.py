#!/usr/bin/env python3
"""The assistant service: which Slack messages wake a turn, how one person's
burst becomes one turn, where the answer is posted, that a read-only
connection posts nothing, that a reload changes the profile the next turn runs
on, and that a turn can hand work to a job whose result lands in the thread.

The harness runner's `run` is stood in for, so no model is reached, and Slack
is a fake client, so no network is. The profiles are the bundle's own,
resolved through the real runner where it is importable. The store-backed
cases need a throwaway Postgres named by SLACKER_TEST_DSN and skip without
one; each writes under an account and an agent of its own.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()
schema = su._service_schema()
dialogue = su._service_module("dialogue")
profiles = su._service_module("profiles")
jobs = su._service_module("jobs")
try:
    import callva.harness_runner  # noqa: F401
    RUNNER = True
except ImportError:
    RUNNER = False
needs_runner = unittest.skipUnless(RUNNER, "callva-harness-runner is not importable")

OWNER = "UOWNER01"
ALICE = "UALICE01"
BOB = "UBOB0001"
SELF_DM = "DSELF001"
ALICE_DM = "DALICE01"
BOB_DM = "DBOB0001"
CHANNEL = "CTEAM001"
IDENTITY = {"user_id": OWNER, "user": "owner", "self_dm": SELF_DM}


def _settings(**over) -> dict:
    base = {
        "environment": "test",
        "assistant_name": "Helper",
        "direct_messages": {"mode": "allowlist", "default_role": "direct_user"},
        "allowed_users": {OWNER: {"name": "Owner", "role": "supervisor"},
                          ALICE: {"name": "Alice"}},
        "allowed_channels": {CHANNEL: {"name": "team"}},
        "defaults": {"debounce": 0.4, "max_age": 600},
    }
    base.update(over)
    return base


def _ts(offset: float = 0.0) -> str:
    return f"{time.time() + offset:.6f}"


def _msg(**over) -> dict:
    base = {"type": "message", "channel": ALICE_DM, "user": ALICE, "text": "hello",
            "ts": _ts(-1)}
    base.update(over)
    return base


def _gate(settings=None, *, seen=None, kinds=None, with_us=(), sent=(), pending=(),
          write=None):
    policy = dialogue.Policy(settings or _settings(), profiles.DEFAULT_PROFILE)
    kinds = {ALICE_DM: "im", BOB_DM: "im", CHANNEL: "public_channel", **(kinds or {})}
    return dialogue.Gate(
        policy, IDENTITY, seen=seen or (lambda channel, ts: None),
        kind_of=lambda channel: kinds.get(channel, "public_channel"),
        in_thread_with_us=lambda channel, thread: (channel, thread) in with_us,
        sent_here=lambda channel, ts: (channel, ts) in sent,
        pending_from=lambda key, user: (key, user) in pending,
        may_write=lambda: write)


# ── Settings ────────────────────────────────────────────────────────────────


class Settings(unittest.TestCase):
    """The settings surface is closed, and the contract's `settings` verb
    refuses through `_settings_check` what the schema refuses."""

    def test_the_seeded_template_is_valid_and_admits_nobody(self):
        template = json.loads(su._service_template().read_text())
        self.assertIs(schema.validate_settings(template), template)
        self.assertEqual(set(template["defaults"]), schema.DEFAULT_KEYS)
        self.assertFalse(schema.dialogue_configured(template))

    def test_a_document_without_keys_admits_nobody(self):
        self.assertFalse(schema.dialogue_configured({}))
        self.assertFalse(schema.dialogue_configured({"connection": "x", "_help": {}}))

    def test_the_pilots_capture_only_document_is_still_read(self):
        pilot = {"connection": "", "_help": {"connection": "...", "model": "..."}}
        self.assertIs(schema.validate_settings(pilot), pilot)
        self.assertFalse(schema.dialogue_configured(pilot))

    def test_a_full_document_is_valid(self):
        schema.validate_settings(_settings(allowed_channels={
            CHANNEL: {"wake": "message", "may_address": [ALICE], "member_role": "team",
                      "aliases": ["Help(er)?"], "reply_in_thread": False,
                      "follow_threads": False, "profile": "slacker-codex",
                      "worker_timeout": 60, "context": "a room",
                      "voice_transcription": "auto"},
            f"{CHANNEL}/1700000000.000100": {"wake": "message"}},
            authority={"roles": {"supervisor": {"allowed_capabilities": {"*": True}}}}))

    def test_what_the_schema_refuses_names_its_path(self):
        cases = [
            ({"nope": 1}, "settings.nope"),
            ({"allowed_users": {"alice": {}}}, "settings.allowed_users.alice"),
            ({"allowed_channels": {"general": {}}}, "settings.allowed_channels.general"),
            ({"allowed_channels": {CHANNEL: {"wake": "always"}}},
             f"settings.allowed_channels.{CHANNEL}.wake"),
            ({"defaults": {"debounce": 900}}, "settings.defaults.debounce"),
            ({"defaults": {"profile": "a b"}}, "settings.defaults.profile"),
        ]
        for document, path in cases:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as caught:
                    schema.validate_settings(document)
                self.assertTrue(str(caught.exception).startswith(path), caught.exception)

    def test_the_settings_verb_refuses_an_invalid_document_through_the_check(self):
        with mock.patch.object(su, "_project_root", return_value=None):
            self.assertEqual(su._settings_check("project", {"allowed_users": {}}), [])
            problems = su._settings_check(
                "project", {"allowed_channels": {CHANNEL: {"wake": "always"}}})
        self.assertEqual(len(problems), 1)
        self.assertIn("wake", problems[0])

    def test_the_settings_are_declared_for_the_contract(self):
        keys = {decl["key"] for decl in su.SETTINGS["keys"]}
        self.assertIn("defaults.profile", keys)
        self.assertEqual(su.SETTINGS["homes"][0]["path"], "service/settings.json")


# ── The gate ────────────────────────────────────────────────────────────────


class Gate(unittest.TestCase):
    def test_an_allowed_person_in_a_direct_message_is_admitted(self):
        verdict = _gate().judge(_msg())
        self.assertTrue(verdict["admit"], verdict)
        self.assertEqual(verdict["role"], "direct_user")

    def test_a_person_not_allowed_wakes_nothing(self):
        verdict = _gate().judge(_msg(channel=BOB_DM, user=BOB))
        self.assertEqual(verdict["reason"], "sender_not_allowed")

    def test_the_accounts_own_message_is_never_answered(self):
        verdict = _gate().judge(_msg(channel=ALICE_DM, user=OWNER))
        self.assertEqual(verdict["reason"], "own")

    def test_in_its_own_direct_message_the_person_is_answered_but_a_post_is_not(self):
        typed = _msg(channel=SELF_DM, user=OWNER)
        self.assertTrue(_gate().judge(typed)["admit"])
        posted = _msg(channel=SELF_DM, user=OWNER)
        verdict = _gate(sent={(SELF_DM, posted["ts"])}).judge(posted)
        self.assertEqual(verdict["reason"], "own")

    def test_an_unlisted_channel_is_not_answered(self):
        verdict = _gate().judge(_msg(channel="COTHER01", text=f"<@{OWNER}> hi"))
        self.assertEqual(verdict["reason"], "chat_not_allowed")

    def test_a_channel_wakes_on_a_mention_an_alias_or_a_thread_we_are_in(self):
        gate = _gate(with_us={(CHANNEL, "1.000001")})
        self.assertEqual(gate.judge(_msg(channel=CHANNEL, text="hi all"))["reason"],
                         "unaddressed")
        self.assertTrue(gate.judge(_msg(channel=CHANNEL, text=f"<@{OWNER}> hi"))["admit"])
        self.assertTrue(gate.judge(_msg(channel=CHANNEL, text="helper, hi"))["admit"])
        self.assertTrue(gate.judge(_msg(channel=CHANNEL, text="more",
                                        thread_ts="1.000001"))["admit"])

    def test_a_channel_set_to_wake_on_every_message_does(self):
        gate = _gate(_settings(allowed_channels={CHANNEL: {"wake": "message"}}))
        self.assertTrue(gate.judge(_msg(channel=CHANNEL, text="hi all"))["admit"])

    def test_one_thread_may_be_listed_alone(self):
        settings = _settings(allowed_channels={f"COTHER01/1.000001": {"wake": "message"}})
        gate = _gate(settings)
        self.assertTrue(gate.judge(_msg(channel="COTHER01", thread_ts="1.000001"))["admit"])
        self.assertEqual(gate.judge(_msg(channel="COTHER01"))["reason"], "chat_not_allowed")

    def test_the_persons_next_message_joins_the_turn_waiting_for_them(self):
        gate = _gate(pending={(CHANNEL, ALICE)})
        verdict = gate.judge(_msg(channel=CHANNEL, text="and also this"))
        self.assertTrue(verdict["admit"])
        self.assertTrue(verdict["continues"])

    def test_a_connection_that_does_not_grant_writes_wakes_nothing(self):
        verdict = _gate(write="read_only").judge(_msg())
        self.assertEqual(verdict["reason"], "read_only")

    def test_an_edit_a_bot_and_a_stale_message_are_not_answered(self):
        self.assertEqual(_gate().judge(_msg(subtype="message_changed"))["reason"], "subtype")
        self.assertEqual(_gate().judge(_msg(bot_id="B1"))["reason"], "bot")
        self.assertEqual(_gate().judge(_msg(ts=_ts(-3600)))["reason"], "stale")


# ── The dialogue, end to end below the network ──────────────────────────────


class FakeRun:
    """The harness runner's `run`: records each call and answers."""

    def __init__(self, answer="=== REPLY ===\nAll done.", block=False, act=None):
        self.answer = answer
        self.block = block
        self.act = act
        self.calls: list[dict] = []
        self.started = threading.Event()

    def __call__(self, prompt, profile, cwd, *, session, environ, extra_env, cancel,
                 on_start):
        self.calls.append({"prompt": prompt, "profile": profile, "extra_env": dict(extra_env)})
        on_start(types.SimpleNamespace(pid=4242, harness=profile.harness,
                                       session_id="s-1"))
        self.started.set()
        if self.act is not None:
            self.act(extra_env)
        if self.block:
            cancel.wait(30)
            return types.SimpleNamespace(ok=False, answer="", session_id="s-1",
                                         failure=types.SimpleNamespace(
                                             kind="cancelled", message="cancelled"))
        return types.SimpleNamespace(ok=True, answer=self.answer, session_id="s-1",
                                     failure=None)


class FakeSlack:
    """The Slack Web API the service reaches: a post is recorded and answered
    as chat.postMessage answers, and nothing else reaches anything."""

    def __init__(self):
        self.posts: list[dict] = []
        self.lock = threading.Lock()

    def post_message(self, channel, text, thread_ts=None, broadcast=False):
        with self.lock:
            ts = f"{time.time():.6f}"
            self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts,
                               "ts": ts})
        message = {"type": "message", "user": OWNER, "text": text, "ts": ts}
        if thread_ts:
            message["thread_ts"] = thread_ts
        return {"ok": True, "channel": channel, "ts": ts, "message": message}

    def user_info(self, user):
        return {"id": user, "name": user.lower()}

    def conversation_info(self, channel):
        return {}

    def replies(self, channel, thread_ts, limit=200):
        return iter(())


def _register_agent(db, agent_id: str) -> None:
    db.execute("INSERT INTO agents (id, kind) VALUES (%s, 'agent') "
               "ON CONFLICT (id) DO NOTHING", (agent_id,))


@_cli.needs_store
@needs_runner
class DialogueCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg(allow_write=True)
        self.db = su._open_store(self.cfg)
        self.addCleanup(self.db.close)
        self.agent = f"prj_{uuid.uuid4().hex[:10]}"
        _register_agent(self.db, self.agent)
        for conv in ({"id": ALICE_DM, "is_im": True, "user": ALICE},
                     {"id": BOB_DM, "is_im": True, "user": BOB},
                     {"id": SELF_DM, "is_im": True, "user": OWNER},
                     {"id": CHANNEL, "name": "team", "is_channel": True}):
            su._upsert_conversation(self.db, conv)
        self.slack = FakeSlack()
        self.logs: list[str] = []

    def make(self, settings=None, run=None):
        self.run = run or FakeRun()
        made = dialogue.Dialogue(
            su._CliFacade(), cfg=self.cfg, root=Path(tempfile.mkdtemp()),
            settings=settings or _settings(), project_id=self.agent, db=self.db,
            state_dir=Path(tempfile.mkdtemp()), profiles=profiles, client=self.slack,
            identity=IDENTITY, run=self.run, session_factory=lambda: "fresh",
            log=self.logs.append)
        made.prepare()
        self.addCleanup(made.shutdown, 2.0)
        return made

    def arrive(self, d, **over):
        """Capture the message as the listener does, then offer it."""
        msg = _msg(**over)
        su._handle_event(self.db, msg)
        d.offer(msg)
        return msg

    def drive(self, d, until, limit=10.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            d.tick()
            if until():
                return True
            time.sleep(0.02)
        return False

    def settle(self, d, limit=10.0):
        self.assertTrue(self.drive(d, lambda: not d.queued() and not d.busy(), limit),
                        self.logs)

    # -- the proving checks --------------------------------------------------

    def test_a_burst_from_an_allowed_person_gets_one_answer(self):
        d = self.make()
        for text in ("hi", "can you look at the report", "the one from Monday"):
            self.arrive(d, text=text, ts=_ts())
            d.tick()
            time.sleep(0.1)
        self.settle(d)
        self.assertEqual(len(self.run.calls), 1, self.logs)
        prompt = self.run.calls[0]["prompt"]
        for text in ("hi", "can you look at the report", "the one from Monday"):
            self.assertIn(text, prompt.split("--- Current request ---")[1])
        self.assertEqual([(p["channel"], p["thread_ts"], p["text"]) for p in self.slack.posts],
                         [(ALICE_DM, None, "All done.")])

    def test_a_message_in_a_thread_is_answered_in_that_thread(self):
        d = self.make()
        parent = self.arrive(d, channel=CHANNEL, text=f"<@{OWNER}> start", ts=_ts(-5))
        self.settle(d)
        self.assertEqual(self.slack.posts[-1]["thread_ts"], parent["ts"])
        self.arrive(d, channel=CHANNEL, text="and one more thing", ts=_ts(),
                    thread_ts=parent["ts"])
        self.settle(d)
        self.assertEqual(len(self.slack.posts), 2, self.logs)
        self.assertEqual(self.slack.posts[-1]["channel"], CHANNEL)
        self.assertEqual(self.slack.posts[-1]["thread_ts"], parent["ts"])

    def test_a_person_not_allowed_wakes_nothing(self):
        d = self.make()
        self.arrive(d, channel=BOB_DM, user=BOB, text="hello?")
        self.arrive(d, channel=CHANNEL, user=BOB, text="hi all")
        self.settle(d)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.slack.posts, [])

    def test_a_read_only_connection_posts_nothing(self):
        self.cfg["allow_write"] = False
        d = self.make()
        self.arrive(d, text="hello")
        self.settle(d)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.slack.posts, [])
        with self.assertRaises(su._Refusal):
            d.post_text(ALICE_DM, None, "anything")
        self.assertEqual(self.slack.posts, [])

    def test_the_services_own_post_never_wakes_a_turn(self):
        d = self.make()
        self.arrive(d, channel=SELF_DM, user=OWNER, text="note to self", ts=_ts())
        self.settle(d)
        self.assertEqual(len(self.run.calls), 1)
        post = self.slack.posts[-1]
        echoed = {"type": "message", "channel": SELF_DM, "user": OWNER,
                  "text": post["text"], "ts": post["ts"]}
        su._handle_event(self.db, echoed)
        d.offer(echoed)
        self.settle(d)
        self.assertEqual(len(self.run.calls), 1, self.logs)
        row = self.db.execute("SELECT sent_by_service FROM slacker_messages WHERE"
                              " account = %s AND channel_id = %s AND ts = %s",
                              (self.db.account, SELF_DM, post["ts"])).fetchone()
        self.assertTrue(row["sent_by_service"])

    def test_a_reload_changes_the_profile_the_next_turn_runs_on(self):
        d = self.make()
        self.arrive(d, text="first", ts=_ts(-2))
        self.settle(d)
        changed = _settings()
        changed["allowed_users"][ALICE]["profile"] = "slacker-codex"
        d.update_settings(changed)
        self.arrive(d, text="second", ts=_ts())
        self.settle(d)
        self.assertEqual([c["profile"].harness for c in self.run.calls],
                         ["claude", "codex"])

    def test_the_listener_takes_a_reload_and_keeps_its_settings_when_one_is_refused(self):
        d = self.make()
        state = tempfile.mkdtemp()
        with mock.patch.object(su, "_STATE_HOME", Path(state)):
            cap = su._Capture(self.cfg, _settings(), None)
        cap.dialogue = d
        registry = {"connections": {self.cfg["id"]: {"allow_write": True}}}
        changed = _settings(allowed_channels={CHANNEL: {"profile": "slacker-codex"}})
        with mock.patch.object(su, "_service_settings", return_value=changed), \
                mock.patch.object(su, "_connections_registry",
                                  return_value=(registry, None)):
            cap.reload()
        self.assertIsNone(cap.reload_error)
        self.assertEqual(d.policy.channels[CHANNEL]["profile"], "slacker-codex")
        refused = _settings(defaults={"profile": "no-such-profile"})
        with mock.patch.object(su, "_service_settings", return_value=refused), \
                mock.patch.object(su, "_connections_registry",
                                  return_value=(registry, None)):
            cap.reload()
        self.assertIn("no-such-profile", cap.reload_error)
        self.assertEqual(d.policy.channels[CHANNEL]["profile"], "slacker-codex")
        health = json.loads((cap.home / "health.json").read_text())
        self.assertEqual(health["settings_reload_attempts"], 2)
        self.assertIn("no-such-profile", health["settings_reload_error"])

    def test_a_reload_reads_the_write_grant_again(self):
        d = self.make()
        with mock.patch.object(su, "_STATE_HOME", Path(tempfile.mkdtemp())):
            cap = su._Capture(self.cfg, _settings(), None)
        cap.dialogue = d
        registry = {"connections": {self.cfg["id"]: {"allow_write": False}}}
        with mock.patch.object(su, "_service_settings", return_value=_settings()), \
                mock.patch.object(su, "_connections_registry",
                                  return_value=(registry, None)):
            cap.reload()
        self.assertFalse(self.cfg["allow_write"])
        self.arrive(d, text="hello")
        self.settle(d)
        self.assertEqual(self.slack.posts, [])

    def test_an_audio_clip_reaches_the_turn_as_its_words(self):
        d = self.make()
        clip = {"id": "F1", "subtype": "slack_audio", "mimetype": "audio/webm",
                "transcription": {"status": "complete",
                                  "preview": {"content": "please call me back",
                                              "has_more": False}}}
        msg = self.arrive(d, text="", files=[clip], ts=_ts())
        self.settle(d)
        self.assertIn("[voice] please call me back", self.run.calls[0]["prompt"])
        row = self.db.execute("SELECT transcript FROM slacker_messages WHERE account = %s"
                              " AND channel_id = %s AND ts = %s",
                              (self.db.account, ALICE_DM, msg["ts"])).fetchone()
        self.assertEqual(row["transcript"], "please call me back")

    def test_an_audio_clip_without_a_transcript_or_a_key_is_answered_as_failed(self):
        d = self.make()
        clip = {"id": "F2", "subtype": "slack_audio", "mimetype": "audio/webm",
                "url_private": "https://files.example.invalid/F2"}
        with mock.patch.object(su, "_resolve_env_key", return_value=(None, None, None)):
            self.arrive(d, text="", files=[clip], ts=_ts())
            self.settle(d)
        self.assertIn("transcription failed: no Deepgram key", self.run.calls[0]["prompt"])

    def test_a_turn_hands_work_to_a_job_whose_result_lands_in_the_thread(self):
        settings = _settings()
        submitted = {}

        def act(env):
            register = jobs.JobRegister(su._CliFacade(), self.db, self.agent, "test")
            row = register.register(
                chat_id=env["SLACKER_AUTHORIZED_CHAT_ID"],
                requested_by=env["SLACKER_AUTHORIZED_REQUESTER"],
                description="compile the weekly numbers",
                origin_message_id=env["SLACKER_AUTHORIZED_ORIGIN_MESSAGE_ID"],
                thread_ts=env["SLACKER_AUTHORIZED_THREAD_TS"] or None)
            submitted.update(register.submit(row["id"]))

        d = self.make(settings, run=FakeRun(answer="=== REPLY ===\none second", act=act))
        job_run = FakeRun(answer="=== REPLY ===\nThe numbers are in.")
        d.jobs = jobs.JobRunner(su._CliFacade(), dialogue=d, db=self.db, run=job_run,
                                sessions=(lambda: "fresh", lambda sid: sid), poll=0.05)
        request = self.arrive(d, channel=CHANNEL, text=f"<@{OWNER}> weekly numbers?",
                              ts=_ts())
        self.settle(d)
        self.assertEqual(submitted["thread_ts"], request["ts"])
        self.assertEqual(self.slack.posts[0]["text"], "one second")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(self.slack.posts) < 2:
            d.jobs.tick()
            time.sleep(0.05)
        self.assertEqual([(p["channel"], p["thread_ts"], p["text"])
                          for p in self.slack.posts[1:]],
                         [(CHANNEL, request["ts"], "The numbers are in.")])
        row = d.jobs.register.get(submitted["id"])
        self.assertEqual((row["outcome"], row["delivery_state"]), ("succeeded", "delivered"))


if __name__ == "__main__":
    unittest.main()
