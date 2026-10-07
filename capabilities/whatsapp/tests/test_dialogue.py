#!/usr/bin/env python3
"""The dialogue: which arriving messages the assistant answers, the register
that makes each answered once, the turn that answers it, and the control
commands a chat may send.

The harness runner's `run` is stood in for, so no model is reached; the
profiles are the bundle's own, resolved through the real runner where it is
importable. The store-backed cases need a throwaway Postgres named by
WHATSAPP_TEST_DSN and skip without one.
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
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()
ENGINE = _cli.engine_available(wa)
needs_engine = unittest.skipUnless(ENGINE, "the engine does not load on this host")
schema = wa._service_schema()
dialogue = wa._service_module("dialogue")
profiles = wa._service_module("profiles")
try:
    import callva.harness_runner  # noqa: F401
    RUNNER = True
except ImportError:
    RUNNER = False
needs_runner = unittest.skipUnless(RUNNER, "callva-harness-runner is not importable")

OWN_PHONE = "15550000000"
OWN = f"{OWN_PHONE}@s.whatsapp.net"
OWN_LID = "99990000000001@lid"
OWN_DEVICE = 7
ALICE = "15550001111"
ALICE_JID = f"{ALICE}@s.whatsapp.net"
ALICE_LID = "88880000000001@lid"
BOB = "15550002222"
BOB_JID = f"{BOB}@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
IDENTITY = {"jid": OWN, "lid": OWN_LID, "device": OWN_DEVICE, "phone": OWN_PHONE}


def _settings(**over) -> dict:
    base = {
        "environment": "test",
        "assistant_name": "Helper",
        "direct_messages": {"mode": "allowlist", "default_role": "direct_user"},
        "allowed_users": {OWN_PHONE: {"name": "Owner", "role": "supervisor"},
                          ALICE: {"name": "Alice"}},
        "allowed_groups": {GROUP: {"name": "Team", "aliases": ["Helper"]}},
        "authority": {"roles": {
            "supervisor": {"allowed_capabilities": {"*": True}},
            "direct_user": {"allowed_capabilities": {"whatsapp": ["messages"]}},
            "group_member": {"allowed_capabilities": {}}}},
        "defaults": {"debounce": 0, "max_age": 600},
    }
    base.update(over)
    return base


def _msg(**over) -> dict:
    base = {"chat_id": ALICE_JID, "id": f"M{time.monotonic_ns()}", "from_me": 0,
            "sender": ALICE_JID, "sender_lid": None, "ts": int(time.time()) - 5,
            "text": "hello", "kind": "conversation", "sync_type": "LIVE",
            "mentions": [], "quoted_id": None, "quoted_participant": None,
            "sender_device": 0, "push_name": "Alice"}
    base.update(over)
    return base


def _gate(settings=None, *, seen=None, resolve=None, ours=(), sent=(), now=None):
    policy = dialogue.Policy(settings or _settings(), profiles.DEFAULT_PROFILE)

    def resolve_phone(pn, lid):
        if resolve is not None:
            return resolve(pn, lid)
        return dialogue.digits(dialogue.jid_user(pn)) if pn else None
    return dialogue.Gate(policy, IDENTITY, seen=seen or (lambda *a: None),
                         resolve_phone=resolve_phone,
                         quoted_is_ours=lambda chat, qid: qid in ours,
                         sent_here=lambda chat, mid: mid in sent,
                         now=now or time.time)


# ── Settings ────────────────────────────────────────────────────────────────


class Settings(unittest.TestCase):
    """The settings surface is closed, and every place that reads it refuses
    what it does not name."""

    def test_the_seeded_template_names_every_key_and_answers_nobody(self):
        template = json.loads(wa._service_template().read_text())
        self.assertIs(schema.validate_settings(template), template)
        self.assertEqual(set(template), schema.TOP_LEVEL - {"send_rate"})
        self.assertEqual(set(template["defaults"]), schema.DEFAULT_KEYS)
        self.assertFalse(schema.dialogue_configured(template))

    def test_the_seeded_context_carries_the_reply_marker(self):
        self.assertIn("=== REPLY ===", wa._service_template("context.md").read_text())

    def test_a_full_document_is_valid(self):
        schema.validate_settings(_settings(
            allowed_groups={GROUP: {"require_reference": False, "aliases": ["Mar(vin)?"],
                                    "may_address": [ALICE], "member_role": "team",
                                    "profile": "whatsapp-codex", "worker_timeout": 60,
                                    "context": "a room"}},
            control={"roles": {"team": {"commands": ["status"]}}},
            defaults={"tail_size": 10, "debounce": 1, "max_age": 60, "worker_timeout": 30,
                      "max_parallel_dialogue": 2, "profile": "whatsapp-claude",
                      "send_rate": 5}))

    def test_unknown_keys_are_refused_with_their_path(self):
        cases = [
            ({"voice": True}, "settings.voice"),
            ({"allowed_users": {ALICE: {"voice_agent": {}}}},
             f"settings.allowed_users.{ALICE}.voice_agent"),
            ({"allowed_groups": {GROUP: {"members": {}}}},
             f"settings.allowed_groups.{GROUP}.members"),
            ({"defaults": {"worker": "claude"}}, "settings.defaults.worker"),
            ({"defaults": {"workers": {}}}, "settings.defaults.workers"),
            ({"direct_messages": {"open": True}}, "settings.direct_messages.open"),
        ]
        for document, path in cases:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, f"{path}: unsupported property"):
                    schema.validate_settings(document)

    def test_bad_values_are_refused(self):
        cases = [
            ({"direct_messages": {"mode": "allowed_users"}}, "direct_messages.mode"),
            ({"allowed_users": {"alice": {}}}, "allowed_users.alice"),
            ({"allowed_users": {ALICE: {}, f"+{ALICE}": {}}}, "same number"),
            ({"allowed_groups": {"-100123": {}}}, "group JID"),
            ({"allowed_groups": {GROUP: {"may_address": "everyone"}}}, "may_address"),
            ({"allowed_groups": {GROUP: {"aliases": ["("]}}}, "aliases"),
            ({"allowed_groups": {GROUP: {"require_reference": "yes"}}}, "require_reference"),
            ({"defaults": {"tail_size": 0}}, "tail_size"),
            ({"defaults": {"max_age": 5}}, "max_age"),
            ({"defaults": {"profile": "a b"}}, "profile"),
            ({"control": {"roles": {"x": {"commands": ["record"]}}}}, "record"),
            ({"send_rate": 5, "defaults": {"send_rate": 5}}, "not both"),
        ]
        for document, fragment in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(ValueError, fragment):
                    schema.validate_settings(document)

    def test_the_old_send_rate_is_read_as_the_default_one(self):
        self.assertEqual(wa._settings_send_rate({"send_rate": 7}), 7)
        self.assertEqual(wa._settings_send_rate({"defaults": {"send_rate": 9}}), 9)
        self.assertEqual(wa._settings_send_rate({}), wa.SEND_RATE_DEFAULT)
        self.assertEqual(schema.deprecations({"send_rate": 7}),
                         ["settings.send_rate is read as defaults.send_rate; "
                          "name that instead"])

    def test_start_doctor_and_reload_refuse_through_the_same_walk(self):
        bad = {"allowed_users": {ALICE: {"worker": "claude"}}}
        with mock.patch.object(wa, "_service_settings", return_value=bad):
            with self.assertRaises(wa._Refusal) as caught:
                wa._checked_service_settings()
            self.assertEqual(caught.exception.code, "service_settings_invalid")
            self.assertIn(f"allowed_users.{ALICE}.worker", caught.exception.message)
            with mock.patch.object(wa, "_running_owner") as owner:
                with self.assertRaises(wa._Refusal):
                    wa._cmd_service_reload(None, 1.0)
            owner.assert_not_called()

    def test_which_settings_need_a_dialogue(self):
        self.assertFalse(schema.dialogue_configured({}))
        self.assertFalse(schema.dialogue_configured({"allowed_users": {}}))
        self.assertTrue(schema.dialogue_configured({"allowed_users": {ALICE: {}}}))
        self.assertFalse(schema.dialogue_configured(
            {"allowed_users": {ALICE: {}}, "direct_messages": {"mode": "off"}}))
        self.assertTrue(schema.dialogue_configured({"allowed_groups": {GROUP: {}}}))
        self.assertTrue(schema.dialogue_configured({"direct_messages": {"mode": "anyone"}}))


# ── The gate ────────────────────────────────────────────────────────────────


class Gate(unittest.TestCase):
    """Each step of the order, and the first stop wins."""

    def judge(self, msg, **gate):
        return _gate(**gate).judge(msg)

    def test_a_direct_message_from_an_allowed_number_is_a_turn(self):
        verdict = self.judge(_msg())
        self.assertTrue(verdict["admit"])
        self.assertEqual((verdict["kind"], verdict["role"], verdict["phone"]),
                         ("turn", "direct_user", ALICE))

    def test_our_own_message_elsewhere_is_never_answered(self):
        verdict = self.judge(_msg(from_me=1, sender=OWN, sender_device=0))
        self.assertEqual(verdict["reason"], "own")

    def test_our_own_send_in_the_self_chat_is_never_answered(self):
        by_device = _msg(chat_id=OWN, from_me=1, sender=OWN, sender_device=OWN_DEVICE)
        self.assertEqual(self.judge(by_device)["reason"], "own")
        by_row = _msg(chat_id=OWN, from_me=1, sender=OWN, sender_device=0, id="SENT1")
        self.assertEqual(self.judge(by_row, sent={"SENT1"})["reason"], "own")

    def test_the_owner_typing_in_the_self_chat_from_the_phone_is_admitted(self):
        for chat in (OWN, OWN_LID):
            with self.subTest(chat=chat):
                verdict = self.judge(_msg(chat_id=chat, from_me=1, sender=OWN,
                                          sender_device=0))
                self.assertTrue(verdict["admit"])
                self.assertEqual((verdict["phone"], verdict["role"]),
                                 (OWN_PHONE, "supervisor"))

    def test_the_self_chat_is_refused_when_the_owner_is_not_allowed(self):
        settings = _settings(allowed_users={ALICE: {}})
        verdict = self.judge(_msg(chat_id=OWN, from_me=1, sender=OWN, sender_device=0),
                             settings=settings)
        self.assertEqual(verdict["reason"], "sender_not_allowed")

    def test_history_sync_is_never_answered(self):
        for origin in ("FULL", "RECENT", "ON_DEMAND", "INITIAL_BOOTSTRAP", "OUTBOUND"):
            with self.subTest(origin=origin):
                self.assertEqual(self.judge(_msg(sync_type=origin))["reason"], "history")

    def test_a_message_older_than_max_age_is_dropped(self):
        now = time.time()
        self.assertEqual(self.judge(_msg(ts=now - 601), now=lambda: now)["reason"],
                         "stale")
        self.assertTrue(self.judge(_msg(ts=now - 599), now=lambda: now)["admit"])

    def test_a_message_the_register_settled_is_dropped(self):
        verdict = self.judge(_msg(id="DONE"),
                             seen=lambda chat, mid, ts: "processed" if mid == "DONE" else None)
        self.assertEqual(verdict["reason"], "processed")

    def test_a_chat_that_is_not_allowed_is_dropped(self):
        self.assertEqual(self.judge(_msg(chat_id="120363999@g.us",
                                         sender=ALICE_JID))["reason"], "chat_not_allowed")
        settings = _settings(direct_messages={"mode": "off"})
        self.assertEqual(self.judge(_msg(), settings=settings)["reason"],
                         "chat_not_allowed")

    def test_an_unknown_sender_is_refused(self):
        verdict = self.judge(_msg(chat_id=BOB_JID, sender=BOB_JID))
        self.assertEqual(verdict["reason"], "sender_not_allowed")
        open_dm = _settings(direct_messages={"mode": "anyone"})
        verdict = self.judge(_msg(chat_id=BOB_JID, sender=BOB_JID), settings=open_dm)
        self.assertEqual((verdict["admit"], verdict["role"]), (True, "direct_user"))

    def test_a_lid_sender_is_matched_to_its_phone(self):
        verdict = self.judge(
            _msg(chat_id=ALICE_LID, sender=None, sender_lid=ALICE_LID),
            resolve=lambda pn, lid: ALICE if lid == ALICE_LID else None)
        self.assertTrue(verdict["admit"])
        self.assertEqual(verdict["phone"], ALICE)
        unknown = self.judge(_msg(chat_id="7777@lid", sender=None, sender_lid="7777@lid"),
                             resolve=lambda pn, lid: None)
        self.assertEqual(unknown["reason"], "sender_not_allowed")

    def test_noreply_on_the_last_line_opts_out(self):
        self.assertEqual(self.judge(_msg(text="for the log\n#noreply"))["reason"],
                         "noreply")
        self.assertTrue(self.judge(_msg(text="what does #noreply do?"))["admit"])

    def test_an_unaddressed_group_message_is_ignored(self):
        verdict = self.judge(_msg(chat_id=GROUP, text="lunch?"))
        self.assertEqual(verdict["reason"], "unaddressed")

    def test_a_mention_a_quote_or_an_alias_addresses_the_group(self):
        cases = {
            "mention phone": _msg(chat_id=GROUP, text="@15550000000 hi", mentions=[OWN]),
            "mention lid": _msg(chat_id=GROUP, text="@9999 hi", mentions=[OWN_LID]),
            "quote participant": _msg(chat_id=GROUP, text="and?", quoted_id="Q1",
                                      quoted_participant=OWN),
            "quote stored": _msg(chat_id=GROUP, text="and?", quoted_id="OURS"),
            "alias": _msg(chat_id=GROUP, text="helper, what time is it"),
        }
        for name, msg in cases.items():
            with self.subTest(name):
                verdict = self.judge(msg, ours={"OURS"})
                self.assertTrue(verdict["admit"], verdict)
                self.assertEqual(verdict["role"], "group_member")
        self.assertEqual(self.judge(_msg(chat_id=GROUP, text="helperism"))["reason"],
                         "unaddressed")
        self.assertEqual(self.judge(_msg(chat_id=GROUP, text="ok", quoted_id="THEIRS"),
                                    ours={"OURS"})["reason"], "unaddressed")

    def test_a_group_without_require_reference_answers_every_message(self):
        settings = _settings(allowed_groups={GROUP: {"require_reference": False}})
        self.assertTrue(self.judge(_msg(chat_id=GROUP, text="lunch?"),
                                   settings=settings)["admit"])

    def test_who_may_address_a_group(self):
        anyone = self.judge(_msg(chat_id=GROUP, sender=BOB_JID, text="Helper?"))
        self.assertEqual((anyone["admit"], anyone["role"]), (True, "group_member"))
        listed = _settings(allowed_groups={GROUP: {"may_address": "allowed_users"}})
        self.assertEqual(self.judge(_msg(chat_id=GROUP, sender=BOB_JID, text="Helper?"),
                                    settings=listed)["reason"], "sender_not_allowed")
        named = _settings(allowed_groups={GROUP: {"may_address": [f"+{BOB}"],
                                                  "member_role": "team"}})
        verdict = self.judge(_msg(chat_id=GROUP, sender=BOB_JID, text="Helper?"),
                             settings=named)
        self.assertEqual((verdict["admit"], verdict["role"]), (True, "team"))

    def test_a_control_command_is_recognised_past_a_leading_address(self):
        cases = {"/status": ("status", []), "/set tail 5": ("set", ["tail", "5"]),
                 "@15550000000 /stop": ("stop", []), "Helper, /help": ("help", [])}
        for text, command in cases.items():
            with self.subTest(text):
                verdict = self.judge(_msg(chat_id=OWN, from_me=1, sender=OWN,
                                          sender_device=0, text=text))
                self.assertEqual((verdict["kind"], verdict["command"]),
                                 ("control", command))
        self.assertEqual(self.judge(_msg(text="/unknown thing"))["kind"], "turn")


# ── Store-backed ────────────────────────────────────────────────────────────


class FakeRun:
    """The runner's `run`, recording each call and answering as told."""

    def __init__(self, answer="=== REPLY ===\nhello back", *, ok=True, kind=None,
                 block=False):
        self.answer, self.ok, self.kind, self.block = answer, ok, kind, block
        self.calls: list[dict] = []
        self.started = threading.Event()

    def __call__(self, prompt, profile, cwd, *, session, environ, extra_env, cancel,
                 on_start):
        auth = extra_env.get("CAPABILITIES_AUTH_CONTEXT")
        self.calls.append({"prompt": prompt, "profile": profile, "cwd": cwd,
                           "session": session, "environ": dict(environ),
                           "extra_env": dict(extra_env), "cancel": cancel,
                           "auth": json.loads(Path(auth).read_text()) if auth else None,
                           "auth_path": auth})
        on_start(types.SimpleNamespace(pid=4242, harness="claude", session_id="s"))
        self.started.set()
        if self.block:
            cancel.wait(30)
            return types.SimpleNamespace(
                ok=False, answer="", failure=types.SimpleNamespace(kind="cancelled",
                                                                   message="cancelled"))
        failure = None if self.ok else types.SimpleNamespace(kind=self.kind or "error",
                                                             message="it broke")
        return types.SimpleNamespace(ok=self.ok, answer=self.answer, failure=failure)


class DialogueCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg(engine=wa.ENGINE_INHOUSE)
        self.db = wa._open_store(self.cfg)
        self.addCleanup(self.db.close)
        self.root = Path(tempfile.mkdtemp())
        self.logs: list[str] = []
        records = types.SimpleNamespace(
            document_read=lambda cap, key: {"body": "CONTEXT: be brief."}
            if key == "context" else None)
        patcher = mock.patch.object(wa, "_records", return_value=records)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, settings=None, run=None, project_id="prj_test"):
        self.run = run or FakeRun()
        made = dialogue.Dialogue(
            wa._CliFacade(), cfg=self.cfg, root=self.root,
            settings=settings or _settings(), project_id=project_id, db=self.db,
            state_dir=Path(tempfile.mkdtemp()), profiles=profiles, run=self.run,
            session_factory=lambda: "fresh-session", log=self.logs.append)
        made.prepare()
        made.identity = dict(IDENTITY)
        self.addCleanup(made.shutdown, 2.0)
        return made

    def capture(self, msg):
        """Write the message as the capture path would, before it is offered."""
        row = {k: msg.get(k) for k in ("chat_id", "id", "sender", "sender_lid",
                                       "from_me", "ts", "kind", "text", "push_name",
                                       "quoted_id", "sync_type")}
        row["captured_at"] = wa._iso_now()
        wa._write_live(self.db, row)
        return msg

    def arrive(self, d, **over):
        msg = self.capture(_msg(**over))
        return msg, d.offer(msg)

    def drive(self, d, until, limit=10.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            d.tick(None)
            if until():
                return True
            time.sleep(0.01)
        return False

    def settle(self, d, limit=10.0):
        self.assertTrue(self.drive(d, lambda: not d.queued() and not d.busy(), limit))

    def outgoing(self, chat=None):
        return self.db.execute(
            "SELECT * FROM whatsapp_messages WHERE account = %s AND local_id IS NOT NULL"
            + (" AND chat_id = %s" if chat else "") + " ORDER BY requested_at",
            (self.db.account, *([chat] if chat else []))).fetchall()

    def register_row(self, chat, project_id="prj_test"):
        return self.db.execute(
            "SELECT * FROM whatsapp_register WHERE project_id = %s AND account = %s"
            " AND chat_id = %s", (project_id, self.db.account, chat)).fetchone()


@_cli.needs_store
@needs_runner
class Register(DialogueCase):
    """A message is answered once, across redelivery and restart, and nothing
    before the service first saw a chat is answered."""

    def test_the_register_is_a_step_of_its_own_under_the_naming_law(self):
        steps = [r["step"] for r in self.db.execute(
            "SELECT step FROM schema_ledger WHERE owner = 'whatsapp' ORDER BY step")]
        self.assertEqual(steps[12], "0013-register")
        version = self.db.execute(
            "SELECT major, minor FROM schema_version WHERE owner = 'whatsapp'").fetchone()
        self.assertEqual((version["major"], version["minor"]), (1, wa.STORE_SCHEMA_MINOR))
        columns = {r["column_name"] for r in self.db.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'whatsapp_register'")}
        self.assertTrue({"project_id", "environment", "account", "chat_id", "watermark",
                         "processed", "overrides", "counters"} <= columns)

    def test_the_reservation_is_written_before_the_turn_starts(self):
        d = self.make()
        msg, verdict = self.arrive(d)
        self.assertTrue(verdict["admit"])
        row = self.register_row(ALICE_JID)
        self.assertIn(msg["id"], row["processed"])
        self.assertEqual(self.run.calls, [])
        self.assertEqual(d.queued(), 1)

    def test_a_redelivered_message_is_answered_once(self):
        d = self.make()
        msg, first = self.arrive(d)
        second = d.offer(dict(msg))
        self.assertTrue(first["admit"])
        self.assertEqual((second["admit"], second["reason"]), (False, "processed"))
        self.settle(d)
        self.assertEqual(len(self.run.calls), 1)
        self.assertEqual(len(self.outgoing(ALICE_JID)), 1)

    def test_concurrent_offers_of_one_message_admit_one(self):
        d = self.make()
        msg = self.capture(_msg())
        verdicts = []
        threads = [threading.Thread(target=lambda: verdicts.append(d.offer(dict(msg))))
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sum(1 for v in verdicts if v["admit"]), 1)

    def test_a_restart_does_not_answer_what_was_processed(self):
        d = self.make()
        msg, _ = self.arrive(d)
        self.settle(d)
        restarted = self.make()
        again = restarted.offer(dict(msg))
        self.assertEqual(again["reason"], "processed")
        self.assertEqual(len(self.outgoing(ALICE_JID)), 1)

    def test_nothing_before_a_chats_first_sight_is_answered(self):
        d = self.make()
        now = int(time.time())
        self.arrive(d, ts=now - 10)
        earlier = self.capture(_msg(ts=now - 60))
        self.assertEqual(d.offer(earlier)["reason"], "before_first_sight")

    def test_registers_are_kept_per_project(self):
        mine = self.make()
        msg, _ = self.arrive(mine)
        other = self.make(project_id="prj_other")
        self.assertTrue(other.offer(dict(msg))["admit"])

    def test_a_lid_sender_is_matched_through_the_stores_identities(self):
        wa._remember_identity(self.db, ALICE_JID, ALICE_LID)
        d = self.make(_settings(allowed_groups={GROUP: {"may_address": "allowed_users",
                                                        "require_reference": False}}))
        _msg_, verdict = self.arrive(d, chat_id=GROUP, sender=None, sender_lid=ALICE_LID)
        self.assertEqual((verdict["admit"], verdict["phone"]), (True, ALICE))

    def test_an_unknown_lid_is_resolved_through_the_engine(self):
        d = self.make()
        d.session = types.SimpleNamespace(
            resolve_lid=lambda lid: ALICE_JID if lid == "5555@lid" else None)
        _msg_, verdict = self.arrive(d, chat_id="5555@lid", sender=None,
                                     sender_lid="5555@lid")
        self.assertEqual((verdict["admit"], verdict["phone"]), (True, ALICE))

    def test_our_own_sent_row_in_the_self_chat_never_starts_a_turn(self):
        d = self.make()
        row = wa._queue_outgoing(self.db, {"chat_id": OWN, "text": "an answer",
                                           "reply_to": None, "mentions": [],
                                           "typing": False})
        wa._apply_delivery(self.db, {"local_id": row["local_id"], "state": "sent",
                                     "message_id": "WAOWN1", "timestamp": time.time()})
        verdict = d.offer(_msg(chat_id=OWN, id="WAOWN1", from_me=1, sender=OWN,
                               sender_device=0, text="an answer"))
        self.assertEqual(verdict["reason"], "own")


@_cli.needs_store
@needs_runner
class StoreLoss(DialogueCase):
    """The store closing a connection that sat idle loses nothing: the
    statement is sent again on a new one, a write proves its connection first,
    and a message the store could not judge at all is judged once it answers."""

    def kill(self, db):
        """Close `db`'s connection from the server side, as an idle timeout does."""
        import psycopg
        pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
        with psycopg.connect(_cli.STORE_DSN, autocommit=True) as other:
            other.execute("SELECT pg_terminate_backend(%s)", (pid,))
        time.sleep(0.2)

    def test_a_statement_on_a_connection_the_server_closed_is_sent_again(self):
        self.kill(self.db)
        self.assertEqual(self.db.execute("SELECT 41 + 1").fetchone()[0], 42)
        self.assertFalse(self.db.broken())

    def test_a_write_proves_an_idle_connection_before_its_transaction(self):
        self.kill(self.db)
        with mock.patch.object(wa, "STORE_IDLE_PROBE", 0.0):
            row = wa._queue_outgoing(self.db, {"chat_id": ALICE_JID, "text": "after",
                                               "reply_to": None, "mentions": [],
                                               "typing": False})
        self.assertEqual(row["delivery"], "pending")

    def test_a_message_arriving_after_an_idle_close_is_answered(self):
        d = self.make()
        msg = self.capture(_msg(text="after a quiet hour"))
        self.kill(self.db)
        with mock.patch.object(wa, "STORE_IDLE_PROBE", 0.0):
            verdict = d.offer(msg)
            self.assertTrue(verdict["admit"], verdict)
            self.settle(d)
        self.assertEqual([r["text"] for r in self.outgoing(ALICE_JID)], ["hello back"])

    def test_a_message_the_store_could_not_judge_is_judged_when_it_answers(self):
        import psycopg
        d = self.make()
        msg = self.capture(_msg(text="while the store is away"))
        real = d.register.seen
        down = {"on": True}

        def seen(*args):
            if down["on"]:
                raise psycopg.OperationalError("server closed the connection unexpectedly")
            return real(*args)
        d.register.seen = seen
        self.assertEqual(d.offer(msg)["reason"], "deferred")
        self.assertEqual(d.summary()["deferred"], 1)
        self.assertIn("server closed", d.summary()["store_error"])
        d.next_retry = 0
        d.tick(None)
        self.assertEqual(len(d.deferred), 1)          # still away: kept
        down["on"] = False
        d.next_retry = 0
        self.settle(d)
        self.assertEqual(d.deferred, [])
        self.assertIsNone(d.summary()["store_error"])
        self.assertEqual(len(self.run.calls), 1)
        self.assertEqual(d.offer(dict(msg))["reason"], "processed")


@_cli.needs_store
@needs_runner
class Turn(DialogueCase):
    """A turn: what it is told, what it runs on, and what it answers."""

    def answer(self, d, **over):
        msg, verdict = self.arrive(d, **over)
        self.assertTrue(verdict["admit"], verdict)
        self.settle(d)
        return msg

    def test_the_prompt_layers_context_overlay_state_request_and_tail(self):
        settings = _settings()
        settings["allowed_users"][ALICE]["context"] = "Alice prefers short answers."
        d = self.make(settings)
        self.capture(_msg(text="an earlier line", ts=int(time.time()) - 30, id="E1"))
        msg = self.answer(d, text="what did I say before?")
        prompt = self.run.calls[0]["prompt"]
        order = ["CONTEXT: be brief.", "--- Channel-specific context ---",
                 "Alice prefers short answers.", "--- Channel state ---",
                 "--- Current request ---", f"Message: #{msg['id']}",
                 "what did I say before?", "--- Conversation ---", "an earlier line"]
        positions = [prompt.index(part) for part in order]
        positions.append(prompt.rindex(dialogue.CONVERSATION_END))
        self.assertEqual(positions, sorted(positions))
        self.assertIn("From: Alice (role: direct_user)", prompt)
        self.assertIn("Tool authority: whatsapp (verbs=messages)", prompt)
        self.assertIn("Context window: 2 msgs (of max 40)", prompt)

    def test_the_tail_is_read_from_the_store_up_to_tail_size(self):
        settings = _settings(defaults={"debounce": 0, "tail_size": 3})
        d = self.make(settings)
        now = int(time.time())
        for n in range(5):
            self.capture(_msg(text=f"line {n}", ts=now - 50 + n, id=f"T{n}"))
        self.answer(d, text="now", ts=now - 5)
        conversation = self.run.calls[0]["prompt"].split("--- Conversation ---")[1]
        self.assertNotIn("line 2", conversation)
        self.assertIn("line 3", conversation)
        self.assertIn("line 4", conversation)
        self.assertIn("Alice: now", conversation)

    def test_the_profile_is_resolved_per_level(self):
        settings = _settings(
            allowed_users={OWN_PHONE: {"role": "supervisor", "profile": "claude-act"},
                           ALICE: {}},
            allowed_groups={GROUP: {"profile": "codex-act", "require_reference": False}},
            defaults={"debounce": 0, "profile": "whatsapp-codex"})
        d = self.make(settings)
        self.assertEqual(d.channel(ALICE_JID, ALICE)["profile"], "whatsapp-codex")
        self.assertEqual(d.channel(OWN, OWN_PHONE)["profile"], "claude-act")
        self.assertEqual(d.channel(GROUP, ALICE)["profile"], "codex-act")
        self.answer(d)
        self.assertEqual(self.run.calls[-1]["profile"].harness, "codex")
        d.register.set_override(ALICE_JID, "profile", "whatsapp-claude")
        self.assertEqual(d.channel(ALICE_JID, ALICE)["profile"], "whatsapp-claude")
        self.answer(d)
        self.assertEqual(self.run.calls[-1]["profile"].harness, "claude")
        self.assertEqual(self.run.calls[-1]["profile"].model, "opus")

    def test_a_project_profile_shadows_the_bundles(self):
        folder = self.root / "capabilities" / "whatsapp" / "service" / "profiles"
        folder.mkdir(parents=True)
        (folder / "whatsapp-claude.toml").write_text(
            'harness = "claude"\nmodel = "sonnet"\npermission_mode = "bypassPermissions"\n')
        d = self.make()
        _profile, origin = d.profile("whatsapp-claude")
        self.assertEqual((origin["source"], origin["model"]), ("folder", "sonnet"))
        self.assertTrue(origin["path"].startswith(str(folder)))

    def test_a_profile_that_does_not_fit_is_refused_at_start(self):
        for name in ("claude-read-sandboxed", "codex-read-sandboxed", "no-such-profile"):
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    self.make(_settings(defaults={"profile": name}))
                self.assertIn("defaults.profile", str(caught.exception))
                self.assertIn(name, str(caught.exception))
        with mock.patch.object(wa, "_runner_importable", return_value=True):
            with self.assertRaises(wa._Refusal) as caught:
                wa._require_profiles(self.root, _settings(
                    allowed_users={ALICE: {"profile": "claude-read-sandboxed"}}))
        self.assertEqual(caught.exception.code, "service_profile_invalid")
        self.assertIn(f"allowed_users.{ALICE}.profile", caught.exception.message)

    def test_the_authority_and_plumbing_reach_the_run(self):
        d = self.make()
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent",
                                          "CLAUDECODE": "1",
                                          "WHATSAPP_SERVICE_LAUNCH_NONCE": "n"}):
            msg = self.answer(d)
        call = self.run.calls[0]
        for name in ("SSH_AUTH_SOCK", "CLAUDECODE", "WHATSAPP_SERVICE_LAUNCH_NONCE"):
            self.assertNotIn(name, call["environ"])
        env = call["extra_env"]
        self.assertEqual(env["WHATSAPP_AUTHORIZED_CHAT_ID"], ALICE_JID)
        self.assertEqual(env["WHATSAPP_AUTHORIZED_REQUESTER"], ALICE)
        self.assertEqual(env["WHATSAPP_AUTHORIZED_ORIGIN_MESSAGE_ID"], msg["id"])
        self.assertEqual(env["WHATSAPP_DAEMON_CHILD"], "1")
        self.assertEqual(call["auth"]["sender_role"], "direct_user")
        self.assertEqual(call["auth"]["allowed_capabilities"],
                         {"whatsapp": {"allow": True, "verbs": ["messages"]}})
        self.assertEqual(call["auth"]["source"], "whatsapp")
        self.assertFalse(Path(call["auth_path"]).exists())
        self.assertEqual(call["cwd"], str(self.root))
        self.assertEqual(call["session"], "fresh-session")

    def test_without_declared_authority_no_context_is_passed(self):
        settings = _settings()
        del settings["authority"]
        d = self.make(settings)
        self.answer(d)
        self.assertNotIn("CAPABILITIES_AUTH_CONTEXT", self.run.calls[0]["extra_env"])

    def test_the_answer_is_cut_at_the_marker_and_queued_for_the_listener(self):
        d = self.make(run=FakeRun("thinking it over\n=== REPLY ===\nThe answer."))
        self.answer(d)
        rows = self.outgoing(ALICE_JID)
        self.assertEqual([(r["text"], r["delivery"], r["quoted_id"], r["typing"])
                          for r in rows], [("The answer.", "pending", None, True)])

    def test_a_group_answer_quotes_the_request(self):
        d = self.make()
        msg = self.answer(d, chat_id=GROUP, text="Helper, status?")
        rows = self.outgoing(GROUP)
        self.assertEqual([r["quoted_id"] for r in rows], [msg["id"]])

    def test_an_empty_completed_turn_is_silent(self):
        for answer in ("", "=== REPLY ===\n", "working\n=== REPLY ===\n   "):
            with self.subTest(answer=answer):
                d = self.make(run=FakeRun(answer))
                self.answer(d)
                self.assertEqual(self.outgoing(ALICE_JID), [])
                self.assertEqual(d.stats["silent"], 1)

    def test_a_failed_turn_says_so_and_a_long_answer_is_split(self):
        d = self.make(run=FakeRun(ok=False, kind="quota"))
        self.answer(d)
        self.assertIn("quota", self.outgoing(ALICE_JID)[0]["text"])
        long = "\n\n".join(("paragraph %d " % n) + "x" * 1500 for n in range(5))
        self.db.execute("DELETE FROM whatsapp_messages WHERE account = %s",
                        (self.db.account,))
        d = self.make(run=FakeRun(long))
        self.answer(d)
        parts = [r["text"] for r in self.outgoing(ALICE_JID)]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= dialogue.REPLY_PART_CHARS for p in parts))
        self.assertEqual("\n\n".join(parts), long)

    def test_the_timeout_ends_the_run_through_cancel(self):
        d = self.make(_settings(defaults={"debounce": 0, "worker_timeout": 1}),
                      run=FakeRun(block=True))
        started = time.monotonic()
        self.answer(d)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.run.calls[0]["cancel"].is_set())
        self.assertIn("longer than 1s", self.outgoing(ALICE_JID)[0]["text"])
        self.assertEqual(self.register_row(ALICE_JID)["counters"]["failed"], 1)

    def test_turns_in_a_chat_run_in_order_after_the_debounce(self):
        d = self.make(_settings(defaults={"debounce": 1}))
        now = int(time.time())
        for n in range(2):
            d.offer(self.capture(_msg(text=f"q{n}", id=f"Q{n}", ts=now - 5 + n)))
        d.tick(None)
        time.sleep(0.3)
        d.tick(None)
        self.assertEqual(self.run.calls, [])
        self.settle(d)
        self.assertEqual([c["prompt"].split("--- Current request ---\n")[1].split("\n")[0]
                          for c in self.run.calls], ["Message: #Q0", "Message: #Q1"])


