#!/usr/bin/env python3
"""Voice notes in the assistant service: which ones are transcribed, that the
transcription runs once through the listener's own connection before the turn,
that the turn reads the words, and that every failure reaches the turn as one.

The engine's download and Deepgram are stood in for; the store-backed cases
need the throwaway Postgres named by WHATSAPP_TEST_DSN and skip without one.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_dialogue as td  # noqa: E402  (beside this file on sys.path)

wa, dialogue, schema = td.wa, td.dialogue, td.schema
ALICE, ALICE_JID, GROUP, OWN = td.ALICE, td.ALICE_JID, td.GROUP, td.OWN
KEY = "dg-test-key"
SPOKEN = "please book the room for friday"
DM_ECHO = f"{dialogue.VOICE_ECHO_DIRECT}\n> {SPOKEN}"
FAILED = dialogue.VOICE_ECHO_FAILED


def _voice(**over) -> dict:
    """A voice note as the listener offers it, its download material included."""
    base = td._msg(kind="audioMessage", text=None)
    base.update({"mimetype": "audio/ogg; codecs=opus", "is_voice": 1, "seconds": 3,
                 "direct_path": "/v/t62.7117-24/voice", "media_key": b"k" * 32,
                 "file_enc_sha256": b"e" * 32, "file_sha256": b"s" * 32,
                 "file_length": 4096})
    base.update(over)
    return base


class FakeSession:
    """The listener's held session, as far as a download goes."""

    def __init__(self, answer=None):
        self.answer = answer or {"state": "fetched", "data": b"OggS voice bytes"}
        self.fetched: list[str] = []

    def fetch_media(self, row):
        self.fetched.append(row["id"])
        return dict(self.answer)


class FakeDeepgram:
    def __init__(self, text="please book the room for friday", *, error=None,
                 gate=None):
        self.text, self.error, self.gate = text, error, gate
        self.calls: list[tuple] = []

    def __call__(self, path, key, model, timeout=300):
        self.calls.append((Path(path).name, key, model, Path(path).read_bytes()))
        if self.gate is not None:
            self.gate.wait(10)
        if self.error is not None:
            raise self.error
        return {"text": self.text, "language": "en", "confidence": 0.97,
                "model": model, "provider": "deepgram",
                "transcribed_at": "2026-10-08T10:00:00+00:00"}


# ── Settings ────────────────────────────────────────────────────────────────


