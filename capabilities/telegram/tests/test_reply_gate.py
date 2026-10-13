#!/usr/bin/env python3
"""The reply gate: per-group triggers, the gate block, batching, the gate's
input and decision, acting on it, shadow mode, the record, the gate profiles,
and what the full turn is told.

The daemon cases drive a live session with a fake client, as the assistant
service suite does; a gate run on claude or codex goes through the daemon's
`harness_run` seam, answered by a fake that tells a gate run (its profile sets
`output_schema`) from a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import result  # noqa: E402
from test_assistant_service import (  # noqa: E402
    TELEGRAM_DIR,
    Event,
    FakeClient,
    Message,
    import_daemon,
    settings,
    successful_result,
    wait_until,
)

SERVICE_DIR = TELEGRAM_DIR / "service"
sys.path.insert(0, str(SERVICE_DIR))
import gate  # noqa: E402

GROUP = -200
ME = 42


def group_settings(*, gate_block=None, triggers=None, profile="stub", debounce=0.05,
                   **group):
    service = settings(profile=profile, debounce=debounce)
    policy = {"name": "Room", "aliases": ["Helper"], **group}
    if gate_block is not None:
        policy["gate"] = gate_block
    if triggers is not None:
        policy["triggers"] = triggers
    service["allowed_groups"] = {str(GROUP): policy}
    return service


def group_message(message_id, text="hello", *, sender=777, mentioned=False, reply_to=None,
                  date=None):
    message = Message(message_id, text=text)
    message.sender_id = sender
    message.mentioned = mentioned
    message.date = date or datetime.now(timezone.utc)
    if reply_to is not None:
        message.is_reply = True
        message.reply_to_msg_id = reply_to.id
        message._reply_message = reply_to
    return message


def group_event(message):
    event = Event(message, chat_id=GROUP)
    event.is_private = False
    return event


def own_message(message_id, text, *, date=None):
    """A message the account itself sent, as the client lists it."""
    return SimpleNamespace(id=message_id, text=text, raw_text=text, message=text, out=True,
                           voice=False, audio=False, video_note=False, photo=False,
                           file=None, is_reply=False, sender_id=ME,
                           date=date or datetime.now(timezone.utc))


class GateClient(FakeClient):
    """A client that lists what it was given, records the chat actions it was
    asked to show and when each ended, and answers the chat's reactions."""

    def __init__(self, messages=(), *, available="all"):
        super().__init__(messages)
        self.actions = []
        self.available = available

    async def get_me(self):
        return SimpleNamespace(first_name="Helper", last_name=None, id=ME,
                               username="helper_bot")

    async def get_messages(self, chat, limit=None):
        # Newest first, as Telegram lists a chat's history.
        listed = await super().get_messages(chat, limit=None)
        listed = sorted(listed, key=lambda m: m.id, reverse=True)
        return listed[:limit] if limit else listed

    @asynccontextmanager
    async def action(self, chat, kind):
        entry = {"chat": chat, "kind": kind, "ended": False}
        self.actions.append(entry)
        try:
            yield
        finally:
            entry["ended"] = True


def gate_records(daemon):
    """Every gate record the daemon logged, parsed."""
    out = []
    for line in daemon._test_logs:
        head, sep, body = str(line).partition(": gate {")
        if sep:
            out.append(json.loads("{" + body))
    return out


class Answering:
    """The harness seam: a gate run (its profile sets an output schema) gets
    the next of `decisions`; a turn gets `turn`. Every call is recorded."""

    def __init__(self, *decisions, turn="the turn's answer", seconds=0.0):
        self.decisions = list(decisions)
        self.turn = turn
        self.seconds = seconds
        self.gate_calls, self.turn_calls = [], []

    def __call__(self, prompt, profile, cwd, **kwargs):
        call = {"prompt": prompt, "profile": profile, "cwd": cwd,
                "cwd_listing": sorted(p.name for p in Path(cwd).iterdir()), **kwargs}
        if profile.output_schema is not None:
            self.gate_calls.append(call)
            if self.seconds:
                import time
                time.sleep(self.seconds)
            answer = self.decisions.pop(0) if self.decisions else {
                "action": "ignore", "target_message_id": None, "emoji": None, "reason": "-"}
            return result(profile.harness, answer=json.dumps(answer), session_id=None,
                          model=profile.model, tokens={"input": 100, "output": 20})
        self.turn_calls.append(call)
        return result(profile.harness, answer=self.turn, model=profile.model)


