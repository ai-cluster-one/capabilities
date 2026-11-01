from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx
from click.testing import CliRunner


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "simplbooks", CAPABILITY / "simplbooks")
    if path.is_file()), CAPABILITY / "bin" / "simplbooks")


def _load_module():
    name = "simplbooks_read_back_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


INCOMING_VIEW = """
<div class="summary-grid"><div>Kuupäev</div><div>01.02.2026</div>
<div>Summa</div><div>150,00</div></div>
<a href="/acct/clients/view/12">Customer</a>
<table><tr><th>Arve</th><th>Kuupäev</th><th>Katte summa</th></tr>
<tr><td><a href="/acct/invoices/view/301">301</a></td><td>01.02.2026</td><td>100,00</td></tr>
<tr><td><a href="/acct/invoices/view/302">302</a></td><td>01.02.2026</td><td>50,00</td></tr>
</table>
"""

PURCHASE_VIEW = """
<div class="text-success card-title">Supplier</div>
<div class="text-success card-title">x</div>
<div class="text-success card-title">Tasutud</div>
<div class="summary-grid"><div>Arve number</div><div>A-1</div></div>
<table><tr><th>Makse</th><th>Katte summa</th></tr>
<tr><td><a href="/acct/payments/view/77">77</a></td><td>19,76</td></tr></table>
"""

INVOICE_COPY = """
<input name="data[Invoice][client_id]" value="12">
<textarea name="additional_info">copied text</textarea>
"""
INVOICE_EDIT = """<textarea name="additional_info">saved text</textarea>"""


class _Case(unittest.TestCase):
    pages: dict[str, httpx.Response] = {}

    def setUp(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            for path, response in self.pages.items():
                if str(request.url).endswith(path):
                    return response
            return httpx.Response(404)

        for patcher in (
            mock.patch.object(simplbooks, "session_or_die", return_value=({}, "acct")),
            mock.patch.object(simplbooks, "build_http",
                              side_effect=lambda *a, **k: httpx.Client(
                                  transport=httpx.MockTransport(handler))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        result = CliRunner().invoke(simplbooks.cli, list(args))
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output


class MoneyShowTests(_Case):
    pages = {"/incomings/view/9": httpx.Response(200, text=INCOMING_VIEW),
             "/payments/view/8": httpx.Response(200, text=INCOMING_VIEW.replace(
                 "invoices/view", "purchases/view"))}

    def test_incoming_show_returns_record_and_bound_invoices(self) -> None:
        rec = json.loads(self.run_cli("incomings", "show", "9", "--json"))
        self.assertEqual(rec["id"], 9)
        self.assertEqual(rec["client_id"], "12")
        self.assertEqual(rec["fields"]["Summa"], "150,00")
        self.assertEqual(rec["bound"], [
            {"document": "invoice", "id": 301, "amount": "100,00"},
            {"document": "invoice", "id": 302, "amount": "50,00"},
        ])

    def test_payment_show_returns_bound_purchases(self) -> None:
        rec = json.loads(self.run_cli("payments", "show", "8", "--json"))
        self.assertEqual([b["id"] for b in rec["bound"]], [301, 302])
        self.assertEqual(rec["bound"][0]["document"], "purchase")


class InvoiceReferenceTests(_Case):
    def test_editable_invoice_reads_the_edit_form(self) -> None:
        self.pages = {"/invoices/add/5": httpx.Response(200, text=INVOICE_COPY),
                      "/invoices/edit/5": httpx.Response(200, text=INVOICE_EDIT)}
        out = json.loads(self.run_cli("invoices", "show", "5", "--json"))
        self.assertEqual(out["additional_info"], "saved text")
        self.assertEqual(out["additional_info_source"], "edit form")

    def test_locked_invoice_falls_back_to_the_copy_form(self) -> None:
        self.pages = {"/invoices/add/5": httpx.Response(200, text=INVOICE_COPY),
                      "/invoices/edit/5": httpx.Response(
                          302, headers={"location": "/acct/invoices/view/5"})}
        out = json.loads(self.run_cli("invoices", "show", "5", "--json"))
        self.assertEqual(out["additional_info"], "copied text")
        self.assertEqual(out["additional_info_source"], "copy form")
        self.assertEqual(out["client_id"], "12")


class PurchaseViewTests(_Case):
    pages = {"/purchases/view/42": httpx.Response(200, text=PURCHASE_VIEW)}

    def test_view_flag_reads_status_and_bindings_of_any_purchase(self) -> None:
        out = json.loads(self.run_cli("purchases", "show", "42", "--view", "--json"))
        self.assertEqual(out["status"], "Tasutud")
        self.assertEqual(out["bindings"], [{"kind": "payment", "id": 77, "amount": "19,76"}])
        self.assertNotIn("payment-locked", out["source"])


if __name__ == "__main__":
    unittest.main()