@_cli.needs_store
@needs_runner
class Control(DialogueCase):
    """Commands sent as text in an admitted chat, gated by role."""

    def command(self, d, text, **over):
        defaults = {"chat_id": OWN, "from_me": 1, "sender": OWN, "sender_device": 0}
        defaults.update(over)
        _msg_, verdict = self.arrive(d, text=text, **defaults)
        self.assertEqual(verdict.get("kind"), "control", verdict)
        rows = self.outgoing(defaults["chat_id"])
        return rows[-1]["text"] if rows else None

    def test_status_and_help(self):
        d = self.make()
        status = self.command(d, "/status")
        self.assertIn("your role: supervisor", status)
        self.assertIn("profile: whatsapp-claude (claude", status)
        self.assertIn("/reload", self.command(d, "/help"))
        self.assertEqual(self.run.calls, [])

    def test_a_role_without_the_command_is_refused(self):
        d = self.make()
        reply = self.command(d, "/set tail 5", chat_id=ALICE_JID, from_me=0,
                             sender=ALICE_JID)
        self.assertEqual(reply, "nope: /set is not allowed for role direct_user")
        self.assertEqual(d.register.overrides(ALICE_JID), {})
        self.assertNotIn("/reload", self.command(d, "/help", chat_id=ALICE_JID, from_me=0,
                                                 sender=ALICE_JID))

    def test_control_roles_follow_the_settings(self):
        settings = _settings(control={"roles": {"direct_user": {"commands": ["set"]}}})
        d = self.make(settings)
        reply = self.command(d, "/set tail 5", chat_id=ALICE_JID, from_me=0,
                             sender=ALICE_JID)
        self.assertEqual(reply, "ok, tail = 5")

    def test_set_persists_in_the_register_and_validates(self):
        d = self.make()
        self.assertEqual(self.command(d, "/set tail 5"), "ok, tail = 5")
        self.assertEqual(self.command(d, "/set worker-timeout 30"),
                         "ok, worker-timeout = 30")
        self.assertIn("must be from 1 to 500", self.command(d, "/set tail 900"))
        self.assertIn("does not fit", self.command(d, "/set profile claude-read-sandboxed"))
        self.assertEqual(self.command(d, "/set profile whatsapp-codex"),
                         "ok, profile = whatsapp-codex")
        self.assertEqual(d.register.overrides(OWN),
                         {"tail_size": 5, "worker_timeout": 30, "profile": "whatsapp-codex"})
        self.assertIn("usage: /set", self.command(d, "/set"))
        self.assertTrue(self.command(d, "/set tail default").startswith("ok, tail = default"))
        self.assertNotIn("tail_size", d.register.overrides(OWN))

    def test_stop_ends_a_running_turn(self):
        d = self.make(run=FakeRun(block=True))
        self.arrive(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0,
                    text="a long job")
        self.assertTrue(self.drive(d, lambda: self.run.started.is_set()))
        self.assertEqual(self.command(d, "/stop"), "Stopped.")
        self.settle(d)
        self.assertTrue(self.run.calls[0]["cancel"].is_set())
        self.assertEqual([r["text"] for r in self.outgoing(OWN)], ["Stopped."])
        self.assertEqual(self.command(d, "/stop"), "Nothing is running right now.")

    def test_reload_asks_the_listener_and_reports_its_answer(self):
        asked = []
        d = self.make()
        d.on_reload = lambda: asked.append(True)
        self.assertIsNone(self.command(d, "/reload"))
        self.assertEqual(asked, [True])
        d.after_reload(None)
        self.assertEqual(self.outgoing(OWN)[-1]["text"], "ok, settings reloaded")
        self.command(d, "/reload")
        d.after_reload("settings.voice: unsupported")
        self.assertIn("reload refused", self.outgoing(OWN)[-1]["text"])

    def test_a_group_command_needs_the_address(self):
        d = self.make()
        _msg_, verdict = self.arrive(d, chat_id=GROUP, text="/status")
        self.assertEqual(verdict["reason"], "unaddressed")
        _msg_, verdict = self.arrive(d, chat_id=GROUP, text="@15550000000 /status",
                                     mentions=[OWN])
        self.assertEqual(verdict["kind"], "control")
        self.assertEqual(self.outgoing(GROUP)[-1]["quoted_id"], _msg_["id"])


