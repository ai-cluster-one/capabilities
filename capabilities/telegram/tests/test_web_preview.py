#!/usr/bin/env python3
"""A message's link preview reaches `read`, `search` and `export` output."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_outbound_actions import import_cli  # noqa: E402


class PhotoEmpty:
    pass


def _page(**over):
    base = dict(url="https://example.com/listing-123", display_url="example.com/listing-123",
                site_name="Example", title="A house for sale", description="5 rooms, 140 m2",
                type="article", photo=SimpleNamespace(id=77))
    base.update(over)
    return SimpleNamespace(**base)


def _message(page, *, photo=True):
    """A Telethon-shaped message whose media is a link preview."""
    return SimpleNamespace(
        id=705, date=datetime(2026, 11, 6, 8, 44, tzinfo=timezone.utc), sender_id=1,
        sender=None, message="https://example.com/listing-123", reply_to_msg_id=None,
        action=None, voice=None, audio=None, video_note=None, video=None, sticker=None,
        document=None, poll=None, contact=None, geo=None, media=object(),
        web_preview=page, photo=(page.photo if (page is not None and photo) else None))


class WebPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = import_cli()

    def test_read_record_carries_the_preview(self):
        rec = self.cli.msg_to_dict(_message(_page()), lambda m: None)
        self.assertEqual(rec["web_preview"]["title"], "A house for sale")
        self.assertEqual(rec["web_preview"]["description"], "5 rooms, 140 m2")
        self.assertEqual(rec["web_preview"]["site_name"], "Example")
        self.assertEqual(rec["web_preview"]["url"], "https://example.com/listing-123")
        self.assertTrue(rec["web_preview"]["has_photo"])

    def test_read_record_without_preview_is_unchanged(self):
        m = _message(None)
        rec = self.cli.msg_to_dict(m, lambda m: None)
        self.assertNotIn("web_preview", rec)
        self.assertEqual(set(rec), {"id", "date", "sender_id", "sender", "text", "reply_to", "topic_id"})

    def test_previewed_photo_keeps_its_kind_and_gains_the_preview(self):
        kind, meta = self.cli._classify_media(_message(_page()))
        self.assertEqual(kind, "photo")
        self.assertEqual(meta["photo_id"], 77)
        self.assertEqual(meta["web_preview"]["title"], "A house for sale")

    def test_preview_without_photo_is_a_webpage_with_its_text(self):
        kind, meta = self.cli._classify_media(_message(_page(photo=PhotoEmpty()), photo=False))
        self.assertEqual(kind, "webpage")
        self.assertEqual(meta["url"], "https://example.com/listing-123")
        self.assertEqual(meta["description"], "5 rooms, 140 m2")
        self.assertFalse(meta["has_photo"])

    def test_plain_photo_has_no_preview(self):
        m = _message(None)
        m.photo = SimpleNamespace(id=5)
        kind, meta = self.cli._classify_media(m)
        self.assertEqual((kind, meta), ("photo", {"photo_id": 5}))


if __name__ == "__main__":
    unittest.main()
