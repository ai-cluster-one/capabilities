from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

import click


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "simplbooks", CAPABILITY / "simplbooks")
    if path.is_file()), CAPABILITY / "bin" / "simplbooks")


def _load_module():
    name = "simplbooks_account_id_spaces_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


CHART_ROWS = [
    {"id": 1, "code": "1010", "name": "Kassa"},
    {"id": 3, "code": "1030", "name": "Raha teel"},
    {"id": 211, "code": "1025", "name": "Swedbank pangakonto"},
    {"id": 248, "code": "1024", "name": "Stripe"},
    {"id": 120, "code": "5050", "name": "Kontorikulud"},
]
CASHBOOK_ROWS = [
    {"id": 1, "code": "", "name": "Kassa"},
    {"id": 3, "code": "", "name": "Swedbank"},
    {"id": 7, "code": "", "name": "Stripe"},
]


def _indexes():
    """Patch the live lookups with a fixed chart + cashbook list."""
    return (
        mock.patch.object(simplbooks, "_chart_index",
                          return_value=simplbooks._index_accounts(CHART_ROWS)),
        mock.patch.object(simplbooks, "_chart_index_all",
                          return_value=simplbooks._index_accounts(CHART_ROWS)),
        mock.patch.object(simplbooks, "_cashbook_index",
                          return_value=simplbooks._index_accounts(CASHBOOK_ROWS)),
    )