# ── The listener ────────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self):
        self.sent: list = []
        self.presence: list = []
        self.is_connected = True
        self.minted = 0

    def send_chat_presence(self, to, state, media):
        self.presence.append(state)

    def send_message(self, to, message):
        self.minted += 1
        self.sent.append((to, message))
        return types.SimpleNamespace(ID=f"WAR{self.minted}{os.getpid()}",
                                     Timestamp=int(time.time() * 1000))


@_cli.needs_store
@needs_runner
@needs_engine
class Listener(DialogueCase):
    """Through the running listener: a captured message is answered by a row
    the listener itself sends, and the answer starts nothing."""

    def test_an_offered_message_is_answered_through_the_listener(self):
        db = self.db
        client = FakeClient()

        class Held:
            def __init__(self):
                self.db = db
                self._client = client
                self._logged_out = threading.Event()
                self._disconnected = threading.Event()
                self.errors, self.live_messages = [], 0
                self.offline_count = 0
                self.last_event_at = self.last_message_at = None
                self.stalled = False
                self.on_live = None

            def open(self):
                return self

            def wait_connected(self, timeout=60):
                return True

            def require_account(self):
                return {"id": OWN_PHONE}

            def account(self):
                return {"jid": OWN, "lid": OWN_LID, "device": OWN_DEVICE, "id": OWN_PHONE}

            def refresh_identities(self):
                return 0

            def close(self):
                pass
        held = Held()
        d = self.make()
        daemon = wa._ServiceDaemon(self.cfg, self.root, _settings(), launch_nonce="n",
                                   session_factory=lambda: held, dialogue=d)
        daemon.log = self.logs.append
        thread = threading.Thread(target=daemon.loop)
        with mock.patch.object(wa, "SERVICE_TICK", 0.01):
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and held.on_live is None:
                time.sleep(0.01)
            self.assertEqual(held.on_live, daemon.offer_live)
            msg = self.capture(_msg(chat_id=OWN, from_me=1, sender=OWN, sender_device=0,
                                    text="hi there"))
            held.on_live(msg)
            while time.monotonic() < deadline + 5 and daemon.sent < 1:
                time.sleep(0.02)
            sent = self.outgoing(OWN)
            self.assertEqual([(r["text"], r["delivery"]) for r in sent],
                             [("hello back", "sent")])
            # The answer's own copy, arriving back from the account, starts nothing.
            echo = _msg(chat_id=OWN, id=sent[0]["id"], from_me=1, sender=OWN,
                        sender_device=OWN_DEVICE, text="hello back")
            held.on_live(echo)
            echo_phone = dict(echo, sender_device=0)
            held.on_live(echo_phone)
            time.sleep(0.2)
            daemon.stop_requested.set()
            thread.join(5)
        self.assertEqual(len(self.run.calls), 1)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(daemon.health["dialogue"]["answered"], 1)

    def test_the_session_offers_each_live_capture_with_its_extras(self):
        from neonize.proto import Neonize_pb2 as proto
        e2e = wa._engine()["e2e"]
        user, _, server = GROUP.partition("@")
        event = proto.Message(
            Info=proto.MessageInfo(
                MessageSource=proto.MessageSource(
                    Chat=proto.JID(User=user, Server=server),
                    Sender=proto.JID(User=ALICE, Server="s.whatsapp.net", Device=3),
                    IsFromMe=False, IsGroup=True),
                ID="LIVE1", Timestamp=int(time.time() * 1000)),
            Message=e2e.Message(extendedTextMessage=e2e.ExtendedTextMessage(
                text="@15550000000 hi", contextInfo=e2e.ContextInfo(
                    mentionedJID=[OWN], stanzaID="Q9", participant=OWN))))
        session = wa.Session.__new__(wa.Session)
        session.db, session.live_messages, session.spool = self.db, 0, None
        offered = []
        session.on_live = offered.append
        session._on_message(event)
        self.assertEqual(len(offered), 1)
        got = offered[0]
        self.assertEqual((got["chat_id"], got["id"], got["sync_type"], got["text"]),
                         (GROUP, "LIVE1", "LIVE", "@15550000000 hi"))
        self.assertEqual((got["mentions"], got["quoted_participant"], got["quoted_id"],
                          got["sender_device"]), ([OWN], OWN, "Q9", 3))
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM whatsapp_messages WHERE account = %s AND id = 'LIVE1'",
            (self.db.account,)).fetchone()[0], 1)