class Settings(unittest.TestCase):
    def test_the_mode_is_read_at_every_level_and_refused_when_unknown(self):
        schema.validate_settings(td._settings(
            allowed_users={ALICE: {"voice_transcription": "off"}},
            allowed_groups={GROUP: {"voice_transcription": "auto"}},
            defaults={"voice_transcription": "addressed"}))
        for document, path in [
                ({"defaults": {"voice_transcription": "on"}},
                 "settings.defaults.voice_transcription"),
                ({"allowed_users": {ALICE: {"voice_transcription": True}}},
                 f"settings.allowed_users.{ALICE}.voice_transcription"),
                ({"allowed_groups": {GROUP: {"voice_transcription": "all"}}},
                 f"settings.allowed_groups.{GROUP}.voice_transcription")]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, f"{path}: must be one of: "
                                            "addressed, auto, off"):
                    schema.validate_settings(document)

    def test_start_doctor_and_reload_refuse_a_bad_mode_through_the_same_walk(self):
        bad = {"defaults": {"voice_transcription": "sometimes"}}
        with mock.patch.object(wa, "_service_settings", return_value=bad):
            with self.assertRaises(wa._Refusal) as caught:
                wa._checked_service_settings()
            self.assertEqual(caught.exception.code, "service_settings_invalid")
            with mock.patch.object(wa, "_running_owner") as owner:
                with self.assertRaises(wa._Refusal):
                    wa._cmd_service_reload(None, 1.0)
            owner.assert_not_called()

    def test_the_levels_resolve_narrowest_first_with_addressed_by_default(self):
        policy = dialogue.Policy(td._settings(
            allowed_users={ALICE: {"voice_transcription": "off"}, td.BOB: {}},
            allowed_groups={GROUP: {"voice_transcription": "auto"}}),
            td.profiles.DEFAULT_PROFILE)
        self.assertEqual(policy.voice_mode(ALICE_JID, ALICE), "off")
        self.assertEqual(policy.voice_mode(td.BOB_JID, td.BOB), "addressed")
        self.assertEqual(policy.voice_mode(GROUP, td.BOB), "auto")
        self.assertEqual(policy.voice_mode(GROUP, td.BOB,
                                           {"voice_transcription": "off"}), "off")

    def test_the_scopes_that_transcribe_are_named(self):
        self.assertEqual(schema.voice_scopes({}), [])
        self.assertEqual(
            schema.voice_scopes(td._settings(
                allowed_users={ALICE: {}, td.BOB: {"voice_transcription": "off"}},
                allowed_groups={GROUP: {}})),
            [f"allowed_users.{ALICE}", f"allowed_groups.{GROUP}"])
        self.assertEqual(schema.voice_scopes(td._settings(
            defaults={"voice_transcription": "off"})), [])
        self.assertEqual(schema.voice_scopes(td._settings(
            direct_messages={"mode": "off"},
            allowed_groups={GROUP: {"voice_transcription": "off"}})), [])

    def _doctor(self, settings, key):
        root = Path(td.tempfile.mkdtemp())
        cfg = {"id": "c1", "home": td.tempfile.mkdtemp(), "account_key": td.OWN_PHONE,
               "engine": wa.ENGINE_INHOUSE}
        rows = [{"name": "whatsapp-claude", "ok": True, "harness": "claude",
                 "model": "opus", "source": "bundle", "path": "/b/whatsapp-claude.toml"},
                {"name": "whatsapp-job-claude", "ok": True, "harness": "claude",
                 "model": "opus", "source": "bundle", "path": "/b/j.toml"}]
        with mock.patch.object(wa, "_service_project_root", return_value=root), \
                mock.patch.object(wa, "_service_settings", return_value=settings), \
                mock.patch.object(wa, "_service_cfg", return_value=cfg), \
                mock.patch.object(wa, "_open_store", return_value=mock.Mock()), \
                mock.patch.object(wa, "_store_location", return_value="pg"), \
                mock.patch.object(wa, "_session_account",
                                  return_value=(td.OWN_PHONE, OWN, "Owner")), \
                mock.patch.object(wa, "_project_id_state_at", return_value={"id": "prj_x"}), \
                mock.patch.object(wa, "_profile_check", return_value=rows), \
                mock.patch.object(wa, "_resolve_env_key",
                                  return_value=(key, "env" if key else None, None)), \
                mock.patch.object(wa, "_cmd_service_status",
                                  return_value={"state": "stopped", "running": False}), \
                mock.patch.object(wa, "_account_lock_free", return_value=True):
            return wa._cmd_service_doctor(None)

    def test_doctor_warns_without_a_key_where_a_chat_transcribes(self):
        report, fail = self._doctor(td._settings(), None)
        checks = {c["item"]: c for c in report["checks"]}
        voice = checks["voice transcription"]
        self.assertTrue(voice["ok"])
        self.assertTrue(voice["warning"])
        self.assertIn("DEEPGRAM_API_KEY is not set", voice["detail"])
        self.assertIn(f"allowed_groups.{GROUP}", voice["detail"])
        self.assertNotIn(KEY, str(report))
        self.assertEqual(report["warnings"], [voice["detail"]])
        self.assertEqual(fail, 0)

    def test_doctor_is_quiet_with_a_key_or_with_voice_off(self):
        report, fail = self._doctor(td._settings(), KEY)
        voice = {c["item"]: c for c in report["checks"]}["voice transcription"]
        self.assertTrue(voice["ok"])
        self.assertNotIn("warning", voice)
        self.assertNotIn(KEY, str(report))
        report, _ = self._doctor(td._settings(defaults={"voice_transcription": "off"}),
                                 None)
        voice = {c["item"]: c for c in report["checks"]}["voice transcription"]
        self.assertEqual(voice["detail"], "off in every admitted chat")
        self.assertNotIn("warnings", report)
        self.assertEqual(fail, 0)