class ParseGateTests(unittest.TestCase):
    """Layer one: pure, so it runs before any lookup and before any request."""

    def test_coa_code_accepts_a_four_digit_code(self) -> None:
        self.assertEqual(simplbooks._parse_coa_code("1030", "--counter-coa"), "1030")

    def test_coa_code_accepts_a_five_digit_code(self) -> None:
        self.assertEqual(simplbooks._parse_coa_code("14301", "--counter-coa"), "14301")

    def test_coa_code_refuses_a_cashbook_id(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_coa_code("3", "--counter-coa")
        self.assertIn("CODE", str(caught.exception))

    def test_coa_code_refuses_an_internal_id(self) -> None:
        # The three-digit zone belongs to neither space.
        with self.assertRaises(click.ClickException):
            simplbooks._parse_coa_code("211", "--counter-coa")

    def test_coa_code_refuses_non_numeric(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._parse_coa_code("Swedbank", "--counter-coa")

    def test_cashbook_id_accepts_one_and_two_digits(self) -> None:
        self.assertEqual(simplbooks._parse_cashbook_id("3", "--bank-cashbook-id"), "3")
        self.assertEqual(simplbooks._parse_cashbook_id("12", "--bank-cashbook-id"), "12")

    def test_cashbook_id_refuses_a_chart_code(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._parse_cashbook_id("1025", "--bank-cashbook-id")

    def test_cashbook_id_refuses_an_internal_id(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._parse_cashbook_id("211", "--bank-cashbook-id")

    def test_cashbook_id_refuses_zero(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._parse_cashbook_id("0", "--bank-cashbook-id")


class MembershipTests(unittest.TestCase):
    """Layer two: the value must exist in the live listing for its own space."""

    def test_code_resolves_to_the_internal_id(self) -> None:
        with self._patched():
            self.assertEqual(simplbooks._coa_by_code("1030", "--counter-coa")["id"], "3")

    def test_the_5050_incident_now_resolves(self) -> None:
        # 5050 was posted as an id and dangled; as a code it resolves to id 120.
        with self._patched():
            self.assertEqual(simplbooks._coa_by_code("5050", "--set-line coa=")["id"], "120")

    def test_unknown_code_is_refused(self) -> None:
        with self._patched():
            with self.assertRaises(click.ClickException) as caught:
                simplbooks._coa_by_code("9999", "--counter-coa")
        self.assertIn("no account with code 9999", str(caught.exception))

    def test_inactive_code_is_named_as_inactive(self) -> None:
        with (
            mock.patch.object(simplbooks, "_chart_index",
                              return_value=simplbooks._index_accounts(CHART_ROWS[:1])),
            mock.patch.object(simplbooks, "_chart_index_all",
                              return_value=simplbooks._index_accounts(CHART_ROWS)),
        ):
            with self.assertRaises(click.ClickException) as caught:
                simplbooks._coa_by_code("1030", "--counter-coa")
        self.assertIn("not in the ACTIVE chart", str(caught.exception))

    def test_a_duplicated_code_is_refused_as_ambiguous(self) -> None:
        doubled = [*CHART_ROWS, {"id": 999, "code": "1030", "name": "Raha teel (dup)"}]
        with (
            mock.patch.object(simplbooks, "_chart_index",
                              return_value=simplbooks._index_accounts(doubled)),
            mock.patch.object(simplbooks, "_chart_index_all",
                              return_value=simplbooks._index_accounts(doubled)),
        ):
            with self.assertRaises(click.ClickException) as caught:
                simplbooks._coa_by_code("1030", "--counter-coa")
        self.assertIn("more than one chart account", str(caught.exception))

    def test_unknown_cashbook_id_is_refused(self) -> None:
        with self._patched():
            with self.assertRaises(click.ClickException) as caught:
                simplbooks._cashbook_by_id("9", "--bank-cashbook-id")
        self.assertIn("not a live cashbook id", str(caught.exception))

    def _patched(self):
        from contextlib import ExitStack
        stack = ExitStack()
        for patcher in _indexes():
            stack.enter_context(patcher)
        return stack


class FieldPathTests(unittest.TestCase):
    """The space is keyed on the FULL form-field path — the leaf name is overloaded."""

    def test_income_account_id_is_coa_inside_an_invoice_row(self) -> None:
        self.assertEqual(
            simplbooks._account_field_space("Task[0][income_account_id]"), "coa")

    def test_income_account_id_is_a_cashbook_inside_an_incoming(self) -> None:
        self.assertEqual(
            simplbooks._account_field_space("data[Incoming][income_account_id]"), "cashbook")

    def test_income_account_id_is_a_cashbook_inside_a_payment(self) -> None:
        self.assertEqual(
            simplbooks._account_field_space("data[Payment][income_account_id]"), "cashbook")

    def test_default_bank_account_is_a_cashbook_field(self) -> None:
        self.assertEqual(simplbooks._account_field_space("default_bank_account"), "cashbook")

    def test_journal_legs_are_coa(self) -> None:
        for name in (
            "data[FinancialTransactions][1][financial_account_id]",
            "data[FinancialTransaction][transactions][7734][financial_account_id]",
            "data[FinancialTransaction][income_financial_account_id]",
        ):
            self.assertEqual(simplbooks._account_field_space(name), "coa", name)

    def test_client_defaults_are_coa(self) -> None:
        for name in (
            "data[Client][trade_receivables_account_id]",
            "data[Client][trade_creditors_account_id]",
            "data[Client][expense_account_id]",
        ):
            self.assertEqual(simplbooks._account_field_space(name), "coa", name)

    def test_purchase_rows_are_coa(self) -> None:
        self.assertEqual(
            simplbooks._account_field_space(
                "data[PurchaseRows][12345][PurchaseRow][expense_account_id]"),
            "coa")

    def test_a_non_account_field_has_no_space(self) -> None:
        self.assertIsNone(simplbooks._account_field_space("data[Invoice][client_id]"))
        self.assertIsNone(simplbooks._account_field_space("Task[0][vat_type_id]"))


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        from contextlib import ExitStack
        self.stack = ExitStack()
        for patcher in _indexes():
            self.stack.enter_context(patcher)
        self.addCleanup(self.stack.close)

    def test_valid_body_reports_what_it_touched(self) -> None:
        touched = simplbooks._validate_account_fields([
            ("Task[0][income_account_id]", "120"),
            ("default_bank_account", "3"),
            ("Task[0][vat_type_id]", "34"),
        ])
        self.assertEqual(
            [(t["field"], t["space"], t["code"], t["name"]) for t in touched],
            [("Task[0][income_account_id]", "coa", "5050", "Kontorikulud"),
             ("default_bank_account", "cashbook", "", "Swedbank")],
        )

    def test_a_cashbook_id_in_a_cashbook_field_is_fine(self) -> None:
        touched = simplbooks._validate_account_fields(
            [("data[Incoming][income_account_id]", "7")])
        self.assertEqual(touched[0]["name"], "Stripe")

    def test_a_cashbook_only_id_in_a_coa_field_is_refused(self) -> None:
        # Cashbook 7 (Stripe) is not a chart id in this fixture — the raw --set
        # escape hatch is where such a value would otherwise slip through.
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._validate_account_fields(
                [("data[FinancialTransactions][1][financial_account_id]", "7")])
        self.assertIn("not a live chart-of-accounts id", str(caught.exception))

    def test_a_chart_id_in_a_cashbook_field_is_refused(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._validate_account_fields(
                [("data[Payment][income_account_id]", "211")])
        self.assertIn("not a live cashbook id", str(caught.exception))

    def test_default_bank_account_is_not_blessed_by_chart_membership(self) -> None:
        # 248 is a live COA id (Stripe) but not a cashbook id: a chart-membership
        # check keyed on the bare name would wrongly accept it here.
        with self.assertRaises(click.ClickException):
            simplbooks._validate_account_fields([("default_bank_account", "248")])

    def test_unset_account_fields_are_skipped(self) -> None:
        self.assertEqual(
            simplbooks._validate_account_fields([
                ("data[Client][trade_receivables_account_id]", ""),
                ("data[Client][expense_account_id]", "0"),
            ]),
            [],
        )

    def test_non_numeric_value_is_refused(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._validate_account_fields([("default_bank_account", "Swedbank")])

    def test_multipart_bodies_are_read(self) -> None:
        touched = simplbooks._validate_account_fields([
            ("data[FinancialTransactions][a][financial_account_id]", (None, "3")),
            ("data[FinancialTransactionsRootDocumentFile]", ("", b"", "application/pdf")),
        ])
        self.assertEqual(touched[0]["code"], "1030")

    def test_duplicate_field_values_are_reported_once(self) -> None:
        touched = simplbooks._validate_account_fields([
            ("default_bank_account", "3"),
            ("default_bank_account", "3"),
        ])
        self.assertEqual(len(touched), 1)

    def test_echo_lines_carry_code_and_name(self) -> None:
        lines = simplbooks._account_echo_lines(
            simplbooks._validate_account_fields([
                ("Task[0][income_account_id]", "3"),
                ("default_bank_account", "7"),
            ]))
        self.assertEqual(lines[0],
                         "1030 Raha teel (COA id 3) ← Task[0][income_account_id]")
        self.assertEqual(lines[1], "cashbook 7 Stripe ← default_bank_account")


class LineKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        from contextlib import ExitStack
        self.stack = ExitStack()
        for patcher in _indexes():
            self.stack.enter_context(patcher)
        self.addCleanup(self.stack.close)

    def test_coa_key_resolves_to_the_internal_id(self) -> None:
        self.assertEqual(simplbooks._line_account_id({"coa": "1025"}, "--line"), "211")

    def test_coa_key_refuses_a_wrong_space_value(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._line_account_id({"coa": "3"}, "--line")

    def test_legacy_keys_pass_the_raw_id_through(self) -> None:
        # Item 5: the legacy path is existence-validated at the POST, never
        # range-rejected — debiting COA id 3 is a legitimate booking.
        self.assertEqual(simplbooks._line_account_id({"acct": "3"}, "--line"), "3")
        self.assertEqual(simplbooks._line_account_id({"account": "120"}, "--line"), "120")

    def test_no_account_key_returns_none(self) -> None:
        self.assertIsNone(simplbooks._line_account_id({"sum": "10,00"}, "--line"))

    def test_two_account_keys_are_refused(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._line_account_id({"coa": "1025", "acct": "211"}, "--line")


class FlagPairTests(unittest.TestCase):
    def setUp(self) -> None:
        from contextlib import ExitStack
        self.stack = ExitStack()
        for patcher in _indexes():
            self.stack.enter_context(patcher)
        self.addCleanup(self.stack.close)

    def test_new_flag_resolves_the_code(self) -> None:
        self.assertEqual(
            simplbooks._one_account_flag("1024", None, "--income-coa", "--income-account-id"),
            "248")

    def test_legacy_flag_passes_the_id_through(self) -> None:
        self.assertEqual(
            simplbooks._one_account_flag(None, 248, "--income-coa", "--income-account-id"),
            "248")

    def test_both_flags_are_refused(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._one_account_flag("1024", 248, "--income-coa", "--income-account-id")

    def test_neither_flag_is_none(self) -> None:
        self.assertIsNone(
            simplbooks._one_account_flag(None, None, "--income-coa", "--income-account-id"))

    def test_required_cashbook_flag_reports_its_absence(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._one_cashbook_flag(None, None, "--bank-cashbook-id", "--account-id")
        self.assertIn("--bank-cashbook-id is required", str(caught.exception))

    def test_optional_cashbook_flag_may_be_absent(self) -> None:
        self.assertIsNone(simplbooks._one_cashbook_flag(
            None, None, "--bank-cashbook-id", "--account-id", required=False))


if __name__ == "__main__":
    unittest.main()
