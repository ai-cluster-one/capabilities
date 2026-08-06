from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click
import httpx


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "simplbooks"


def _load_module():
    name = "simplbooks_purchase_integrity_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


class AttachmentParserTests(unittest.TestCase):
    def test_parses_read_only_preview(self) -> None:
        html = """<a href="#" onclick="showFilePreview('1860', 'pdf'); return false;">view</a>"""
        self.assertEqual(
            simplbooks._parse_purchase_attachments(html, "account"),
            [
                {
                    "file_id": "1860",
                    "type": "pdf",
                    "url": "https://app.simplbooks.com/account/purchases/view_file/1860",
                    "name": "1860.pdf",
                }
            ],
        )

    def test_parses_edit_form_href_and_type(self) -> None:
        html = """
        <input type="hidden" name="data[Purchase][file_type]" value="PDF">
        <a href="/account/purchases/view_file/1884" id="ViewCopyButton">view</a>
        """
        attachments = simplbooks._parse_purchase_attachments(html, "account")
        self.assertEqual(attachments[0]["file_id"], "1884")
        self.assertEqual(attachments[0]["type"], "pdf")

    def test_deduplicates_preview_and_href_for_same_file(self) -> None:
        html = """
        <input type="hidden" name="data[Purchase][file_type]" value="pdf">
        <a href="/account/purchases/view_file/1884"
           onclick="showFilePreview('1884', 'pdf'); return false;">view</a>
        """
        self.assertEqual(len(simplbooks._parse_purchase_attachments(html, "account")), 1)


class MultipartTests(unittest.TestCase):
    def test_create_and_update_attachment_builder_carries_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invoice.pdf"
            source.write_bytes(b"pdf bytes")
            parts = simplbooks._purchase_multipart_parts([("field", "value")], str(source))
        self.assertEqual(parts[0], ("field", (None, "value")))
        self.assertEqual(
            parts[1],
            ("data[Purchase][copy]", ("invoice.pdf", b"pdf bytes", "application/pdf")),
        )

    def test_build_fields_serializes_rounding(self) -> None:
        fields = simplbooks.build_purchase_fields(
            csrf="csrf",
            supplier={"id": 1, "name": "Supplier"},
            number="N-1",
            internal_number="N-1",
            invoice_date=simplbooks.datetime(2026, 8, 1),
            due_date=simplbooks.datetime(2026, 8, 1),
            currency_code="EUR",
            currency_rate=1.0,
            lines=[
                {
                    "expense_account_id": "5000",
                    "vat_type_id": "34",
                    "vat": "24",
                    "name": "Service",
                    "amount": "1",
                    "unit": "tk",
                    "sum": "19,77",
                }
            ],
            vat_total="3,83",
            row_sum_with_vat=True,
            rounding="-0,01",
        )
        self.assertEqual(dict(fields)["data[Purchase][rounding]"], "-0,01")

    def test_expected_payable_includes_rounding(self) -> None:
        payable = simplbooks._purchase_expected_payable(
            [{"sum": "19,77"}], True, "3,83", "-0,01"
        )
        self.assertEqual(payable, simplbooks.Decimal("19.76"))


class AttachmentVerificationTests(unittest.TestCase):
    def test_compares_downloaded_attachment_bytes(self) -> None:
        expected = b"expected PDF"

        def handler(request: httpx.Request) -> httpx.Response:
            if "/purchases/view/42" in str(request.url):
                return httpx.Response(
                    200,
                    text='<a onclick="showFilePreview(\'42\', \'pdf\'); return false;">view</a>',
                )
            if "/purchases/view_file/42" in str(request.url):
                return httpx.Response(200, content=expected)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invoice.pdf"
            source.write_bytes(expected)
            with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                self.assertTrue(
                    simplbooks._purchase_attachment_matches(http, "account", 42, str(source))
                )


class CreateIntegrityTests(unittest.TestCase):
    def test_repairs_missing_rounding_and_attachment_once(self) -> None:
        with (
            mock.patch.object(
                simplbooks, "_purchase_read_rounding", side_effect=["0,00", "-0,01"]
            ),
            mock.patch.object(
                simplbooks, "_purchase_attachment_matches", side_effect=[False, True]
            ),
            mock.patch.object(simplbooks, "_purchase_update_created_integrity") as repair,
        ):
            repaired = simplbooks._ensure_created_purchase_integrity(
                mock.Mock(),
                "account",
                42,
                rounding="-0,01",
                expected_payable=simplbooks.Decimal("19.76"),
                attachment_path="invoice.pdf",
            )
        self.assertTrue(repaired)
        repair.assert_called_once_with(
            mock.ANY,
            "account",
            42,
            rounding="-0,01",
            attachment_path="invoice.pdf",
        )

    def test_reports_persistent_mismatch_after_repair(self) -> None:
        with (
            mock.patch.object(simplbooks, "_purchase_read_rounding", return_value="0,00"),
            mock.patch.object(simplbooks, "_purchase_attachment_matches", return_value=False),
            mock.patch.object(simplbooks, "_purchase_update_created_integrity"),
        ):
            with self.assertRaises(click.ClickException) as caught:
                simplbooks._ensure_created_purchase_integrity(
                    mock.Mock(),
                    "account",
                    42,
                    rounding="-0,01",
                    expected_payable=simplbooks.Decimal("19.76"),
                    attachment_path="invoice.pdf",
                )
        self.assertIn("readback still does not match", str(caught.exception))

    def test_locked_purchase_uses_payable_readback(self) -> None:
        with (
            mock.patch.object(simplbooks, "_purchase_read_rounding", return_value=None),
            mock.patch.object(simplbooks, "_purchase_read_payable", return_value="19,76"),
            mock.patch.object(simplbooks, "_purchase_update_created_integrity") as repair,
        ):
            repaired = simplbooks._ensure_created_purchase_integrity(
                mock.Mock(),
                "account",
                42,
                rounding="-0,01",
                expected_payable=simplbooks.Decimal("19.76"),
                attachment_path=None,
            )
        self.assertFalse(repaired)
        repair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
