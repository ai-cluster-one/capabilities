#!/usr/bin/env python3
"""A forward reaches the worker as a forward (issue 99).

A forwarded message rendered as the forwarder's own typed words, so a worker
handed the assistant's own earlier report took it for the person's proposal
and argued against it. A forward now opens with `[forwarded from <origin>]`
in the tail and in the current request; every other message renders as before.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_assistant_service import (  # noqa: E402
    Event,
    FakeClient,
    Message,
    import_daemon,
    settings,
    successful_result,
    wait_until,
)
sys.path.pop(0)

# FakeClient.get_me() answers as account 42, named "Assistant" in settings().
SELF_ID = 42


def forwarded(message, *, user_id=None, sender=None, channel_id=None, chat=None,
              from_name=None):
    """Give a test message the two faces Telethon gives a forward: the raw
    MessageFwdHeader on `fwd_from`, and the resolved Forward on `forward`,
    which adds whatever sender or chat entity it could find."""
    peer = None
    if user_id is not None:
        peer = SimpleNamespace(user_id=user_id)
    elif channel_id is not None:
        peer = SimpleNamespace(channel_id=channel_id)
    message.fwd_from = SimpleNamespace(from_id=peer, from_name=from_name)
    message.forward = SimpleNamespace(from_id=peer, from_name=from_name,
                                      sender=sender, chat=chat)
    return message


def person(first, last=None):
    return SimpleNamespace(first_name=first, last_name=last, username=None)


class ForwardOriginTests(unittest.TestCase):
    """What a forward says about where it came from."""

    def daemon(self, td, **extra):
        service_settings = settings()
        service_settings.update(extra)
        daemon = import_daemon(Path(td), service_settings)
        daemon.SELF_ACCOUNT_ID = SELF_ID
        return daemon

    def test_a_forward_of_this_accounts_own_words_reads_as_yours(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            message = forwarded(Message(21, text="the build passed"), user_id=SELF_ID,
                                sender=person("Assistant"))
            self.assertEqual(daemon.message_tail_text(message),
                             "[forwarded from Assistant (you)] the build passed")

    def test_a_forward_from_another_person_names_them(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            message = forwarded(Message(5, text="thursday then"), user_id=555,
                                sender=person("Other", "Person"))
            self.assertEqual(daemon.message_tail_text(message),
                             "[forwarded from Other Person] thursday then")

    def test_a_configured_person_is_named_the_way_the_tail_names_them(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td, allowed_users={"555": {"name": "Configured Name", "role": "x"}})
            message = forwarded(Message(5, text="thursday then"), user_id=555,
                                sender=person("Other", "Person"))
            self.assertEqual(daemon.message_tail_text(message),
                             "[forwarded from Configured Name] thursday then")

    def test_a_forward_from_a_channel_names_the_channel(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            message = forwarded(Message(6, text="rates fell"), channel_id=1234,
                                chat=SimpleNamespace(title="Market News", username=None))
            self.assertEqual(daemon.message_tail_text(message),
                             "[forwarded from Market News] rates fell")

    def test_a_forward_from_a_hidden_account_keeps_the_name_it_left(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            message = forwarded(Message(7, text="between us"), from_name="Hidden Name")
            self.assertEqual(daemon.message_tail_text(message),
                             "[forwarded from Hidden Name] between us")

    def test_an_origin_that_cannot_be_resolved_falls_back_and_never_raises(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError(f"cannot read {name}")

        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            broken = Message(11, text="d")
            broken.fwd_from = Exploding()
            broken.forward = Exploding()
            self.assertEqual(daemon.message_tail_text(forwarded(Message(8, text="a"))),
                             "[forwarded from an unknown sender] a")
            self.assertEqual(
                daemon.message_tail_text(forwarded(Message(9, text="b"), user_id=555)),
                "[forwarded from user 555] b")
            self.assertEqual(
                daemon.message_tail_text(forwarded(Message(10, text="c"), channel_id=77)),
                "[forwarded from chat 77] c")
            self.assertEqual(daemon.message_tail_text(broken),
                             "[forwarded from an unknown sender] d")

    def test_only_a_forward_changes_how_a_message_renders(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)

            def shapes():
                text = Message(20, text="hello")
                attachment = Message(21, text="see this")
                attachment.file = SimpleNamespace(name="plan.pdf", mime_type="application/pdf")
                photo = Message(22, text="")
                photo.file = SimpleNamespace(name=None, mime_type="image/jpeg")
                photo.photo = True
                voice = Message(23, voice=True)
                return [text, attachment, photo, voice]

            before = ["hello", "see this\n[attachment: plan.pdf | msg 21]",
                      "[attachment: photo-22.jpg | msg 22]", None]
            plain = shapes()
            # A real Telethon message that is not a forward carries both as None.
            for message in plain[::2]:
                message.fwd_from = None
                message.forward = None
            self.assertEqual([daemon.message_tail_text(m) for m in plain], before)

            marked = [forwarded(m, user_id=555, sender=person("Other")) for m in shapes()]
            self.assertEqual([daemon.message_tail_text(m) for m in marked],
                             [f"[forwarded from Other] {t}" if t else None for t in before])


class ForwardInTheRequestTests(unittest.IsolatedAsyncioTestCase):
    """A forward, delivered to a running daemon, reaches the worker marked."""

    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)

    async def asyncTearDown(self):
        self._td.cleanup()

    def daemon(self):
        daemon = import_daemon(self.td, settings())
        self.dispatches = []

        def capture(_chat, tail, state=None, _procs=None):
            self.dispatches.append({"tail": tail, "state": state,
                                    "prompt": daemon.build_prompt(tail, state)})
            return successful_result(f"answer {len(self.dispatches)}")

        daemon.WORKERS["stub"] = capture
        return daemon

    async def run_one(self, daemon, message):
        client = FakeClient()
        task = asyncio.create_task(daemon.run_session(client))
        await client.started.wait()
        client.messages.insert(0, message)
        await client.handler(Event(message))
        await wait_until(lambda: self.dispatches)
        await wait_until(lambda: client.sent)
        client.disconnected.set()
        await asyncio.wait_for(task, timeout=5)
        return self.dispatches[0]

    async def test_a_forward_of_this_accounts_words_is_marked_in_tail_and_request(self):
        daemon = self.daemon()
        dispatch = await self.run_one(
            daemon, forwarded(Message(50, text="the build passed"), user_id=SELF_ID))

        self.assertEqual(dispatch["state"]["current_request"]["text"],
                         "[forwarded from Assistant (you)] the build passed")
        prompt = dispatch["prompt"]
        self.assertIn("separate jobs.\n[forwarded from Assistant (you)] the build passed\n",
                      prompt)
        self.assertIn("] Test User: [forwarded from Assistant (you)] the build passed", prompt)

    async def test_a_plain_message_reaches_the_worker_unchanged(self):
        daemon = self.daemon()
        message = Message(51, text="hello there")
        message.fwd_from = None
        message.forward = None
        dispatch = await self.run_one(daemon, message)

        self.assertEqual(dispatch["state"]["current_request"]["text"], "hello there")
        self.assertNotIn("forwarded", dispatch["prompt"])
        self.assertIn("] Test User: hello there\n", dispatch["prompt"])

    async def test_a_forwarded_voice_note_says_whose_voice_it_is(self):
        daemon = self.daemon()
        daemon.deepgram_transcribe = lambda audio, mime: "meeting on thursday"
        dispatch = await self.run_one(
            daemon, forwarded(Message(52, voice=True), user_id=555, sender=person("Other")))

        self.assertEqual(dispatch["state"]["current_request"]["text"],
                         "[forwarded from Other] meeting on thursday")


if __name__ == "__main__":
    unittest.main()
