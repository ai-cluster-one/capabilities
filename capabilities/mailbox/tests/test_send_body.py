from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "mailbox", CAPABILITY / "mailbox")
    if path.is_file()), CAPABILITY / "bin" / "mailbox")


def _load_module():
    name = "mailbox_send_body_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


mailbox = _load_module()


class ResolveSendBodyTests(unittest.TestCase):
    def _stdin(self, text):
        return mock.patch.object(mailbox.sys, "stdin", io.StringIO(text))

    def test_body_dash_reads_stdin(self):
        with self._stdin("piped body\n"):
            self.assertEqual(
                mailbox._resolve_send_body("-", None), "piped body\n")

    def test_body_text_is_verbatim(self):
        self.assertEqual(
            mailbox._resolve_send_body("hello there", None), "hello there")

    def test_body_text_matching_a_path_stays_verbatim(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write("file content")
            f.flush()
            self.assertEqual(
                mailbox._resolve_send_body(f.name, None), f.name)

    def test_body_and_body_file_together_refused(self):
        with self.assertRaises(SystemExit) as caught:
            mailbox._resolve_send_body("text", "somefile.txt")
        self.assertEqual(caught.exception.code, 6)

    def test_body_file_dash_reads_stdin(self):
        with self._stdin("piped body\n"):
            self.assertEqual(
                mailbox._resolve_send_body(None, "-"), "piped body\n")

    def test_body_file_reads_existing_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write("file content\n")
            f.flush()
            self.assertEqual(
                mailbox._resolve_send_body(None, f.name), "file content\n")

    def test_body_file_missing_path_refused(self):
        with self.assertRaises(SystemExit) as caught:
            mailbox._resolve_send_body(None, "/no/such/file.txt")
        self.assertEqual(caught.exception.code, 6)

    def test_no_body_arguments_reads_stdin(self):
        with self._stdin("piped body\n"):
            self.assertEqual(
                mailbox._resolve_send_body(None, None), "piped body\n")


class SendWiringTests(unittest.TestCase):
    def test_main_routes_body_dash_through_stdin(self):
        argv = ["mailbox", "send", "--to", "r@example.com",
                "--subject", "Subject", "--body", "-"]
        cfg = {"id": "default", "allow_write": True}
        with (
            mock.patch.object(mailbox.sys, "argv", argv),
            mock.patch.object(mailbox.sys, "stdin", io.StringIO("piped body\n")),
            mock.patch.object(mailbox, "_gate"),
            mock.patch.object(mailbox, "_load_config", return_value=cfg),
            mock.patch.object(mailbox, "_write_gate"),
            mock.patch.object(mailbox, "cmd_send", return_value={}) as sent,
            mock.patch.object(mailbox, "_emit"),
        ):
            mailbox.main()
        self.assertEqual(sent.call_args.args[4], "piped body\n")


if __name__ == "__main__":
    unittest.main()
