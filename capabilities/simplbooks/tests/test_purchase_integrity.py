from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click
import httpx
from click.testing import CliRunner


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "simplbooks", CAPABILITY / "simplbooks")
    if path.is_file()), CAPABILITY / "bin" / "simplbooks")


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


VIEW_ROWS = """
<div class="attachments-manager attachments-manager--list">
    <div class="attachments-manager__row"
         data-attachment-id="9001"
         data-file-type="pdf"
         data-preview-url="/account/purchases/view_file/501?attachment_id=9001"
         role="button"
         aria-label="Eelvaade first.pdf">
        <div class="attachments-manager__row-name" title="first.pdf">first.pdf</div>
        <span class="attachments-manager__row-star attachments-manager__row-star--primary"></span>
    </div>
    <div class="attachments-manager__row"
         data-attachment-id="9002"
         data-file-type="pdf"
         data-preview-url="/account/purchases/view_file/501?attachment_id=9002"
         role="button"
         aria-label="Eelvaade first.pdf">
        <div class="attachments-manager__row-name" title="first.pdf">first.pdf</div>
    </div>
</div>
"""

VIEW_EMPTY = """
<div class="attachments-manager attachments-manager--list">
    <div class="attachments-manager__empty">Manuseid pole veel lisatud.</div>
</div>
"""

EDIT_FILES = """
<div class="attachments-manager col-12">
  <input type="hidden" name="data[Purchase][attachments_existing][]" value="9101">
  <input type="file" name="data[Purchase][attachments][]" class="js-attachments-manager-input"
     data-files="[{&quot;id&quot;:9101,&quot;name&quot;:&quot;scan.png&quot;,&quot;size&quot;:690453,&quot;type&quot;:&quot;&quot;,&quot;file_type&quot;:&quot;png&quot;,&quot;is_primary&quot;:true,&quot;preview_url&quot;:&quot;\\/account\\/purchases\\/view_file\\/502?attachment_id=9101&quot;,&quot;download_url&quot;:&quot;\\/account\\/purchases\\/view_file\\/502?attachment_id=9101&quot;}]">
</div>
"""

EDIT_EMPTY = """
<div class="attachments-manager col-12">
  <input type="file" name="data[Purchase][attachments][]" class="js-attachments-manager-input"
         data-files="[]">
</div>
"""


def _soup(html: str):
    return simplbooks.BeautifulSoup(html, "html.parser")


class AttachmentParserTests(unittest.TestCase):
    def test_view_page_rows_carry_id_name_type_and_primary(self) -> None:
        attachments = simplbooks._parse_purchase_attachments(VIEW_ROWS, "account")
        self.assertEqual(
            [a["attachment_id"] for a in attachments], ["9001", "9002"]
        )
        self.assertEqual(attachments[0], {
            "attachment_id": "9001",
            "file_id": "9001",
            "name": "first.pdf",
            "type": "pdf",
            "size": None,
            "is_primary": True,
            "url": "https://app.simplbooks.com/account/purchases/view_file/501?attachment_id=9001",
        })
        self.assertFalse(attachments[1]["is_primary"])

    def test_edit_form_json_carries_size(self) -> None:
        attachments = simplbooks._parse_purchase_attachments(EDIT_FILES, "account")
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["attachment_id"], "9101")
        self.assertEqual(attachments[0]["name"], "scan.png")
        self.assertEqual(attachments[0]["type"], "png")
        self.assertEqual(attachments[0]["size"], 690453)
        self.assertTrue(attachments[0]["is_primary"])
        self.assertEqual(
            attachments[0]["url"],
            "https://app.simplbooks.com/account/purchases/view_file/502?attachment_id=9101",
        )

    def test_rendered_empty_states_are_true_negatives(self) -> None:
        for html in (VIEW_EMPTY, EDIT_EMPTY):
            self.assertEqual(simplbooks._parse_purchase_attachments(html, "account"), [])
            self.assertTrue(simplbooks._purchase_attachments_surface(_soup(html)))

    def test_missing_surface_is_not_an_empty_purchase(self) -> None:
        self.assertFalse(simplbooks._purchase_attachments_surface(_soup("<div>nothing</div>")))

    def test_view_file_path_segment_is_the_purchase_not_an_attachment(self) -> None:
        # The download URL names the purchase and carries the attachment in its
        # query, so a bare link to it must never be read as an attachment of its own.
        html = """<a href="/account/purchases/view_file/1884">view</a>"""
        self.assertEqual(simplbooks._parse_purchase_attachments(html, "account"), [])


