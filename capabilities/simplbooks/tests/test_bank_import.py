"""`bank-transactions import` — the PSD2 re-import surface.

The verb is dry-run by default and every parameter it posts is scraped from the
live form, so these tests cover: the form scrape, the date shape SimpleBooks
actually takes, the cashbook gate (both id-space layers plus the bank link's own
option list), and the guarantee that a dry run posts nothing.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import click
from click.testing import CliRunner


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "simplbooks"


def _load_module():
    name = "simplbooks_bank_import_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


CHART_ROWS = [
    {"id": 211, "code": "1025", "name": "Swedbank pangakonto 3368"},
    {"id": 248, "code": "1024", "name": "Stripe Juko"},
]
CASHBOOK_ROWS = [
    {"id": 1, "code": "", "name": "Kassa"},
    {"id": 3, "code": "", "name": "Swedbank"},
    {"id": 7, "code": "", "name": "Stripe"},
]

# Trimmed from the live /dashboard render.
DASHBOARD = """
<div class="offcanvas" id="offcanvas-bank-import-options">
  <div class="tab-pane fade" id="bank-import-tab-swedbank">
    <form method="post" class="bank-import-form"
          action="/acct/bank_transactions/import_from_bank">
      <input type="hidden" name="data[source]" value="swedbank">
      <input class="b-datepicker" name="data[since]" type="text" value="01.08.2026">
      <input class="b-datepicker" name="data[until]" type="text" value="14.08.2026">
      <select class="form-select" name="data[income_account_id]">
        <option value="3">EE112200221048693368   Swedbank</option>
      </select>
      <input autocomplete="off" name="_csrfToken" type="hidden" value="CSRF-TOKEN">
    </form>
  </div>
  <form id="import-file-form" action="/acct/bank_transactions/import" method="post">
    <input name="import[data_file]" type="file">
  </form>