class Session:
    """One daemon session over a GateClient, with the gate's harness answered."""

    def __init__(self, case, td, service, *, messages=(), answering=None, available="all",
                 reactions=None):
        self.daemon = import_daemon(Path(td), service)
        self.daemon.save_register({str(GROUP): {"last_processed_message_id": 0}})
        self.daemon.WORKERS["stub"] = lambda *_a: successful_result("stub answer")
        self.client = GateClient(messages, available=available)
        self.answering = answering or Answering()
        self.reactions = [] if reactions is None else reactions
        self.reaction_reads = 0
        self.case = case

    async def __aenter__(self):
        session = self

        async def send_reaction(_client, peer, message_id, emoji):
            session.reactions.append((peer, message_id, emoji))
            if getattr(session, "refuse_reactions", False):
                raise RuntimeError("REACTION_INVALID")

        async def available(_client, _peer):
            session.reaction_reads += 1
            return session.client.available

        self.patches = contextlib.ExitStack()
        self.patches.enter_context(mock.patch.object(self.daemon, "harness_run", self.answering))
        self.patches.enter_context(mock.patch.object(self.daemon, "send_reaction", send_reaction))
        self.patches.enter_context(mock.patch.object(self.daemon, "chat_available_reactions",
                                                     available))
        self.task = asyncio.create_task(self.daemon.run_session(self.client))
        await self.client.started.wait()
        return self

    async def send(self, message):
        await self.client.handler(group_event(message))

    async def __aexit__(self, *exc):
        self.client.disconnected.set()
        await asyncio.wait_for(self.task, timeout=10)
        self.patches.close()


# --- Settings ------------------------------------------------------------------