class MultipartTests(unittest.TestCase):
    def test_upload_field_is_the_attachments_manager_input(self) -> None:
        # Pinned to the name SimpleBooks renders on #purchase-form; a file posted
        # under any other name is accepted by the endpoint and silently dropped.
        self.assertEqual(
            simplbooks.ATTACHMENTS_UPLOAD_FIELD, "data[Purchase][attachments][]"
        )

    def test_create_and_update_attachment_builder_carries_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invoice.pdf"
            source.write_bytes(b"pdf bytes")
            parts = simplbooks._purchase_multipart_parts([("field", "value")], str(source))
        self.assertEqual(parts[0], ("field", (None, "value")))
        self.assertEqual(
            parts[1],
            (
                "data[Purchase][attachments][]",
                ("invoice.pdf", b"pdf bytes", "application/pdf"),
            ),
        )

    def test_no_attachment_still_submits_the_empty_file_part(self) -> None:
        parts = simplbooks._purchase_multipart_parts([("field", "value")], None)
        self.assertEqual(
            parts[-1],
            ("data[Purchase][attachments][]", ("", b"", "application/octet-stream")),
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
                return httpx.Response(200, text=VIEW_ROWS.replace("/501?", "/42?"))
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


    def test_unreadable_markup_refuses_instead_of_reporting_a_miss(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<div>markup moved</div>")

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invoice.pdf"
            source.write_bytes(b"expected PDF")
            with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(click.ClickException):
                    simplbooks._purchase_attachment_matches(http, "account", 42, str(source))


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
        message = str(caught.exception)
        self.assertIn("is stored", message)
        self.assertIn("Do not run create again", message)

    def test_document_only_failure_points_at_update_rather_than_a_second_create(self) -> None:
        with (
            mock.patch.object(simplbooks, "_purchase_read_rounding", return_value="-0,01"),
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
                    attachment_path="/tmp/invoice.pdf",
                )
        message = str(caught.exception)
        self.assertIn("Do not run create again", message)
        self.assertIn("purchases update 42 --attach /tmp/invoice.pdf", message)

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


def _edit_form(*account_ids: str) -> str:
    rows = "".join(
        f"""<select name="data[PurchaseRows][row{i}][PurchaseRow][expense_account_id]">
              <option value="{value}" selected="selected">chosen</option>
              <option value="999">other</option>
            </select>"""
        for i, value in enumerate(account_ids)
    )
    return f'<form id="purchase-form">{rows}</form>'


class RowAccountReadbackTests(unittest.TestCase):
    CHART = {"by_id": {
        "701": {"code": "6100", "name": "Asked account"},
        "702": {"code": "6110", "name": "Stored account"},
    }}

    def _http(self, status: int, text: str) -> mock.Mock:
        return mock.Mock(get=mock.Mock(return_value=mock.Mock(status_code=status, text=text)))

    def test_reads_the_account_the_form_actually_has_selected(self) -> None:
        stored = simplbooks._purchase_stored_row_accounts(
            self._http(200, _edit_form("702", "701")), "account", 42
        )
        self.assertEqual(stored, ["702", "701"])

    def test_a_locked_purchase_reads_back_as_unknown_not_as_a_mismatch(self) -> None:
        with mock.patch.object(simplbooks, "_chart_index", return_value=self.CHART):
            verified = simplbooks._verify_stored_row_accounts(
                self._http(302, ""), "account", 42, ["701"]
            )
        self.assertFalse(verified)

    def test_the_requested_account_surviving_is_a_pass(self) -> None:
        with mock.patch.object(simplbooks, "_chart_index", return_value=self.CHART):
            verified = simplbooks._verify_stored_row_accounts(
                self._http(200, _edit_form("701")), "account", 42, ["701"]
            )
        self.assertTrue(verified)

    def test_a_substituted_account_raises_and_names_both_codes(self) -> None:
        with mock.patch.object(simplbooks, "_chart_index", return_value=self.CHART):
            with self.assertRaises(click.ClickException) as caught:
                simplbooks._verify_stored_row_accounts(
                    self._http(200, _edit_form("702")), "account", 42, ["701"]
                )
        message = str(caught.exception)
        self.assertIn("6100", message)
        self.assertIn("6110", message)
        self.assertIn("purchases update 42", message)
        self.assertNotIn("create", message.split("rather than")[0])


class AttachmentDeleteUnbindTests(unittest.TestCase):
    """--force-unbind must never leave a purchase unbound after a failed delete."""

    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.bound = [("Payment", 77)]
        self.rebinds: list[tuple[str, int, list[int]]] = []
        for patcher in (
            mock.patch.object(simplbooks, "session_or_die", return_value=({}, "account")),
            mock.patch.object(simplbooks, "_purchase_remove_all_bindings"),
            mock.patch.object(
                simplbooks, "_do_bind",
                side_effect=lambda kind, bid, ids: self.rebinds.append((kind, bid, ids)),
            ),
        ):
            self.stack.enter_context(patcher)
        self.runner = CliRunner()

    def run_delete(self, *args):
        return self.runner.invoke(
            simplbooks.cli, ["purchases", "attachment-delete", "501", "9001", *args]
        )

    def test_a_failed_delete_puts_the_bindings_back(self) -> None:
        with (
            mock.patch.object(simplbooks, "_purchase_bindings", return_value=self.bound),
            mock.patch.object(
                simplbooks, "build_http", side_effect=httpx.ConnectError("no route")
            ),
        ):
            result = self.run_delete("--force-unbind")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.rebinds, [("Payment", 77, [501])])

    def test_without_the_flag_nothing_is_unbound(self) -> None:
        with (
            mock.patch.object(simplbooks, "_purchase_bindings", return_value=self.bound),
            mock.patch.object(simplbooks, "build_http", side_effect=httpx.ConnectError("x")),
        ):
            self.run_delete()
        self.assertEqual(self.rebinds, [])
        simplbooks._purchase_remove_all_bindings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
