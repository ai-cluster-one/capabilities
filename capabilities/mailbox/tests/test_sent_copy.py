from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "mailbox"


def _load_module():
    name = "mailbox_sent_copy_under_test"
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
    def __init__(self, folders, existing=False, append_error=None, logout_error=None):
        self.folder = FakeFolder(folders)
        self.existing = existing
        self.append_error = append_error
        self.logout_error = logout_error
        self.appended = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def logout(self):
        if self.logout_error:
            raise self.logout_error

    def uids(self, criterion):
        self.criterion = criterion
        return ["1"] if self.existing else []

    def append(self, message, folder, dt, flag_set):
        if self.append_error:
            raise self.append_error
        self.appended.append(
            {"message": message, "folder": folder, "dt": dt, "flag_set": flag_set}
        )


class FakeSMTP:
    def __init__(self, *_args, **_kwargs):
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self, context):
        self.tls_context = context

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, from_addr, to_addrs, message, mail_options=()):
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.message = message
        self.mail_options = mail_options


def _folder(name, *flags):
    return SimpleNamespace(name=name, flags=flags)


def _cfg():
    return {
        "user": "sender@example.com",
        "password": "secret",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
    }


class SentFolderTests(unittest.TestCase):
    def test_prefers_special_use_flag(self):
        folders = [_folder("Archive"), _folder("Provider Sent", r"\Sent")]
        self.assertEqual(mailbox._sent_folder_name(folders), "Provider Sent")

    def test_falls_back_to_conventional_name(self):
        folders = [_folder("INBOX"), _folder("[Gmail]/Sent Mail")]
        self.assertEqual(mailbox._sent_folder_name(folders), "[Gmail]/Sent Mail")


class SendCopyTests(unittest.TestCase):
    def _send(self, fake_mailbox):
        smtp = FakeSMTP()
        with (
            mock.patch.object(mailbox, "_imap", return_value=fake_mailbox),
            mock.patch.object(mailbox.smtplib, "SMTP", return_value=smtp),
        ):
            result = mailbox.cmd_send(
                _cfg(), ["recipient@example.com"], [], "Subject", "Body", [], None
            )
        return result, smtp

    def test_appends_exact_sent_message_with_seen_flag(self):
        imap = FakeMailbox([_folder("Sent", r"\Sent")])
        result, smtp = self._send(imap)

        self.assertTrue(result["sent"])
        self.assertEqual(result["sent_copy"]["folder"], "Sent")
        self.assertTrue(result["sent_copy"]["saved"])
        self.assertFalse(result["sent_copy"]["provider_saved"])
        self.assertEqual(len(imap.appended), 1)
        self.assertEqual(imap.appended[0]["message"], smtp.message)
        appended = message_from_bytes(imap.appended[0]["message"])
        self.assertEqual(appended["Message-ID"], result["message_id"])
        self.assertIsNotNone(appended["Date"])
        self.assertEqual(imap.appended[0]["flag_set"], [mailbox.MailMessageFlags.SEEN])

    def test_does_not_duplicate_provider_saved_copy(self):
        imap = FakeMailbox([_folder("Sent", r"\Sent")], existing=True)
        result, _smtp = self._send(imap)

        self.assertTrue(result["sent_copy"]["provider_saved"])
        self.assertEqual(imap.appended, [])

    def test_reports_copy_failure_without_claiming_send_failed(self):
        imap = FakeMailbox(
            [_folder("Sent", r"\Sent")], append_error=RuntimeError("append refused")
        )
        result, smtp = self._send(imap)

        self.assertIsNotNone(smtp.message)
        self.assertTrue(result["sent"])
        self.assertFalse(result["sent_copy"]["saved"])
        self.assertIn("do not retry", result["warning"])

    def test_logout_failure_does_not_mask_successful_send(self):
        imap = FakeMailbox(
            [_folder("Sent", r"\Sent")], logout_error=RuntimeError("logout failed")
        )
        result, smtp = self._send(imap)

        self.assertIsNotNone(smtp.message)
        self.assertTrue(result["sent"])
        self.assertTrue(result["sent_copy"]["saved"])

    def test_missing_sent_folder_refuses_before_smtp(self):
        imap = FakeMailbox([_folder("INBOX")])
        smtp = FakeSMTP()
        with (
            mock.patch.object(mailbox, "_imap", return_value=imap),
            mock.patch.object(mailbox.smtplib, "SMTP", return_value=smtp),
            self.assertRaises(SystemExit) as caught,
        ):
            mailbox.cmd_send(
                _cfg(), ["recipient@example.com"], [], "Subject", "Body", [], None
            )
        self.assertEqual(caught.exception.code, 6)
        self.assertIsNone(smtp.message)


if __name__ == "__main__":
    unittest.main()
