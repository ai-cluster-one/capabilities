#!/usr/bin/env python3
"""A message composed with rich formatting reads as its words (issue 102).

Current Telegram apps send a paragraph followed by a list as a rich message:
the text arrives empty and the words live only in `rich_message.blocks`. The
worker's tail dropped such a message and `read`, `search` and `export` showed
it with empty text. Its blocks now read as its text everywhere a message's
text is read.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_assistant_service import Message, import_daemon, settings  # noqa: E402
from test_outbound_actions import (  # noqa: E402
    _HistoryClient,
    _forum_message,
    import_cli,
)
sys.path.pop(0)


def _node(type_name, **fields):
    """An object shaped like the Telethon type of that name."""
    obj = type(type_name, (), {})()
    obj.__dict__.update(fields)
    return obj


def plain(text):
    return _node("TextPlain", text=text)


def paragraph(text):
    return _node("PageBlockParagraph", text=text)


def ordered(*items):
    return _node("PageBlockOrderedList", items=list(items), reversed=False,
                 start=None, type=None)


def ordered_item(num, text, checkbox=False, checked=False):
    return _node("PageListOrderedItemText", text=text, num=num, value=None,
                 checkbox=checkbox, checked=checked, type=None)


def bullets(*items):
    return _node("PageBlockList", items=list(items))


def bullet(text, checkbox=False, checked=False):
    return _node("PageListItemText", text=text, checkbox=checkbox, checked=checked)


def rich(*blocks):
    return _node("RichMessage", blocks=list(blocks), photos=[], documents=[],
                 rtl=False, part=None)


# The shape the report read off the wire with Telethon 1.45.0: empty text, no
# media, and the words in a paragraph and an ordered list.
OFFER = rich(
    paragraph(_node("TextConcat", texts=[plain("Two options for "),
                                         _node("TextBold", text=plain("Friday")),
                                         plain(":")])),
    ordered(ordered_item("1", plain("take the early slot")),
            ordered_item("2", _node("TextUrl", text=plain("move it to Monday"),
                                    url="https://example.invalid", webpage_id=0))),
)
OFFER_TEXT = "Two options for Friday:\n1. take the early slot\n2. move it to Monday"

CHECKLIST = rich(
    paragraph(plain("Before the call")),
    bullets(bullet(plain("agenda"), checkbox=True, checked=True),
            bullet(plain("slides"), checkbox=True),
            bullet(plain("notes"))),
)
CHECKLIST_TEXT = "Before the call\n- [x] agenda\n- [ ] slides\n- notes"


def rich_tail_message(message_id, blocks):
    message = Message(message_id, text="")
    message.rich_message = blocks
    return message


def rich_history_message(message_id, blocks):
    message = _forum_message(message_id, text="")
    message.rich_message = blocks
    return message


class TailTests(unittest.TestCase):

    def test_a_rich_message_reaches_the_tail_as_its_words(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.assertEqual(daemon.message_tail_text(rich_tail_message(5, OFFER)),
                             OFFER_TEXT)
            self.assertEqual(daemon.message_tail_text(rich_tail_message(6, CHECKLIST)),
                             CHECKLIST_TEXT)

    def test_a_message_with_text_reads_as_its_text(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            message = Message(7, text="plain words")
            message.rich_message = OFFER
            self.assertEqual(daemon.message_tail_text(message), "plain words")
            self.assertIsNone(daemon.message_tail_text(rich_tail_message(8, None)))


class ReadVerbTests(unittest.TestCase):
    """`read`, `search` and `export` print a rich message's words."""

    def setUp(self):
        self.cli = import_cli()

        async def authorize(_client):
            return None

        self.cli._require_auth = authorize
        self.cli.entity_label = lambda entity: str(entity)

    def _rich_history(self):
        self.client = _HistoryClient([rich_history_message(31, OFFER),
                                      _forum_message(30, text="before it")])
        self.cli.make_client = lambda _cfg: self.client

        async def resolve(_client, _chat):
            return SimpleNamespace(forum=False, title="Example Group")

        self.cli.resolve_chat = resolve

    def test_read_shows_the_words_of_a_rich_message(self):
        self._rich_history()
        rows = asyncio.run(self.cli.cmd_read({"id": "test"}, "-1002", 50, None, True))
        self.assertEqual([(row["id"], row["text"]) for row in rows],
                         [(30, "before it"), (31, OFFER_TEXT)])

        self._rich_history()
        transcript = asyncio.run(self.cli.cmd_read({"id": "test"}, "-1002", 50, None, False))
        self.assertIn(": " + OFFER_TEXT.replace("\n", " ⏎ "), transcript)

    def test_search_shows_the_words_of_a_rich_message(self):
        self._rich_history()
        rows = asyncio.run(self.cli.cmd_search({"id": "test"}, "-1002", "slot", 50, True))
        self.assertEqual(rows[-1]["text"], OFFER_TEXT)

    def test_export_shows_the_words_of_a_rich_message(self):
        self._rich_history()
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "export.json"
            asyncio.run(self.cli.cmd_export(
                {"id": "test"}, "-1002", str(output), None, 0, None,
                False, False, False, False, None))
            payload = json.loads(output.read_text())
        self.assertEqual({row["id"]: row["text"] for row in payload["messages"]},
                         {30: "before it", 31: OFFER_TEXT})


if __name__ == "__main__":
    unittest.main()