# ── The gate ────────────────────────────────────────────────────────────────


class Gate(unittest.TestCase):
    def judge(self, msg, settings=None, **gate):
        return td._gate(settings, **gate).judge(msg)

    def test_a_direct_voice_note_is_transcribed_unless_off(self):
        verdict = self.judge(_voice())
        self.assertEqual((verdict["admit"], verdict["kind"], verdict["voice"]),
                         (True, "turn", "addressed"))
        off = self.judge(_voice(), td._settings(defaults={"voice_transcription": "off"}))
        self.assertTrue(off["admit"])
        self.assertNotIn("voice", off)

    def test_a_group_voice_note_under_addressed_needs_a_quote_of_ours(self):
        quoted = self.judge(_voice(chat_id=GROUP, quoted_id="OURS"), ours={"OURS"})
        self.assertEqual((quoted["admit"], quoted["voice"]), (True, "addressed"))
        plain = self.judge(_voice(chat_id=GROUP))
        self.assertEqual((plain["admit"], plain["reason"]), (False, "unaddressed"))
        self.assertNotIn("voice", plain)

    def test_a_group_voice_note_under_auto_is_heard_and_judged_on_its_words(self):
        settings = td._settings(allowed_groups={GROUP: {
            "aliases": ["Helper"], "voice_transcription": "auto"}})
        verdict = self.judge(_voice(chat_id=GROUP), settings)
        self.assertEqual((verdict["admit"], verdict["voice"]), (True, "ambient"))
        quoted = self.judge(_voice(chat_id=GROUP, quoted_id="OURS"), settings,
                            ours={"OURS"})
        self.assertEqual(quoted["voice"], "addressed")

    def test_a_chats_set_override_decides_before_the_settings(self):
        gate = td._gate()
        self.assertFalse(gate.judge(_voice(chat_id=GROUP))["admit"])
        gate.voice_mode = lambda chat, phone: "auto"
        self.assertEqual(gate.judge(_voice(chat_id=GROUP))["voice"], "ambient")


# ── Through the dialogue ────────────────────────────────────────────────────