class SettingsSchema(unittest.TestCase):

    def validate(self, daemon, td, group):
        root = Path(td)
        return daemon.validate_settings(
            {"connection": "test", "allowed_groups": {"-1": group}}, root, root)

    def test_triggers_take_known_names_only(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            for good in ([], ["username"], ["alias", "reply"], ["username", "alias", "reply"]):
                with self.subTest(good=good):
                    self.validate(daemon, td, {"triggers": good})
            for bad, why in ((["mention"], r"triggers\[0\]: must be one of"),
                             ("username", r"triggers: must be a JSON array"),
                             (["reply", "reply"], r"triggers: must not name a trigger twice"),
                             ([3], r"triggers\[0\]: must be one of")):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, why):
                    self.validate(daemon, td, {"triggers": bad})

    def test_the_gate_block_is_validated_key_by_key(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.validate(daemon, td, {"gate": {
                "enabled": True, "mode": "live", "brief": "Answers build questions.",
                "context_messages": 20, "context_chars": 3000, "max_batch": 5,
                "model": {"claude": "sonnet", "codex": "gpt-6-luna"}, "effort": "medium"}})
            self.validate(daemon, td, {"gate": {"enabled": False}})
            for bad, why in (
                    ({"enabled": "yes"}, r"gate\.enabled: must be a boolean"),
                    ({"mode": "loud"}, r"gate\.mode: must be one of: live, shadow"),
                    ({"brief": 3}, r"gate\.brief: must be a string"),
                    ({"brief": "x" * 2001}, r"gate\.brief: must be at most"),
                    ({"context_messages": -1}, r"gate\.context_messages: must be between"),
                    ({"context_messages": 1.5}, r"gate\.context_messages: must be an integer"),
                    ({"context_chars": 50000}, r"gate\.context_chars: must be between"),
                    ({"max_batch": 0}, r"gate\.max_batch: must be between"),
                    ({"model": {"gemini": "x"}}, r"gate\.model\.gemini: unsupported property"),
                    ({"effort": ""}, r"gate\.effort: must not be empty"),
                    ({"threshold": 1}, r"gate\.threshold: unsupported property"),
                    ("on", r"gate: must be a JSON object")):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, why):
                    self.validate(daemon, td, {"gate": bad})

    def test_a_switched_on_gate_without_a_mode_is_shadow_with_the_defaults(self):
        self.assertIsNone(gate.settings({}))
        self.assertIsNone(gate.settings({"gate": {"mode": "live"}}))
        self.assertEqual(gate.settings({"gate": {"enabled": True}}), {
            "mode": "shadow", "brief": "", "context_messages": 12, "context_chars": 4000,
            "max_batch": 10, "model": None, "effort": None})
        self.assertEqual(gate.settings({"gate": {"enabled": True, "mode": "live"}})["mode"],
                         "live")

    def test_without_triggers_every_mechanical_trigger_is_on(self):
        self.assertEqual(gate.triggers({}), {"username", "alias", "reply"})
        self.assertEqual(gate.triggers(None), {"username", "alias", "reply"})
        self.assertEqual(gate.triggers({"triggers": []}), set())
        self.assertEqual(gate.triggers({"triggers": ["alias"]}), {"alias"})

    def test_existing_settings_files_still_validate(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            seed = json.loads((SERVICE_DIR / "templates" / "settings.json").read_text())
            daemon.validate_settings(seed, Path(td), Path(td))


# --- Admission -------------------------------------------------------------------

class Admission(unittest.IsolatedAsyncioTestCase):
    """The existing doors and the mechanical triggers, with and without
    `triggers`, as `_message_addresses_me` decides them."""

    me = SimpleNamespace(id=ME, username="helper_bot")

    def daemon(self, td):
        return import_daemon(Path(td), settings())

    def messages(self):
        own = SimpleNamespace(out=True, sender_id=ME)
        other = SimpleNamespace(out=False, sender_id=555)

        def msg(text, *, mentioned=False, reply=None, sender=777, entities=None):
            async def get_reply_message():
                return reply
            return SimpleNamespace(sender_id=sender, raw_text=text, mentioned=mentioned,
                                   is_reply=reply is not None, entities=entities,
                                   get_reply_message=get_reply_message)
        return {
            "flag": msg("hi there", mentioned=True),
            "username": msg("@helper_bot hi"),
            "alias": msg("Helper, what's up"),
            # Telegram raises the mention flag on a reply to the account's message.
            "reply": msg("thanks", reply=own, mentioned=True),
            "reply_to_other": msg("thanks", reply=other),
            "nothing": msg("just chatting"),
            "entity": msg("hey you", mentioned=True, reply=own,
                          entities=[SimpleNamespace(user_id=ME)]),
        }

    async def addressed(self, daemon, policy):
        return {name: await daemon._message_addresses_me(message, self.me, policy)
                for name, message in self.messages().items()}

    async def test_the_default_admits_exactly_what_it_admitted_before(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            policy = {"aliases": ["Helper"]}
            self.assertEqual(await self.addressed(daemon, policy), {
                "flag": True, "username": True, "alias": True, "reply": True,
                "reply_to_other": False, "nothing": False, "entity": True})
            # Spelling out all three triggers is the default.
            self.assertEqual(await self.addressed(daemon, policy), await self.addressed(
                daemon, {**policy, "triggers": ["username", "alias", "reply"]}))
            self.assertTrue(await daemon._message_addresses_me(
                self.messages()["nothing"], self.me, {"require_reference": False}))

    async def test_username_switched_off(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            got = await self.addressed(daemon, {"aliases": ["Helper"],
                                                "triggers": ["alias", "reply"]})
            self.assertEqual(got, {"flag": False, "username": False, "alias": True,
                                   "reply": True, "reply_to_other": False, "nothing": False,
                                   "entity": True})

    async def test_alias_switched_off(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            got = await self.addressed(daemon, {"aliases": ["Helper"],
                                                "triggers": ["username", "reply"]})
            self.assertEqual(got, {"flag": True, "username": True, "alias": False,
                                   "reply": True, "reply_to_other": False, "nothing": False,
                                   "entity": True})
            self.assertFalse(daemon._text_names_me("Helper, hi", self.me,
                                                   {"aliases": ["Helper"],
                                                    "triggers": ["username"]}))

    async def test_reply_switched_off(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            got = await self.addressed(daemon, {"aliases": ["Helper"],
                                                "triggers": ["username", "alias"]})
            # The mention flag on a plain reply no longer admits it; a real
            # mention entity on the same reply still does.
            self.assertEqual(got, {"flag": True, "username": True, "alias": True,
                                   "reply": False, "reply_to_other": False, "nothing": False,
                                   "entity": True})

    async def test_every_trigger_off_leaves_commands_and_every_message(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            got = await self.addressed(daemon, {"aliases": ["Helper"], "triggers": []})
            self.assertFalse(any(value for name, value in got.items() if name != "entity"))
            self.assertTrue(await daemon._message_addresses_me(
                self.messages()["nothing"], self.me,
                {"triggers": [], "require_reference": False}))

    async def test_the_gate_takes_only_what_the_doors_would_pass(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            policy = {"gate": {"enabled": True},
                      "members": {"555": {"kind": "agent"}, "556": {"may_address": False}}}
            plain = SimpleNamespace(sender_id=777, raw_text="hello", out=False)
            self.assertTrue(daemon._gate_candidate(plain, policy))
            self.assertFalse(daemon._gate_candidate(plain, {}))
            for name, message in {
                    "agent": SimpleNamespace(sender_id=555, raw_text="hello", out=False),
                    "muted": SimpleNamespace(sender_id=556, raw_text="hello", out=False),
                    "noreply": SimpleNamespace(sender_id=777, raw_text="hi\n#noreply",
                                               out=False),
                    "command": SimpleNamespace(sender_id=777, raw_text="/status", out=False),
                    "own": SimpleNamespace(sender_id=ME, raw_text="hello", out=True)}.items():
                with self.subTest(name=name):
                    self.assertFalse(daemon._gate_candidate(message, policy))


# --- The decision ------------------------------------------------------------------

class Decision(unittest.TestCase):

    emoji = ["👍", "❤", "🔥"]

    def test_the_schema_offers_the_allowed_set_and_never_names_the_batch(self):
        schema = gate.decision_schema(self.emoji)
        props = schema["properties"]
        self.assertEqual(schema["required"], ["action", "target_message_id", "emoji", "reason"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(props["action"]["enum"], ["ignore", "react", "reply"])
        self.assertEqual(props["target_message_id"], {"type": ["integer", "null"]})
        self.assertEqual(props["emoji"]["enum"], ["👍", "❤", "🔥", None])
        # A chat that allows no reaction is offered no react.
        self.assertEqual(gate.decision_schema([])["properties"]["action"]["enum"],
                         ["ignore", "reply"])
        # The harness caches the schema at the head of the prompt prefix, so
        # it is the chat's alone; the decision's parse holds the batch.
        self.assertIn("target_message_id",
                      gate.parse_decision({"action": "reply", "target_message_id": 7,
                                           "emoji": None, "reason": "q"},
                                          [5, 6], self.emoji)["invalid"])

    def test_a_valid_decision_is_read_as_given(self):
        for answer, expected in (
                ({"action": "ignore", "target_message_id": None, "emoji": None,
                  "reason": "banter"}, ("ignore", None, None)),
                ({"action": "react", "target_message_id": 6, "emoji": "❤️",
                  "reason": "thanks"}, ("react", 6, "❤")),
                ({"action": "reply", "target_message_id": 5, "emoji": None,
                  "reason": "a question"}, ("reply", 5, None))):
            with self.subTest(answer=answer):
                got = gate.parse_decision(answer, [5, 6], self.emoji)
                self.assertNotIn("invalid", got)
                self.assertEqual((got["action"], got["target_message_id"], got["emoji"]),
                                 expected)
        self.assertEqual(gate.parse_decision(json.dumps(
            {"action": "reply", "target_message_id": 5, "emoji": None, "reason": "q"}),
            [5], self.emoji)["action"], "reply")

    def test_a_malformed_or_out_of_set_decision_is_ignore(self):
        for answer, why in (
                ("not json", "not JSON"),
                ([1], "not an object"),
                ({"action": "shout", "target_message_id": 5}, "action"),
                ({"action": "react", "target_message_id": 5, "emoji": None}, "emoji"),
                ({"action": "react", "target_message_id": 5, "emoji": "🦖"}, "emoji"),
                ({"action": "reply", "target_message_id": 99}, "target_message_id"),
                ({"action": "reply", "target_message_id": True}, "target_message_id"),
                ({"action": "react", "target_message_id": None, "emoji": "👍"},
                 "target_message_id")):
            with self.subTest(answer=answer):
                got = gate.parse_decision(answer, [5, 6], self.emoji)
                self.assertEqual(got["action"], "ignore")
                self.assertIn(why, got["invalid"])

    def test_the_allowed_emoji_are_the_standard_set_narrowed_by_the_chat(self):
        self.assertEqual(gate.allowed_emoji("all"), list(gate.STANDARD_REACTIONS))
        self.assertEqual(gate.allowed_emoji(None), [])
        self.assertEqual(gate.allowed_emoji(["❤️", "👍", "🦖"]), ["👍", "❤"])

    def test_the_lead_in_keeps_the_newest_within_the_character_cap(self):
        rows = [{"id": n, "text": "x" * 10} for n in range(20)]
        self.assertEqual([r["id"] for r in gate.lead_in_within(rows, 12, 4000)],
                         list(range(8, 20)))
        self.assertEqual([r["id"] for r in gate.lead_in_within(rows, 12, 35)], [17, 18, 19])
        self.assertEqual(gate.lead_in_within(rows, 0, 4000), [])


# --- The gate's profiles -------------------------------------------------------------

class Profiles(unittest.TestCase):

    def setUp(self):
        import profiles
        self.profiles = profiles

    def bundled(self, name):
        return self.profiles.resolve(name, [], None, fit=False)

    def test_both_gate_profiles_are_bundled_light_turns(self):
        lib = self.profiles.runner()
        for name, harness, model in (("telegram-gate-claude", "claude", "haiku"),
                                     ("telegram-gate-codex", "codex", "luna")):
            with self.subTest(name=name):
                found = self.bundled(name)
                knobs = found["knobs"]
                self.assertEqual(found["layer"], "bundled")
                self.assertEqual((knobs["harness"], knobs["model"]), (harness, model))
                self.assertEqual(knobs["effort"], "low")
                self.assertIs(knobs["persist_session"], False)
                self.assertIn("reply gate", knobs["system_prompt"])
                self.assertEqual(knobs["output_schema"]["required"],
                                 ["action", "target_message_id", "emoji", "reason"])
                self.assertEqual(gate.fit_problems(knobs), [])
                lib.Profile.from_dict(knobs)
                # Not a turn's: the turn's fit refuses it.
                self.assertTrue(self.profiles.fit_problems(knobs))
        claude = self.bundled("telegram-gate-claude")["knobs"]
        self.assertEqual((claude["tools"], claude["setting_sources"], claude["max_turns"],
                          claude["thinking"], claude["strict_mcp"], claude["mcp_config"]),
                         ([], [], 1, False, True, {"mcpServers": {}}))
        # Thinking off reaches Haiku 5.5 only with the CLI's mark taken off
        # it, and a named session makes no session-title request; both reach
        # the CLI as the runner builds its options.
        self.assertEqual(claude["env_set"], {
            "CLAUDE_CODE_MODEL_CAPABILITIES": "claude-haiku-5-5=-rejects_disabled_thinking"})
        self.assertEqual(claude["name"], "telegram-gate")
        from callva.harness_runner import claude as claude_sdk
        opts = claude_sdk.options(lib.Profile.from_dict(claude), cli_path="claude", cwd=".",
                                  env={}, session_id=None, resume_id=None,
                                  stderr=lambda _line: None)
        self.assertEqual(opts.extra_args.get("name"), "telegram-gate")
        self.assertEqual(opts.thinking, {"type": "disabled"})
        codex = self.bundled("telegram-gate-codex")["knobs"]
        # Codex makes no title request; its `name` would name the thread.
        self.assertNotIn("name", codex)
        self.assertEqual(codex["approval_policy"], "never")
        self.assertEqual(codex["codex_config"]["sandbox_mode"], "read-only")
        self.assertFalse(any(codex["codex_config"]["features"].values()))
        self.assertEqual(codex["codex_config"]["skills"],
                         {"include_instructions": False, "bundled": {"enabled": False}})

    def test_a_turn_profile_does_not_fit_the_gate(self):
        for name in ("telegram-claude", "telegram-codex"):
            self.assertTrue(gate.fit_problems(self.bundled(name)["knobs"]))

    def test_a_gate_profile_is_never_saved_from_telegram(self):
        for name in self.profiles.GATE_BUNDLED:
            with self.subTest(name=name), self.assertRaises(self.profiles.Refusal) as refused:
                self.profiles.checked_spec(name, profiles_text(self.bundled(name)), "x", [])
            self.assertEqual(refused.exception.code, "profile_reserved")

    def test_set_profile_refuses_a_gate_profile(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), group_settings())
            for name in ("telegram-gate-claude", "telegram-gate-codex"):
                with self.subTest(name=name), self.assertRaisesRegex(
                        ValueError, "does not fit a telegram turn"):
                    daemon.set_channel_setting({}, str(GROUP), "profile", name)

    def test_the_gate_follows_the_channel_harness_and_its_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), group_settings())
            for harness in ("claude", "codex"):
                found = daemon.resolve_gate_profile(harness, {"model": None, "effort": None})
                self.assertEqual(found["knobs"]["harness"], harness)
                self.assertEqual(found["name"], f"telegram-gate-{harness}")
            found = daemon.resolve_gate_profile(
                "codex", {"model": {"claude": "sonnet", "codex": "gpt-6-luna"},
                          "effort": "medium"})
            self.assertEqual((found["knobs"]["model"], found["knobs"]["effort"]),
                             ("gpt-6-luna", "medium"))
            self.assertIsInstance(daemon.resolve_gate_profile("gemini", {}), Exception)


def profiles_text(found):
    from callva import harness_runner as lib
    return lib.profile_text(found["knobs"])


# --- The session -----------------------------------------------------------------------

class GateSession(unittest.IsolatedAsyncioTestCase):

    async def test_with_no_gate_an_unaddressed_message_starts_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            async with Session(self, td, group_settings()) as s:
                await s.send(group_message(1, "just chatting"))
                await s.send(group_message(2, "Helper, are you there?"))
                await wait_until(lambda: len(s.client.sent) == 1)
                await asyncio.sleep(0.2)
                self.assertEqual(gate_records(s.daemon), [])
                self.assertEqual(len(s.client.sent), 1)
                self.assertIn("ignored text from 777", "\n".join(map(str, s.daemon._test_logs)))

    async def test_a_matching_trigger_bypasses_the_gate_and_agents_never_enter_it(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"},
                                     members={"555": {"name": "Peer", "kind": "agent"}})
            async with Session(self, td, service) as s:
                await s.send(group_message(1, "Helper, a question"))
                await wait_until(lambda: len(s.client.sent) == 1)
                await s.send(group_message(2, "talking to myself", sender=555))
                await s.send(group_message(3, "hi\n#noreply"))
                await asyncio.sleep(0.3)
                self.assertEqual(gate_records(s.daemon), [])
                self.assertEqual(s.client.sent[0]["reply_to"], 1)

    async def test_unaddressed_messages_coalesce_into_one_run(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True}, debounce=0.3)
            async with Session(self, td, service) as s:
                for n in (1, 2, 3):
                    await s.send(group_message(n, f"message {n}"))
                    await asyncio.sleep(0.1)
                # The debounce restarted on each, so nothing has run yet.
                self.assertEqual(gate_records(s.daemon), [])
                await wait_until(lambda: gate_records(s.daemon))
                await asyncio.sleep(0.4)
                records = gate_records(s.daemon)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["batch"], [1, 2, 3])

    async def test_one_run_per_channel_in_flight_and_arrivals_form_the_next_batch(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True},
                                     profile="telegram-claude", debounce=0.05)
            answering = Answering(seconds=0.6)
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "first"))
                await wait_until(lambda: len(answering.gate_calls) == 1)
                await s.send(group_message(2, "second"))
                await s.send(group_message(3, "third"))
                await wait_until(lambda: len(gate_records(s.daemon)) == 2, timeout=5)
                self.assertEqual([r["batch"] for r in gate_records(s.daemon)], [[1], [2, 3]])
                self.assertEqual(len(answering.gate_calls), 2)

    async def test_max_batch_caps_a_burst(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "max_batch": 2},
                                     profile="telegram-claude", debounce=30)
            answering = Answering(seconds=0.5)
            async with Session(self, td, service, answering=answering) as s:
                # A full batch does not wait for the (long) debounce.
                await s.send(group_message(1, "m1"))
                await s.send(group_message(2, "m2"))
                await wait_until(lambda: len(answering.gate_calls) == 1)
                # Three arrive while it runs; the next run reads the newest two.
                for n in (3, 4, 5):
                    await s.send(group_message(n, f"m{n}"))
                await wait_until(lambda: len(gate_records(s.daemon)) == 2, timeout=5)
                self.assertEqual([r["batch"] for r in gate_records(s.daemon)],
                                 [[1, 2], [4, 5]])

    async def test_catch_up_never_runs_the_gate(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"})
            missed = group_message(5, "said while the service was down")
            addressed = group_message(6, "Helper, and this?")
            async with Session(self, td, service, messages=[missed, addressed]) as s:
                await wait_until(lambda: len(s.client.sent) == 1)
                await asyncio.sleep(0.3)
                self.assertEqual(gate_records(s.daemon), [])
                self.assertEqual(s.client.sent[0]["reply_to"], 6)

    async def test_the_gate_reads_its_own_input_and_nothing_of_the_turns(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(
                gate_block={"enabled": True, "brief": "Answers questions about the build.",
                            "context_messages": 3},
                profile="telegram-claude", triggers=["username", "alias"],
                context="CHANNEL-CONTEXT-SENTINEL",
                members={"777": {"name": "Ann", "role": "owner"}})
            now = datetime.now(timezone.utc)
            earlier = [group_message(n, f"earlier {n}", date=now - timedelta(minutes=5))
                       for n in (1, 2, 3)]
            mine = own_message(4, "the lint step fails", date=now - timedelta(seconds=90))
            answering = Answering()
            async with Session(self, td, service, messages=[*earlier, mine],
                               answering=answering, available=["👍", "🦖"]) as s:
                s.daemon.CONTEXT_FILE.write_text("CONTEXT-MD-SENTINEL")
                question = group_message(5, "it worked, thanks", reply_to=mine)
                stranger = group_message(6, "lunch?", sender=999)
                stranger.photo = True
                stranger.file = SimpleNamespace(name=None, mime_type="image/jpeg")
                stranger.text = stranger.raw_text = stranger.message = ""
                s.client.messages.extend([question, stranger])
                await s.send(question)
                await s.send(stranger)
                await wait_until(lambda: answering.gate_calls)
                call = answering.gate_calls[0]
                prompt = call["prompt"]
                document = json.loads(prompt[prompt.index("{"):])
                self.assertEqual(document["you"], {
                    "name": "Helper", "username": "helper_bot", "aliases": ["Helper"],
                    "brief": "Answers questions about the build."})
                self.assertEqual([m["id"] for m in document["batch"]], [5, 6])
                first, second = document["batch"]
                self.assertEqual((first["sender"], first["role"], first["reply_to"],
                                  first["reply_to_you"], first["text"]),
                                 ("Ann", "owner", 4, True, "it worked, thanks"))
                self.assertEqual((second["role"], second["text"]), ("unknown", "[photo]"))
                self.assertIsNotNone(first["time"])
                self.assertEqual([m["id"] for m in document["lead_in"]], [2, 3, 4])
                self.assertEqual([m["you"] for m in document["lead_in"]],
                                 [False, False, True])
                self.assertAlmostEqual(document["facts"]["seconds_since_you_spoke"], 90,
                                       delta=10)
                self.assertEqual(document["facts"]["your_messages_in_window"], 1)
                self.assertEqual(document["allowed_emoji"], ["👍"])
                for absent in ("CONTEXT-MD-SENTINEL", "CHANNEL-CONTEXT-SENTINEL",
                               "Channel state", "Progress command", "Tool authority"):
                    self.assertNotIn(absent, prompt)
                # A run is a fresh session from an empty directory under the
                # service's state, removed after, on the chat's schema.
                self.assertEqual(call["session"].kind, "fresh")
                self.assertEqual(call["cwd_listing"], [])
                self.assertEqual(Path(call["cwd"]).parent, s.daemon.GATE_RUN_DIR)
                self.assertFalse(Path(call["cwd"]).exists())
                self.assertEqual(call["profile"].output_schema["properties"]["emoji"]["enum"],
                                 ["👍", None])
                self.assertNotIn("enum", call["profile"].output_schema["properties"]
                                 ["target_message_id"])
                self.assertNotIn("CODEX_HOME", call["extra_env"])
                self.assertEqual(call["profile"].name, "telegram-gate")
                self.assertEqual(call["profile"].env_set["CLAUDE_CODE_MODEL_CAPABILITIES"],
                                 "claude-haiku-5-5=-rejects_disabled_thinking")
                # The chat's reactions are read once.
                await s.send(group_message(7, "more"))
                await wait_until(lambda: len(answering.gate_calls) == 2)
                self.assertEqual(s.reaction_reads, 1)
                # Two batches of one chat run on one schema and one system
                # prompt, the prefix the harness caches, so the later run
                # reads what the earlier one wrote.
                later = answering.gate_calls[1]["profile"]
                self.assertEqual(json.dumps(later.output_schema),
                                 json.dumps(call["profile"].output_schema))
                self.assertEqual(later.system_prompt, call["profile"].system_prompt)

    async def test_a_voice_note_carries_its_transcript_only_once_one_was_made(self):
        for mode, expected in (("auto", "the build is red again"), ("disabled", "[voice]")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                service = group_settings(gate_block={"enabled": True},
                                         profile="telegram-claude",
                                         voice_transcription={"mode": mode})
                answering = Answering()
                async with Session(self, td, service, answering=answering) as s:
                    voice = group_message(1)
                    voice.voice = True
                    voice.text = voice.raw_text = voice.message = ""
                    voice.file = SimpleNamespace(mime_type="audio/ogg", name=None)
                    with mock.patch.object(s.daemon, "deepgram_transcribe",
                                           lambda *_a: "the build is red again"):
                        await s.send(voice)
                        await wait_until(lambda: answering.gate_calls)
                    prompt = answering.gate_calls[0]["prompt"]
                    document = json.loads(prompt[prompt.index("{"):])
                    self.assertEqual(document["batch"][0]["text"], expected)

    async def test_a_codex_channel_gates_on_codex_under_the_gate_codex_home(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True}, profile="telegram-codex")
            answering = Answering()
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "hello"))
                await wait_until(lambda: gate_records(s.daemon))
                call = answering.gate_calls[0]
                self.assertEqual(call["profile"].harness, "codex")
                self.assertEqual(call["profile"].model, "luna")
                self.assertEqual(call["extra_env"]["CODEX_HOME"],
                                 str(s.daemon.gate_codex_home()))
                self.assertEqual(s.daemon.gate_codex_home(),
                                 s.daemon.STATE_HOME / "telegram" / "gate-codex-home")
                with mock.patch.dict("os.environ", {"TELEGRAM_GATE_CODEX_HOME": "/srv/x"}):
                    self.assertEqual(s.daemon.gate_codex_home(), Path("/srv/x"))
                self.assertEqual(gate_records(s.daemon)[0]["harness"], "codex")

    async def test_a_stub_channel_gates_on_the_configured_stub_decision(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"})
            answering = Answering()
            async with Session(self, td, service, answering=answering) as s:
                with mock.patch.object(s.daemon.reply_gate, "STUB_DECISION", {
                        "action": "react", "target_message_id": 1, "emoji": "🔥",
                        "reason": "configured"}):
                    await s.send(group_message(1, "shipped it"))
                    await wait_until(lambda: gate_records(s.daemon))
                self.assertEqual(answering.gate_calls, [])
                self.assertEqual(s.reactions, [(GROUP, 1, "🔥")])
                self.assertEqual(gate_records(s.daemon)[0]["harness"], "stub")

    async def test_live_react_sets_the_reaction_and_a_refusal_is_logged_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"},
                                     profile="telegram-claude")
            answering = Answering(
                {"action": "react", "target_message_id": 2, "emoji": "👍", "reason": "thanks"},
                {"action": "react", "target_message_id": 3, "emoji": "❤", "reason": "nice"})
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "fixed"))
                await s.send(group_message(2, "thanks!"))
                await wait_until(lambda: gate_records(s.daemon))
                self.assertEqual(s.reactions, [(GROUP, 2, "👍")])
                record = gate_records(s.daemon)[0]
                self.assertEqual((record["action"], record["target"], record["emoji"],
                                  record["acted"], record["outcome"]),
                                 ("react", 2, "👍", True, "reacted"))
                s.refuse_reactions = True
                await s.send(group_message(3, "great"))
                await wait_until(lambda: len(gate_records(s.daemon)) == 2)
                await asyncio.sleep(0.2)
                self.assertEqual(s.reactions[1:], [(GROUP, 3, "❤")])
                record = gate_records(s.daemon)[1]
                self.assertFalse(record["acted"])
                self.assertIn("reaction refused", record["outcome"])
                self.assertEqual(s.client.sent, [])

    async def test_live_reply_runs_a_turn_with_the_reason_and_shows_typing(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"},
                                     profile="telegram-claude")
            answering = Answering({"action": "reply", "target_message_id": 2, "emoji": None,
                                   "reason": "a question it can answer."},
                                  turn="=== REPLY ===\nthe answer")
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "anyone know"))
                await s.send(group_message(2, "why the build is red?"))
                await wait_until(lambda: s.client.sent)
                self.assertEqual(s.client.sent[0]["reply_to"], 2)
                self.assertEqual(s.client.sent[0]["text"], "the answer")
                self.assertEqual(len(answering.turn_calls), 1)
                turn_prompt = answering.turn_calls[0]["prompt"]
                self.assertIn("Woken by: the reply gate", turn_prompt)
                self.assertIn("a question it can answer.", turn_prompt)
                self.assertIn("Message: #2", turn_prompt)
                record = gate_records(s.daemon)[0]
                self.assertEqual((record["action"], record["target"], record["acted"],
                                  record["outcome"]), ("reply", 2, True, "reply queued"))
                # Typing from the decision, held until the turn answered.
                await wait_until(lambda: all(a["ended"] for a in s.client.actions))
                self.assertGreaterEqual(len([a for a in s.client.actions
                                             if a["kind"] == "typing"]), 2)

    async def test_a_reply_after_the_watermark_moved_past_the_target_still_runs(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"},
                                     profile="telegram-claude", debounce=0.3)
            answering = Answering({"action": "reply", "target_message_id": 1, "emoji": None,
                                   "reason": "q"}, turn="ok")
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "how do I rerun it?"))
                await s.send(group_message(2, "Helper, ping"))
                await wait_until(lambda: len(s.client.sent) == 2, timeout=5)
                self.assertEqual(sorted(m["reply_to"] for m in s.client.sent), [1, 2])

    async def test_shadow_decides_and_logs_and_acts_on_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True}, profile="telegram-claude")
            answering = Answering(
                {"action": "reply", "target_message_id": 1, "emoji": None, "reason": "q"},
                {"action": "react", "target_message_id": 2, "emoji": "👍", "reason": "t"})
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "a question?"))
                await wait_until(lambda: len(gate_records(s.daemon)) == 1)
                await s.send(group_message(2, "thanks"))
                await wait_until(lambda: len(gate_records(s.daemon)) == 2)
                await asyncio.sleep(0.3)
                self.assertEqual((s.client.sent, s.reactions, s.client.actions), ([], [], []))
                self.assertEqual(answering.turn_calls, [])
                self.assertEqual(s.daemon.load_register()[str(GROUP)].get("jobs") or {}, {})
                for record in gate_records(s.daemon):
                    self.assertEqual((record["mode"], record["acted"], record["outcome"]),
                                     ("shadow", False, "shadow"))

    async def test_a_malformed_decision_is_ignore_and_logged(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True, "mode": "live"},
                                     profile="telegram-claude")
            answering = Answering({"action": "react", "target_message_id": 1,
                                   "emoji": "🦖", "reason": "x"})
            async with Session(self, td, service, answering=answering,
                               available=["👍"]) as s:
                await s.send(group_message(1, "hi"))
                await wait_until(lambda: gate_records(s.daemon))
                record = gate_records(s.daemon)[0]
                self.assertEqual((record["action"], record["acted"]), ("ignore", False))
                self.assertIn("emoji", record["invalid"])
                self.assertEqual(s.reactions, [])

    async def test_one_record_per_run_with_every_field(self):
        with tempfile.TemporaryDirectory() as td:
            service = group_settings(gate_block={"enabled": True}, profile="telegram-claude")
            answering = Answering({"action": "ignore", "target_message_id": 1,
                                   "emoji": None, "reason": "banter"})
            async with Session(self, td, service, answering=answering) as s:
                await s.send(group_message(1, "lol"))
                await wait_until(lambda: gate_records(s.daemon))
                await asyncio.sleep(0.3)
                records = gate_records(s.daemon)
                self.assertEqual(len(records), 1)
                self.assertEqual(len(answering.gate_calls), 1)
                record = records[0]
                for field in ("channel", "batch", "harness", "model", "action", "target",
                              "emoji", "reason", "latency_ms", "tokens", "mode", "acted"):
                    self.assertIn(field, record)
                self.assertEqual((record["channel"], record["batch"], record["harness"],
                                  record["model"], record["action"], record["reason"]),
                                 (str(GROUP), [1], "claude", "haiku", "ignore", "banter"))
                self.assertEqual(record["tokens"]["input"], 100)
                self.assertIsInstance(record["latency_ms"], int)


# --- The full turn -------------------------------------------------------------------

class FullTurn(unittest.TestCase):

    def test_the_template_says_silence_and_a_reaction_are_valid(self):
        text = (SERVICE_DIR / "templates" / "context.md").read_text()
        self.assertIn("An empty final answer is silence", text)
        self.assertIn("valid outcome", text)
        self.assertIn("reply gate woke this turn", text)
        self.assertIn("A reaction on the request message", text)

    def test_a_gate_woken_prompt_says_so_and_carries_the_reason(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            state = {"chat_id": "-200", "current_request": {"message_id": 9, "text": "q"},
                     "gate": {"reason": "A question about the build."}}
            prompt = daemon.build_prompt([], state)
            self.assertIn("Woken by: the reply gate, not by an address.", prompt)
            self.assertIn("A question about the build.", prompt)
            self.assertIn("An empty final answer (silence) or a reaction", prompt)
            self.assertIn("Woken by: the reply gate",
                          daemon.resumed_prompt({**state, "resume_session": "s"}))
            self.assertNotIn("Woken by", daemon.build_prompt([], {**state, "gate": None}))


if __name__ == "__main__":
    unittest.main()
