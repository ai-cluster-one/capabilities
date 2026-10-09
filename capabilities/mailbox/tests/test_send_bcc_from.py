"""`send --bcc` puts blind recipients on the SMTP envelope and never in a header, and
the From display name comes from --from-name or the connection's display_name."""
from __future__ import annotations

import subprocess
import sys
import unittest
from email import message_from_bytes
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sent_copy import FakeMailbox, FakeSMTP, SCRIPT, _cfg, _folder, mailbox  # noqa: E402


class BccAndFromTests(unittest.TestCase):
    def _send(self, cfg=None, **kwargs):
        smtp = FakeSMTP()
        imap = FakeMailbox([_folder("Sent", r"\Sent")])
        with (
            mock.patch.object(mailbox, "_imap", return_value=imap),
            mock.patch.object(mailbox.smtplib, "SMTP", return_value=smtp),
        ):
            result = mailbox.cmd_send(cfg or _cfg(), ["recipient@example.com"], [],
                                      "Subject", "Body", [], None, **kwargs)
        return result, smtp, imap

    def test_bcc_is_an_envelope_recipient_and_never_a_header(self):
        result, smtp, imap = self._send(bcc=["log@bcc.example.net"])
        self.assertEqual(smtp.to_addrs, ["recipient@example.com", "log@bcc.example.net"])
        wire = message_from_bytes(smtp.message)
        self.assertIsNone(wire["Bcc"])
        self.assertNotIn(b"log@bcc.example.net", smtp.message)
        self.assertNotIn(b"log@bcc.example.net", imap.appended[0]["message"])
        self.assertEqual(result["bcc"], ["log@bcc.example.net"])

    def test_bcc_takes_a_named_address(self):
        _result, smtp, _imap = self._send(bcc=["147585693 <147585693@bcc.eu1.hubspot.com>"])
        self.assertEqual(smtp.to_addrs[-1], "147585693@bcc.eu1.hubspot.com")

    def test_no_bcc_keeps_the_recipients_as_they_were(self):
        _result, smtp, _imap = self._send()
        self.assertEqual(smtp.to_addrs, ["recipient@example.com"])

    def test_display_name_from_the_connection(self):
        cfg = dict(_cfg(), display_name="Jane Doe")
        result, smtp, _imap = self._send(cfg=cfg)
        wire = message_from_bytes(smtp.message)
        self.assertEqual(wire["From"], "Jane Doe <sender@example.com>")
        self.assertEqual(smtp.from_addr, "sender@example.com")
        self.assertEqual(result["from"], "Jane Doe <sender@example.com>")

    def test_from_name_overrides_the_connection(self):
        cfg = dict(_cfg(), display_name="Jane Doe")
        _result, smtp, _imap = self._send(cfg=cfg, from_name="Stepan Sarkisov")
        self.assertEqual(message_from_bytes(smtp.message)["From"], "Stepan Sarkisov <sender@example.com>")

    def test_no_name_is_the_bare_address(self):
        _result, smtp, _imap = self._send()
        self.assertEqual(message_from_bytes(smtp.message)["From"], "sender@example.com")

    def test_help_documents_bcc_and_display_name(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "help"], capture_output=True, text=True).stdout
        for said in ("--bcc A", "--from-name NAME", '"display_name"', "no Bcc header"):
            self.assertIn(said, out)


if __name__ == "__main__":
    unittest.main()