class Wiring(unittest.TestCase):
    """How the listener comes by the runner, and that a listener whose settings
    admit nobody is the listener it was."""

    def test_the_runner_pin_is_one(self):
        self.assertEqual(wa._service_module_pin(), profiles.RUNNER_PIN)
        header = (wa._service_bundle_dir() / "service" / "profiles.py").read_text()
        self.assertIn(f'"{profiles.RUNNER_PIN}"', header.split("# ///")[1])
        cli_header = Path(_cli.CLI_PATH).read_text().split("# ///")[1]
        self.assertNotIn("callva-harness-runner", cli_header)

    def _run(self, settings, *, importable):
        root = Path(tempfile.mkdtemp())
        cfg = {"id": "c1", "home": tempfile.mkdtemp(), "account_key": OWN_PHONE,
               "engine": wa.ENGINE_INHOUSE}
        with mock.patch.object(wa, "_service_prepare", return_value=(root, settings, cfg)), \
                mock.patch.object(wa, "_runner_importable", return_value=importable), \
                mock.patch.object(wa, "_project_id_state_at", return_value={"id": "prj_x"}), \
                mock.patch.object(wa.os, "execvpe") as execvpe, \
                mock.patch.object(wa, "_build_dialogue") as build, \
                mock.patch.object(wa._ServiceDaemon, "run", return_value=0) as run, \
                mock.patch.object(wa.sys, "exit") as leave:
            execvpe.side_effect = SystemExit(0)
            try:
                wa._cmd_service_run(None)
            except SystemExit:
                pass
        return execvpe, build, run, leave

    def test_a_listener_that_admits_nobody_needs_no_runner(self):
        execvpe, build, run, _ = self._run({"environment": "x"}, importable=False)
        execvpe.assert_not_called()
        build.assert_not_called()
        run.assert_called_once()

    def test_a_dialogue_runs_the_listener_again_with_the_runner(self):
        with mock.patch.object(wa.shutil, "which", return_value="/usr/bin/uv"), \
                mock.patch.dict(os.environ, {wa.SERVICE_RUNNER_EXEC_ENV: ""}):
            execvpe, build, _run, _ = self._run(_settings(), importable=False)
        command = execvpe.call_args[0][1]
        self.assertEqual(command[:6], ["/usr/bin/uv", "run", "--quiet", "--with",
                                       profiles.RUNNER_PIN, "--script"])
        self.assertEqual(command[-4:], ["service", "run", "--connection", "c1"])
        self.assertEqual(execvpe.call_args[0][2][wa.SERVICE_RUNNER_EXEC_ENV], "1")
        build.assert_not_called()

    def test_a_listener_with_the_runner_builds_its_dialogue(self):
        execvpe, build, run, _ = self._run(_settings(), importable=True)
        execvpe.assert_not_called()
        build.assert_called_once()

    def test_start_checks_the_profiles_before_launching(self):
        root = Path(tempfile.mkdtemp())
        cfg = {"id": "c1", "home": tempfile.mkdtemp(), "account_key": OWN_PHONE,
               "engine": wa.ENGINE_INHOUSE}
        bad = [{"name": "whatsapp-claude", "ok": False, "error": "it broke"}]
        with mock.patch.object(wa, "_service_prepare", return_value=(root, _settings(), cfg)), \
                mock.patch.object(wa, "_project_id_state_at", return_value={"id": "prj_x"}), \
                mock.patch.object(wa, "_profile_check", return_value=bad), \
                mock.patch.object(wa.subprocess, "Popen") as popen:
            with self.assertRaises(wa._Refusal) as caught:
                wa._cmd_service_start(None)
        self.assertEqual(caught.exception.code, "service_profile_invalid")
        self.assertIn("defaults.profile", caught.exception.message)
        popen.assert_not_called()

    def test_without_a_project_id_a_dialogue_is_refused(self):
        with mock.patch.object(wa, "_project_id_state_at", return_value={"id": None}):
            with self.assertRaises(wa._Refusal) as caught:
                wa._service_project_id(Path("/p"))
        self.assertEqual(caught.exception.code, "project_id_missing")

    def test_the_profiles_script_is_asked_where_the_runner_is_absent(self):
        done = types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"ok": True, "profiles": [{"name": "whatsapp-claude", "ok": True}]}))
        with mock.patch.object(wa, "_runner_importable", return_value=False), \
                mock.patch.object(wa.shutil, "which", return_value="/usr/bin/uv"), \
                mock.patch.object(wa.subprocess, "run", return_value=done) as ran, \
                mock.patch.object(wa, "_project_capabilities_dir",
                                  return_value=Path("/p/capabilities")):
            rows = wa._profile_check(Path("/p"), ["whatsapp-claude"])
        self.assertEqual(rows, [{"name": "whatsapp-claude", "ok": True}])
        command = ran.call_args[0][0]
        self.assertEqual(command[:4], ["/usr/bin/uv", "run", "--quiet", "--script"])
        self.assertTrue(command[4].endswith("service/profiles.py"))
        self.assertEqual(command[5:7], ["check", "whatsapp-claude"])
        self.assertIn("/p/capabilities/whatsapp/service/profiles", command)

    def test_doctor_reports_the_dialogue_and_each_profile(self):
        root = Path(tempfile.mkdtemp())
        cfg = {"id": "c1", "home": tempfile.mkdtemp(), "account_key": OWN_PHONE,
               "engine": wa.ENGINE_INHOUSE}
        rows = [{"name": "whatsapp-claude", "ok": True, "harness": "claude",
                 "model": "opus", "source": "folder", "path": "/b/whatsapp-claude.toml"},
                {"name": "claude-read-sandboxed", "ok": False, "error": "does not fit"}]
        settings = _settings(allowed_users={ALICE: {"profile": "claude-read-sandboxed"}})
        db = mock.Mock()
        with mock.patch.object(wa, "_service_project_root", return_value=root), \
                mock.patch.object(wa, "_service_settings", return_value=settings), \
                mock.patch.object(wa, "_service_cfg", return_value=cfg), \
                mock.patch.object(wa, "_open_store", return_value=db), \
                mock.patch.object(wa, "_store_location", return_value="pg"), \
                mock.patch.object(wa, "_session_account",
                                  return_value=(OWN_PHONE, OWN, "Owner")), \
                mock.patch.object(wa, "_project_id_state_at", return_value={"id": "prj_x"}), \
                mock.patch.object(wa, "_profile_check", return_value=rows), \
                mock.patch.object(wa, "_cmd_service_status",
                                  return_value={"state": "stopped", "running": False}), \
                mock.patch.object(wa, "_account_lock_free", return_value=True):
            report, fail = wa._cmd_service_doctor(None)
        checks = {c["item"]: c for c in report["checks"]}
        self.assertTrue(checks["dialogue"]["ok"])
        self.assertTrue(checks["profile whatsapp-claude"]["ok"])
        self.assertFalse(checks["profile claude-read-sandboxed"]["ok"])
        self.assertIn(f"allowed_users.{ALICE}.profile",
                      checks["profile claude-read-sandboxed"]["detail"])
        self.assertEqual(fail, 6)


class Pieces(unittest.TestCase):
    def test_the_reply_marker_cut(self):
        self.assertEqual(dialogue.cut_at_reply_marker("plan\n=== REPLY ===\nhi"), "hi")
        self.assertEqual(dialogue.cut_at_reply_marker("a\n=== REPLY ===\nb\n=== REPLY ===\nc"),
                         "c")
        self.assertEqual(dialogue.cut_at_reply_marker("just text"), "just text")

    def test_splitting_keeps_paragraphs_whole(self):
        text = "one\n\ntwo\n\nthree"
        self.assertEqual(dialogue.split_reply(text, limit=8), ["one\n\ntwo", "three"])
        self.assertEqual(dialogue.split_reply("x" * 20, limit=8), ["x" * 8, "x" * 8, "x" * 4])
        self.assertEqual(dialogue.split_reply("   "), [])


if __name__ == "__main__":
    unittest.main()