@td._cli.needs_store
@td.needs_runner
class Voice(td.DialogueCase):
    def setUp(self):
        super().setUp()
        self.deepgram = FakeDeepgram()
        for name, value in (("_deepgram_transcribe", self.deepgram),
                            ("_resolve_env_key", lambda key: (KEY, "env", None))):
            patcher = mock.patch.object(wa, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self, settings=None, run=None, session=None):
        d = super().make(settings, run)
        self.session = session or FakeSession()
        d.session = self.session
        return d

    def capture(self, msg):
        row = {k: msg.get(k) for k in wa._MESSAGE_COLUMNS if k in msg}
        row["captured_at"] = wa._iso_now()
        wa._write_live(self.db, row)
        return msg

    def voice(self, d, **over):
        msg = self.capture(_voice(**over))
        return msg, d.offer(msg)

    def enrichment(self, msg):
        return self.db.execute(
            "SELECT * FROM whatsapp_enrichment WHERE account = %s AND chat_id = %s"
            " AND id = %s", (self.db.account, msg["chat_id"], msg["id"])).fetchone()

    def test_a_direct_voice_note_is_answered_from_its_transcript_without_a_job(self):
        d = self.make()
        msg, verdict = self.voice(d)
        self.assertEqual(verdict["voice"], "addressed")
        self.settle(d)
        self.assertEqual(self.session.fetched, [msg["id"]])
        self.assertEqual(len(self.deepgram.calls), 1)
        name, key, model, data = self.deepgram.calls[0]
        self.assertEqual((key, model, data), (KEY, "nova-3", b"OggS voice bytes"))
        prompt = self.run.calls[0]["prompt"]
        request = prompt.split("--- Current request ---")[1].split("--- Conversation")[0]
        self.assertIn("[voice] please book the room for friday", request)
        self.assertNotIn("[audioMessage]", prompt)
        sent = self.outgoing(ALICE_JID)
        self.assertEqual([r["text"] for r in sent], [DM_ECHO, "hello back"])
        self.assertIsNone(sent[0]["quoted_id"])
        self.assertLess(sent[0]["requested_at"], sent[1]["requested_at"])
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM whatsapp_jobs WHERE account = %s",
            (self.db.account,)).fetchone()[0], 0)
        row = self.enrichment(msg)
        self.assertEqual((row["transcript"], row["transcript_provider"],
                          row["transcript_model"], row["media_state"]),
                         ("please book the room for friday", "deepgram", "nova-3",
                          "fetched"))
        self.assertTrue(Path(row["media_path"]).is_file())
        self.assertEqual(d.summary()["transcribed"], 1)

    def test_the_cli_reads_the_transcript_the_listener_stored(self):
        d = self.make()
        msg, _ = self.voice(d)
        self.settle(d)
        envelope = [m for m in wa._read_messages(self.db, ALICE_JID, limit=None,
                                                  from_ts=None, to_ts=None)
                    if m["id"] == msg["id"]][0]
        self.assertEqual(envelope["type"], "ptt")
        self.assertEqual(envelope["transcription"]["text"],
                         "please book the room for friday")
        self.assertEqual(envelope["transcription"]["provider"], "deepgram")

    def test_the_owners_voice_note_in_the_self_chat_is_transcribed(self):
        d = self.make()
        msg, verdict = self.voice(d, chat_id=OWN, from_me=1, sender=OWN,
                                  sender_device=0)
        self.assertEqual(verdict["voice"], "addressed")
        self.settle(d)
        self.assertIn("[voice] please book", self.run.calls[0]["prompt"])

    def test_a_redelivered_voice_note_is_transcribed_and_answered_once(self):
        gate = threading.Event()
        self.deepgram.gate = gate
        d = self.make()
        msg, first = self.voice(d)
        again = d.offer(dict(msg))
        threads = [threading.Thread(target=d.offer, args=(dict(msg),)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        gate.set()
        self.settle(d)
        self.assertTrue(first["admit"])
        self.assertEqual(again["reason"], "processed")
        self.assertEqual(len(self.deepgram.calls), 1)
        self.assertEqual(self.session.fetched, [msg["id"]])
        self.assertEqual(len(self.run.calls), 1)
        self.assertEqual([r["text"] for r in self.outgoing(ALICE_JID)],
                         [DM_ECHO, "hello back"])

    def test_the_reservation_is_written_before_the_transcription_starts(self):
        gate = threading.Event()
        self.deepgram.gate = gate
        d = self.make()
        msg, _ = self.voice(d)
        try:
            self.assertTrue(self.drive(d, lambda: self.deepgram.calls, 5))
            row = self.register_row(ALICE_JID)
            self.assertIn(msg["id"], td.json.loads(row["processed"])
                          if isinstance(row["processed"], str) else row["processed"])
            self.assertEqual(self.run.calls, [])
        finally:
            gate.set()
        self.settle(d)

    def test_a_restart_does_not_transcribe_or_answer_again(self):
        d = self.make()
        msg, _ = self.voice(d)
        self.settle(d)
        again = self.make()
        self.assertEqual(again.offer(dict(msg))["reason"], "processed")
        self.settle(again)
        self.assertEqual(len(self.deepgram.calls), 1)

    def test_a_chat_keeps_its_order_while_a_voice_note_is_transcribed(self):
        gate = threading.Event()
        self.deepgram.gate = gate
        d = self.make()
        self.voice(d, ts=int(time.time()) - 6)
        self.arrive(d, text="and one more thing")
        time.sleep(0.2)
        for _ in range(20):
            d.tick(None)
        self.assertEqual(self.run.calls, [])
        gate.set()
        self.settle(d)
        requests = [c["prompt"].split("--- Current request ---")[1].split(
            "--- Conversation")[0] for c in self.run.calls]
        self.assertEqual(len(requests), 2)
        self.assertIn("[voice] please book", requests[0])
        self.assertIn("and one more thing", requests[1])

    def test_with_voice_off_a_voice_note_is_answered_as_before(self):
        d = self.make(td._settings(defaults={"debounce": 0, "max_age": 600,
                                             "voice_transcription": "off"}))
        self.voice(d)
        self.settle(d)
        self.assertEqual(self.deepgram.calls, [])
        self.assertEqual(self.session.fetched, [])
        request = self.run.calls[0]["prompt"].split("--- Current request ---")[1]
        self.assertIn("[audioMessage]", request)

    # -- groups ---------------------------------------------------------------

    def ours(self, chat):
        row = wa._queue_outgoing(self.db, {"chat_id": chat, "text": "earlier answer",
                                           "reply_to": None, "mentions": []})
        return row["local_id"]

    def test_addressed_transcribes_only_a_group_voice_note_that_quotes_us(self):
        d = self.make()
        quoted = self.ours(GROUP)
        plain, verdict = self.voice(d, chat_id=GROUP)
        self.assertEqual(verdict["reason"], "unaddressed")
        msg, verdict = self.voice(d, chat_id=GROUP, quoted_id=quoted)
        self.assertEqual(verdict["voice"], "addressed")
        self.settle(d)
        self.assertEqual(self.session.fetched, [msg["id"]])
        self.assertEqual(len(self.deepgram.calls), 1)
        self.assertEqual(len(self.run.calls), 1)
        self.assertIn("[voice] please book", self.run.calls[0]["prompt"])
        self.assertIsNone(self.enrichment(plain))
        self.assertEqual([(r["text"], r["quoted_id"]) for r in self.outgoing(GROUP)
                          if r["local_id"] != quoted],
                         [(f"> {SPOKEN}", msg["id"]), ("hello back", msg["id"])])

    def test_auto_hears_every_group_voice_note_and_answers_one_that_names_us(self):
        settings = td._settings(allowed_groups={GROUP: {
            "name": "Team", "aliases": ["Helper"], "voice_transcription": "auto"}})
        d = self.make(settings)
        self.deepgram.text = "helper, what is on the agenda?"
        named, verdict = self.voice(d, chat_id=GROUP)
        self.assertEqual(verdict["voice"], "ambient")
        self.settle(d)
        self.assertEqual(len(self.run.calls), 1)
        self.assertIn("[voice] helper, what is on the agenda?", self.run.calls[0]["prompt"])
        sent = self.outgoing(GROUP)
        self.assertEqual([(r["text"], r["quoted_id"]) for r in sent[-2:]],
                         [("> helper, what is on the agenda?", named["id"]),
                          ("hello back", named["id"])])
        self.deepgram.text = "see you all tomorrow"
        unnamed, _ = self.voice(d, chat_id=GROUP)
        self.settle(d)
        self.assertEqual(len(self.deepgram.calls), 2)
        self.assertEqual(len(self.run.calls), 1)
        self.assertEqual([(r["text"], r["quoted_id"]) for r in self.outgoing(GROUP)[-1:]],
                         [("> see you all tomorrow", unnamed["id"])])
        self.assertEqual(self.enrichment(unnamed)["transcript"], "see you all tomorrow")
        self.assertEqual(d.summary()["voice_unaddressed"], 1)

    def test_set_voice_transcription_changes_one_chat(self):
        d = self.make()
        msg, _ = self.arrive(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0,
                             text="/set voice-transcription off")
        self.settle(d)
        self.assertEqual(self.outgoing(OWN)[-1]["text"], "ok, voice-transcription = off")
        self.voice(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0)
        self.settle(d)
        self.assertEqual(self.deepgram.calls, [])
        self.arrive(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0,
                    text="/set voice-transcription sometimes")
        self.settle(d)
        self.assertIn("nope: voice-transcription must be", self.outgoing(OWN)[-1]["text"])
        self.arrive(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0,
                    text="/set voice-transcription default")
        self.settle(d)
        self.assertEqual(self.outgoing(OWN)[-1]["text"],
                         "ok, voice-transcription = default (addressed effective)")
        self.arrive(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0,
                    text="/set help")
        self.settle(d)
        self.assertIn("voice-transcription <off|addressed|auto>",
                      self.outgoing(OWN)[-1]["text"])

    # -- failures -------------------------------------------------------------

    def failed_prompt(self, d):
        self.settle(d)
        self.assertEqual(len(self.run.calls), 1)
        return self.run.calls[0]["prompt"].split("--- Current request ---")[1].split(
            "--- Conversation")[0]

    def test_expired_media_reaches_the_turn_as_a_failure(self):
        session = FakeSession({"state": "expired", "error": "status code 410"})
        d = self.make(session=session)
        msg, _ = self.voice(d)
        request = self.failed_prompt(d)
        self.assertIn("[voice note - transcription failed: the audio could not be "
                      "fetched: expired (status code 410)]", request)
        self.assertEqual(self.deepgram.calls, [])
        self.assertEqual(self.outgoing(ALICE_JID)[0]["text"],
                         f"{dialogue.VOICE_ECHO_DIRECT}\n> {FAILED}")
        row = self.enrichment(msg)
        self.assertEqual(row["media_state"], "expired")
        self.assertIn("expired", row["transcript_error"])
        d.offer(dict(msg))
        self.settle(d)
        self.assertEqual(session.fetched, [msg["id"]])
        self.assertEqual(len(self.run.calls), 1)
        self.assertEqual(len(self.outgoing(ALICE_JID)), 2)

    def test_a_deepgram_error_reaches_the_turn_as_a_failure(self):
        self.deepgram.error = wa.HttpFailure(5, "server_error", "Deepgram returned 503")
        d = self.make()
        msg, _ = self.voice(d)
        request = self.failed_prompt(d)
        self.assertIn("[voice note - transcription failed: Deepgram: Deepgram "
                      "returned 503]", request)
        self.assertEqual(len(self.deepgram.calls), 1)
        self.assertEqual(self.enrichment(msg)["transcript_error"],
                         "Deepgram: Deepgram returned 503")
        self.assertEqual(d.summary()["transcription_failed"], 1)
        self.assertEqual([r["text"] for r in self.outgoing(ALICE_JID)],
                         [f"{dialogue.VOICE_ECHO_DIRECT}\n> {FAILED}",
                          "hello back"])

    def test_without_a_key_the_turn_is_told_so(self):
        d = self.make()
        with mock.patch.object(wa, "_resolve_env_key", lambda key: (None, None, None)):
            msg, _ = self.voice(d)
            request = self.failed_prompt(d)
        self.assertIn("[voice note - transcription failed: no Deepgram key; "
                      "DEEPGRAM_API_KEY is not set]", request)
        self.assertEqual(self.session.fetched, [])
        self.assertEqual(self.deepgram.calls, [])

    def test_a_transcription_that_raises_still_reaches_the_turn(self):
        d = self.make()
        with mock.patch.object(wa, "_transcribe_live_voice",
                               side_effect=RuntimeError("boom")):
            self.voice(d)
            request = self.failed_prompt(d)
        self.assertIn("[voice note - transcription failed: RuntimeError: boom]", request)

    def test_an_unaddressed_auto_note_that_fails_is_echoed_and_not_answered(self):
        settings = td._settings(allowed_groups={GROUP: {
            "aliases": ["Helper"], "voice_transcription": "auto"}})
        self.deepgram.error = wa.HttpFailure(5, "timeout", "request timed out")
        d = self.make(settings)
        msg, _ = self.voice(d, chat_id=GROUP)
        self.settle(d)
        self.assertEqual(self.run.calls, [])
        self.assertEqual([(r["text"], r["quoted_id"]) for r in self.outgoing(GROUP)],
                         [(f"> {FAILED}", msg["id"])])

    # -- the tail -------------------------------------------------------------

    def delivered(self):
        """Mark every queued row sent, as the listener would once it sent it."""
        self.db.execute(
            "UPDATE whatsapp_messages SET delivery = 'sent', id = 'WA' || local_id,"
            " ts = requested_at - interval '8 seconds'"
            " WHERE account = %s AND delivery = 'pending'", (self.db.account,))

    def test_an_echo_is_never_the_assistants_words_nor_a_second_copy(self):
        settings = td._settings(allowed_groups={GROUP: {
            "name": "Team", "aliases": ["Helper"], "voice_transcription": "auto"}})
        d = self.make(settings)
        self.deepgram.text = "see you all tomorrow"
        heard, _ = self.voice(d, chat_id=GROUP, ts=int(time.time()) - 20)
        self.settle(d)
        self.assertEqual(len(self.outgoing(GROUP)), 1)
        self.delivered()
        self.arrive(d, chat_id=GROUP, text="Helper, who is coming tomorrow?")
        self.settle(d)
        self.deepgram.text = SPOKEN
        self.voice(d, ts=int(time.time()) - 20)
        self.settle(d)
        self.delivered()
        self.arrive(d, text="and the time?")
        self.settle(d)
        group_tail = self.run.calls[0]["prompt"].split("--- Conversation ---")[1]
        self.assertEqual(group_tail.count("see you all tomorrow"), 1)
        self.assertIn(f"#{heard['id']}] Alice: [voice] see you all tomorrow", group_tail)
        self.assertNotIn("> see you all tomorrow", group_tail)
        direct_tail = self.run.calls[-1]["prompt"].split("--- Conversation ---")[1]
        self.assertEqual(direct_tail.count(SPOKEN), 1)
        self.assertIn(f"Alice: [voice] {SPOKEN}", direct_tail)
        self.assertNotIn(dialogue.VOICE_ECHO_DIRECT, direct_tail)
        self.assertIn("Helper (you): hello back", direct_tail)

    def test_the_echo_quotes_every_line(self):
        self.assertEqual(dialogue.voice_echo("one\n\ntwo", direct=False), "> one\n>\n> two")
        self.assertEqual(dialogue.voice_echo("one", direct=True),
                         f"{dialogue.VOICE_ECHO_DIRECT}\n> one")


    def test_the_tail_shows_older_voice_notes_by_their_transcript(self):
        for mode in ("addressed", "off"):
            with self.subTest(mode=mode):
                d = self.make(td._settings(defaults={"debounce": 0, "max_age": 600,
                                                     "voice_transcription": mode}))
                old = self.capture(_voice(ts=int(time.time()) - 3000))
                wa._record_transcript(self.db, ALICE_JID, old["id"],
                                      {"text": "the older words", "provider": "deepgram"})
                lost = self.capture(_voice(ts=int(time.time()) - 2000))
                wa._record_transcript(self.db, ALICE_JID, lost["id"],
                                      {"error": "the audio is expired"})
                bare = self.capture(_voice(ts=int(time.time()) - 1000))
                self.arrive(d, text="what did I say?")
                self.settle(d)
                tail = self.run.calls[0]["prompt"].split("--- Conversation ---")[1]
                self.assertIn(f"#{old['id']}] Alice: [voice] the older words", tail)
                self.assertIn(f"#{lost['id']}] Alice: [voice note - transcription "
                              "failed: the audio is expired]", tail)
                self.assertIn(f"#{bare['id']}] Alice: [audioMessage]", tail)


if __name__ == "__main__":
    unittest.main()
