from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "mailbox", CAPABILITY / "mailbox")
    if path.is_file()), CAPABILITY / "bin" / "mailbox")


def _load_module():
    name = "mailbox_draft_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


mailbox = _load_module()


class FakeFolder:
    def __init__(self, folders):
        self._folders = folders
        self.selected = "INBOX"

    def list(self):
        return self._folders

    def set(self, name):
        self.selected = name


class FakeMailbox:
    def __init__(self, folders, append_response=(b"[APPENDUID 7 42] APPEND completed",)):
        self.folder = FakeFolder(folders)
        self.append_response = list(append_response)
        self.appended = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def uids(self, criterion):
        self.criterion = criterion
        return ["40", "41"]

    def append(self, message, folder, dt, flag_set):
        self.appended.append(
            {"message": message, "folder": folder, "dt": dt, "flag_set": flag_set}
        )
        return "OK", self.append_response


def _folder(name, *flags):
    return SimpleNamespace(name=name, flags=flags)


def _cfg(allow_write=True):
    return {
        "id": "work",
        "user": "sender@example.com",
        "password": "secret",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "allow_write": allow_write,
    }


def _no_smtp(*_args, **_kwargs):
    raise AssertionError("draft must never open SMTP")


class DraftsFolderTests(unittest.TestCase):
    def test_prefers_special_use_flag(self):
        folders = [_folder("Drafts"), _folder("Entwürfe", r"\Drafts")]
        self.assertEqual(mailbox._drafts_folder_name(folders), "Entwürfe")

    def test_falls_back_to_conventional_name(self):
        folders = [_folder("INBOX"), _folder("[Gmail]/Drafts")]
        self.assertEqual(mailbox._drafts_folder_name(folders), "[Gmail]/Drafts")


class DraftTests(unittest.TestCase):
    def _draft(self, imap, in_reply_to=None):
        with (
            mock.patch.object(mailbox, "_imap", return_value=imap),
            mock.patch.object(mailbox.smtplib, "SMTP", side_effect=_no_smtp),
        ):
            return mailbox.cmd_draft(
                _cfg(), ["recipient@example.com"], [], "Subject", "Body", [],
                in_reply_to,
            )

    def test_appends_to_drafts_with_draft_flag_and_sends_nothing(self):
        imap = FakeMailbox([_folder("INBOX"), _folder("Drafts", r"\Drafts")])
        result = self._draft(imap)

        self.assertFalse(result["sent"])
        self.assertEqual(result["folder"], "Drafts")
        self.assertEqual(result["uid"], "42")
        self.assertEqual(len(imap.appended), 1)
        self.assertEqual(imap.appended[0]["folder"], "Drafts")
        self.assertEqual(imap.appended[0]["flag_set"], [mailbox.MailMessageFlags.DRAFT])
        saved = message_from_bytes(imap.appended[0]["message"])
        self.assertEqual(saved["Message-ID"], result["message_id"])
        self.assertEqual(saved["To"], "recipient@example.com")

    def test_reply_draft_carries_threading_headers(self):
        imap = FakeMailbox([_folder("Drafts", r"\Drafts")])
        result = self._draft(imap, in_reply_to="<original@example.com>")

        saved = message_from_bytes(imap.appended[0]["message"])
        self.assertEqual(saved["In-Reply-To"], "<original@example.com>")
        self.assertEqual(saved["References"], "<original@example.com>")
        self.assertEqual(result["in_reply_to"], "<original@example.com>")

    def test_uid_falls_back_to_message_id_search(self):
        imap = FakeMailbox([_folder("Drafts", r"\Drafts")],
                           append_response=(b"APPEND completed",))
        result = self._draft(imap)

        self.assertEqual(result["uid"], "41")
        self.assertIn(result["message_id"], imap.criterion)

    def test_missing_drafts_folder_writes_nothing(self):
        imap = FakeMailbox([_folder("INBOX")])
        with self.assertRaises(SystemExit) as caught:
            self._draft(imap)
        self.assertEqual(caught.exception.code, 6)
        self.assertEqual(imap.appended, [])


class DraftWriteGateTests(unittest.TestCase):
    def test_read_only_connection_exits_4_and_writes_nothing(self):
        imap = FakeMailbox([_folder("Drafts", r"\Drafts")])
        argv = ["mailbox", "draft", "--to", "recipient@example.com",
                "--subject", "Subject", "--body", "Body"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(mailbox, "_gate"),
            mock.patch.object(mailbox, "_contract"),
            mock.patch.object(mailbox, "_load_config", return_value=_cfg(False)),
            mock.patch.object(mailbox, "_imap", return_value=imap) as opened,
            mock.patch("sys.stderr") as stderr,
            self.assertRaises(SystemExit) as caught,
        ):
            mailbox.main()

        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(imap.appended, [])
        opened.assert_not_called()
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("writing drafts needs write access", written)

    def test_draft_is_a_declared_write_verb(self):
        self.assertIn("draft", mailbox.WRITE_VERBS)


if __name__ == "__main__":
    unittest.main()