</div>
"""


class _Resp:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.is_redirect = False


class _Http:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.headers: dict = {}
        self.posts: list = []

    def get(self, url: str) -> _Resp:
        return self._resp

    def post(self, url: str, **kwargs) -> _Resp:
        self.posts.append((url, kwargs))
        return _Resp("", 200)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DateShapeTests(unittest.TestCase):
    """The form's datepicker takes dd.mm.yyyy; the CLI's surface is ISO."""

    def test_iso_becomes_estonian(self) -> None:
        self.assertEqual(simplbooks._bt_iso_to_ee("2026-01-31", "--to"), "31.01.2026")

    def test_an_estonian_date_on_the_cli_is_refused(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._bt_iso_to_ee("31.01.2026", "--from")
        self.assertIn("YYYY-MM-DD", str(caught.exception))

    def test_a_nonsense_date_is_refused(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._bt_iso_to_ee("2026-02-31", "--from")


class FormScrapeTests(unittest.TestCase):
    def test_the_live_form_supplies_source_csrf_and_cashbooks(self) -> None:
        forms = simplbooks._bt_import_forms(_Http(_Resp(DASHBOARD)), "acct")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["source"], "swedbank")
        self.assertEqual(forms[0]["csrf"], "CSRF-TOKEN")
        self.assertEqual([o["cashbook_id"] for o in forms[0]["cashbooks"]], ["3"])

    def test_the_file_upload_form_is_not_mistaken_for_it(self) -> None:
        forms = simplbooks._bt_import_forms(_Http(_Resp(DASHBOARD)), "acct")
        self.assertTrue(all(f["action"].endswith("import_from_bank") for f in forms))

    def test_no_form_refuses_rather_than_guessing_a_payload(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._bt_import_forms(_Http(_Resp("<html></html>")), "acct")
        self.assertIn("refusing to post a guessed payload", str(caught.exception).lower())

    def test_a_non_200_dashboard_refuses(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._bt_import_forms(_Http(_Resp("", 503)), "acct")


class FieldSpaceTests(unittest.TestCase):
    def test_the_import_cashbook_field_is_in_the_cashbook_space(self) -> None:
        self.assertEqual(
            simplbooks._account_field_space("data[income_account_id]"), "cashbook")


class _CommandCase(unittest.TestCase):
    def setUp(self) -> None:
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.http = _Http(_Resp(DASHBOARD))
        for patcher in (
            mock.patch.object(simplbooks, "_chart_index",
                              return_value=simplbooks._index_accounts(CHART_ROWS)),
            mock.patch.object(simplbooks, "_chart_index_all",
                              return_value=simplbooks._index_accounts(CHART_ROWS)),
            mock.patch.object(simplbooks, "_cashbook_index",
                              return_value=simplbooks._index_accounts(CASHBOOK_ROWS)),
            mock.patch.object(simplbooks, "session_or_die", return_value=({}, "acct")),
            mock.patch.object(simplbooks, "build_http", return_value=self.http),
            mock.patch.object(simplbooks, "_bt_worklist_snapshot", return_value={}),
            mock.patch.object(simplbooks, "_CONN", None),
        ):
            self.stack.enter_context(patcher)
        self.runner = CliRunner()

    def run_import(self, *args):
        return self.runner.invoke(simplbooks.cli, ["bank-transactions", "import", *args])


class DryRunTests(_CommandCase):
    DEFAULTS = ("--from", "2026-01-01", "--to", "2026-01-31", "--bank-cashbook-id", "3")

    def test_a_dry_run_posts_nothing(self) -> None:
        result = self.run_import(*self.DEFAULTS)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.http.posts, [])
        self.assertIn("DRY RUN", result.output)

    def test_the_dry_run_shows_the_body_it_would_send(self) -> None:
        result = self.run_import(*self.DEFAULTS, "--json")
        payload = json.loads(result.output)
        self.assertEqual(
            {k: v for k, v in payload["would_post"]},
            {"data[source]": "swedbank", "data[since]": "01.01.2026",
             "data[until]": "31.01.2026", "data[income_account_id]": "3"},
        )
        self.assertFalse(payload["applied"])

    def test_the_csrf_token_is_not_echoed(self) -> None:
        result = self.run_import(*self.DEFAULTS, "--json")
        self.assertNotIn("CSRF-TOKEN", result.output)

    def test_the_cashbook_is_echoed_by_name(self) -> None:
        result = self.run_import(*self.DEFAULTS)
        self.assertIn("cashbook 3 Swedbank", result.output)

    def test_the_dedupe_scope_is_declared_unverified(self) -> None:
        result = self.run_import(*self.DEFAULTS, "--json")
        self.assertIn("unverified", json.loads(result.output)["dedupe_scope"])


class GateTests(_CommandCase):
    def test_a_chart_code_in_the_cashbook_flag_is_refused_at_parse(self) -> None:
        result = self.run_import("--from", "2026-01-01", "--to", "2026-01-31",
                                 "--bank-cashbook-id", "1025")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cashbook id", result.output)
        self.assertEqual(self.http.posts, [])

    def test_a_cashbook_the_bank_link_does_not_offer_is_refused(self) -> None:
        # 7 (Stripe) is a live cashbook, but the swedbank link imports only into 3.
        result = self.run_import("--from", "2026-01-01", "--to", "2026-01-31",
                                 "--bank-cashbook-id", "7")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not import into it", result.output)

    def test_an_unknown_source_is_refused(self) -> None:
        result = self.run_import("--from", "2026-01-01", "--to", "2026-01-31",
                                 "--bank-cashbook-id", "3", "--source", "seb")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not offered by this account", result.output)

    def test_a_reversed_window_is_refused(self) -> None:
        result = self.run_import("--from", "2026-02-01", "--to", "2026-01-31",
                                 "--bank-cashbook-id", "3")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--to is before --from", result.output)

    def test_the_window_is_required(self) -> None:
        self.assertNotEqual(self.run_import("--bank-cashbook-id", "3").exit_code, 0)


class ApplyTests(_CommandCase):
    """--apply MEASURES the dedupe scope; it never asserts it."""

    def _rows(self, spec):
        return {i: {"id": i, "date": "2026-01-15", "sum": -1.0, "status": s,
                    "direction": "out", "party_name": "", "transaction_note": "",
                    "default_object_type": "payment"}
                for i, s in spec.items()}

    def test_apply_posts_the_scraped_body_including_the_csrf(self) -> None:
        with mock.patch.object(simplbooks, "_bt_worklist_snapshot",
                               side_effect=[{}, self._rows({9001: "valid"})]):
            result = self.run_import("--from", "2026-01-01", "--to", "2026-01-31",
                                     "--bank-cashbook-id", "3", "--apply")
        self.assertEqual(result.exit_code, 0, result.output)
        url, kwargs = self.http.posts[0]
        self.assertTrue(url.endswith("/bank_transactions/import_from_bank"))
        self.assertIn("_csrfToken=CSRF-TOKEN", kwargs["content"])
        self.assertIn("data%5Bsince%5D=01.01.2026", kwargs["content"])

    def test_new_rows_are_counted_by_status(self) -> None:
        with mock.patch.object(
                simplbooks, "_bt_worklist_snapshot",
                side_effect=[self._rows({1: "valid"}),
                             self._rows({1: "valid", 2: "duplicate", 3: "valid"})]):
            result = self.run_import("--from", "2026-01-01", "--to", "2026-01-31",
                                     "--bank-cashbook-id", "3", "--apply", "--json")
        payload = json.loads(result.output)
        self.assertEqual(payload["created"]["count"], 2)
        self.assertEqual(payload["created"]["by_status"], {"duplicate": 1, "valid": 1})
        self.assertEqual(payload["created"]["ids"], [2, 3])
        self.assertIn("1 of 2", payload["dedupe_observation"])

    def test_a_pre_existing_row_that_flips_status_is_reported(self) -> None:
        with mock.patch.object(
                simplbooks, "_bt_worklist_snapshot",
                side_effect=[self._rows({1: "valid"}), self._rows({1: "duplicate"})]):
            result = self.run_import("--from", "2026-01-01", "--to", "2026-01-31",
                                     "--bank-cashbook-id", "3", "--apply", "--json")
        payload = json.loads(result.output)
        self.assertEqual(payload["existing_rows_restatused"],
                         [{"id": 1, "from": "valid", "to": "duplicate"}])
        self.assertEqual(payload["created"]["count"], 0)


class BulkDeleteTests(unittest.TestCase):
    """Item 31's prerequisite is unmet — no bulk delete is exposed, in any form."""

    def test_no_bulk_delete_verb_exists(self) -> None:
        names = simplbooks.bank_transactions.commands.keys()
        self.assertNotIn("delete-all", names)
        self.assertNotIn("delete_all", names)
        self.assertEqual(
            sorted(names), ["delete", "import", "list", "save", "show"])

    def test_the_delete_all_endpoint_is_never_addressed_in_code(self) -> None:
        # It is documented in a comment (why it stays unexposed) and nowhere else.
        code = [line for line in SCRIPT.read_text().splitlines()
                if not line.lstrip().startswith("#")]
        self.assertEqual(
            [line for line in code if "bank_transactions/delete_all" in line], [])


if __name__ == "__main__":
    unittest.main()
