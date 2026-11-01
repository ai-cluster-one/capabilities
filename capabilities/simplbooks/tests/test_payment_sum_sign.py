from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "simplbooks", CAPABILITY / "simplbooks")
    if path.is_file()), CAPABILITY / "bin" / "simplbooks")


def _load_module():
    name = "simplbooks_payment_sum_sign_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


class _Posted(Exception):
    """Stops a save at the form, before anything reaches SB."""


def _row(bt_sum: float) -> dict:
    return {"sum": bt_sum, "date": "2026-10-28", "income_account_id": "1",
            "transaction_note": "note", "party_name": "Supplier"}


class BankRowPaymentSumTests(unittest.TestCase):
    def _save(self, bt_sum: float, *args: str):
        seen: dict = {}

        def fields(**kw):
            seen.update(kw)
            raise _Posted()

        with mock.patch.object(simplbooks, "_bt_load_one",
                               return_value=(_row(bt_sum), "csrf", {}, "acct")), \
             mock.patch.object(simplbooks, "load_clients_cache",
                               return_value={"42": {"id": 42, "name": "Supplier"}}), \
             mock.patch.object(simplbooks, "_bt_money_save_fields", side_effect=fields):
            result = CliRunner().invoke(simplbooks.cli, [
                "bank-transactions", "save", "1001", "--as", "payment",
                "--client-id", "42", *args])
        return result, seen.get("sum_amount")

    def test_an_outflow_rows_minus_is_dropped_not_posted(self) -> None:
        result, posted = self._save(-25.40, "--sum=-25.40")
        self.assertIsInstance(result.exception, _Posted)
        self.assertEqual(posted, "25.40")

    def test_an_estonian_minus_is_dropped_too(self) -> None:
        _, posted = self._save(-25.40, "--sum=-25,40")
        self.assertEqual(posted, "25.40")

    def test_a_positive_sum_still_reaches_the_form(self) -> None:
        _, posted = self._save(-25.40, "--sum", "10")
        self.assertEqual(posted, "10")

    def test_no_sum_still_takes_the_rows_own_amount(self) -> None:
        _, posted = self._save(-25.40)
        self.assertEqual(posted, "25.40")

    def test_a_minus_on_an_inflow_row_is_refused(self) -> None:
        result, posted = self._save(25.40, "--sum=-25.40")
        self.assertEqual(result.exit_code, 6)
        self.assertIsNone(posted)
        self.assertIn("inflow", result.output)

    def test_a_zero_sum_is_refused(self) -> None:
        result, posted = self._save(-25.40, "--sum", "0")
        self.assertEqual(result.exit_code, 6)
        self.assertIsNone(posted)


class CreateMoneySumTests(unittest.TestCase):
    def _create(self, verb: str, amount: str):
        with mock.patch.object(simplbooks, "_create_money",
                               side_effect=_Posted()) as create:
            result = CliRunner().invoke(simplbooks.cli, [
                verb, "create", "--client-id", "42", "--bank-cashbook-id", "1",
                "--sum", amount, "--date", "2026-10-28"])
        return result, create.called

    def test_a_negative_or_zero_sum_is_refused_before_anything_is_sent(self) -> None:
        for verb in ("payments", "incomings"):
            for amount in ("-25.40", "-25,40", "0", "0,00"):
                with self.subTest(verb=verb, amount=amount):
                    result, called = self._create(verb, amount)
                    self.assertEqual(result.exit_code, 6, result.output)
                    self.assertFalse(called)

    def test_a_positive_sum_goes_through(self) -> None:
        for verb in ("payments", "incomings"):
            with self.subTest(verb=verb):
                result, called = self._create(verb, "25,40")
                self.assertTrue(called)
                self.assertIsInstance(result.exception, _Posted)


if __name__ == "__main__":
    unittest.main()
