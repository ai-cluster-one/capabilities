from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

import click


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "simplbooks", CAPABILITY / "simplbooks")
    if path.is_file()), CAPABILITY / "bin" / "simplbooks")


def _load_module():
    name = "simplbooks_report_parsing_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


# The live käibeandmik column descriptor, as SimplBooks emits it.
COLUMNS = [
    {"type": "name"},
    {"type": "balance_summed", "highlight": True}, {"type": "balance_summed"},
    {"type": "balance_expanded", "highlight": True}, {"type": "balance_expanded"},
    {"type": "change_summed", "highlight": True}, {"type": "change_summed"},
    {"type": "change_expanded", "highlight": True}, {"type": "change_expanded"},
    {"type": "final_summed", "highlight": True}, {"type": "final_summed"},
    {"type": "final_expanded", "highlight": True}, {"type": "final_expanded"},
]


def _payload(rows, columns=None):
    return {"header": ["GRUPP / KONTO"], "columns": columns or COLUMNS, "rows": rows}


def _row(label, account_id, cells):
    return {"account_id": account_id, "cells": [label, *cells]}


LEAF = _row("1010 Kassa", 1,
            [20057.78, "", 44946.13, 24888.35, "", 44.17, 22.31, 66.48,
             20013.61, "", 44968.44, 24954.83])
GROUP = _row("Varad", None,
             [65201.3, "", 1235421.87, 1170220.57, 263.07, "", 88625.91,
              88362.84, 65464.37, "", 1324047.78, 1258583.41])


class ReportNumberTests(unittest.TestCase):
    def test_empty_side_is_zero(self) -> None:
        self.assertEqual(simplbooks._report_num(""), 0.0)
        self.assertEqual(simplbooks._report_num(None), 0.0)

    def test_estonian_formatting_is_read(self) -> None:
        self.assertEqual(simplbooks._report_num("1 234,50"), 1234.50)
        self.assertEqual(simplbooks._report_num("1\xa0234,50"), 1234.50)

    def test_unparseable_cell_raises_instead_of_reading_zero(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._report_num("n/a", "row 4 closing debit")
        self.assertIn("row 4 closing debit", str(caught.exception))
        self.assertIn("Refusing", str(caught.exception))


class TrialBalanceTests(unittest.TestCase):
    def test_columns_resolve_by_declared_type(self) -> None:
        self.assertEqual(simplbooks._trial_balance_columns(_payload([])), (0, 1, 2, 11, 12))

    def test_columns_follow_an_inserted_column(self) -> None:
        shifted = [{"type": "row_marker"}, *COLUMNS]
        self.assertEqual(simplbooks._trial_balance_columns(_payload([], shifted)),
                         (1, 2, 3, 12, 13))

    def test_leaf_row_carries_code_name_and_balances(self) -> None:
        parsed = simplbooks._parse_trial_balance(_payload([LEAF]))
        self.assertEqual(parsed, [{
            "account_id": 1, "code": "1010", "name": "Kassa", "is_group": False,
            "opening": 20057.78, "closing": 20013.61,
        }])

    def test_group_row_is_marked_and_keeps_its_label(self) -> None:
        parsed = simplbooks._parse_trial_balance(_payload([GROUP]))
        self.assertTrue(parsed[0]["is_group"])
        self.assertEqual(parsed[0]["name"], "Varad")
        self.assertEqual(parsed[0]["code"], "")

    def test_missing_column_descriptor_raises(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_trial_balance({"rows": [LEAF]})
        self.assertIn("no `columns` descriptor", str(caught.exception))

    def test_a_renamed_column_type_raises(self) -> None:
        renamed = [dict(c) for c in COLUMNS]
        renamed[11]["type"] = "closing_expanded"
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_trial_balance(_payload([LEAF], renamed))
        self.assertIn("final_expanded", str(caught.exception))

    def test_short_row_raises_rather_than_shifting_figures(self) -> None:
        short = {"account_id": 1, "cells": ["1010 Kassa", 1.0, ""]}
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_trial_balance(_payload([short]))
        self.assertIn("3 cells against 13", str(caught.exception))

    def test_unparseable_figure_raises(self) -> None:
        broken = _row("1010 Kassa", 1, [20057.78, "", 0, 0, "", 0, 0, 0, 0, "", "??", 0])
        with self.assertRaises(click.ClickException):
            simplbooks._parse_trial_balance(_payload([broken]))

    def test_leaf_without_a_code_raises(self) -> None:
        unlabelled = _row("Kassa", 1, [0, "", 0, 0, "", 0, 0, 0, 0, "", 0, 0])
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_trial_balance(_payload([unlabelled]))
        self.assertIn("no leading account code", str(caught.exception))


class AccountLedgerTests(unittest.TestCase):
    HEAD = ["HEAD", ["Kande nr.", "%s", 7], ["1025 Swedbank", "%s", 0],
            ["Selgitus", "%s", 0], ["Kuupäev", "%s", 13], ["Deebet", "%.2f", 13],
            ["Kreedit", "%.2f", 13], ["Saldo", "%.2f", 13]]
    OPENING = ["ROW", "", "algsaldo", "", "01.01.2026", "184.42", "", "184.42"]
    MOVE = ["ROW", 7520, "Makse tarnijale Olerex AS", "", "02.01.2026", "", "100.02", "84.40"]
    PREFOOT = ["PREFOOT", "", "Valitud perioodi liikumiste saldo", "", "15.08.2026",
               "39349.69", "27736.97", "11612.72"]
    FOOT = ["FOOT", "", "Perioodi lõppsaldo", "", "15.08.2026",
            "39534.11", "27736.97", "11797.14"]

    def test_full_card_parses(self) -> None:
        led = simplbooks._parse_account_ledger(
            [self.HEAD, self.OPENING, self.MOVE, self.PREFOOT, self.FOOT])
        self.assertEqual(led["account"], "1025 Swedbank")
        self.assertEqual(led["opening"], 184.42)
        self.assertEqual(led["closing"], 11797.14)
        self.assertEqual(led["period"], {"debit": 39349.69, "credit": 27736.97,
                                         "net": 11612.72})
        self.assertEqual(len(led["moves"]), 1)
        self.assertEqual(led["moves"][0]["credit"], 100.02)

    def test_unknown_tag_raises_instead_of_being_skipped(self) -> None:
        subhead = ["SUBHEAD", "", "", "", "", "", "", ""]
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_account_ledger(
                [self.HEAD, self.OPENING, subhead, self.PREFOOT, self.FOOT])
        self.assertIn("unknown row tag", str(caught.exception))

    def test_short_element_raises(self) -> None:
        short = ["ROW", 7520, "Makse", "", "02.01.2026", "", "100.02"]
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_account_ledger(
                [self.HEAD, self.OPENING, short, self.PREFOOT, self.FOOT])
        self.assertIn("expected 8", str(caught.exception))

    def test_missing_foot_raises(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._parse_account_ledger([self.HEAD, self.OPENING, self.MOVE])
        self.assertIn("no FOOT row", str(caught.exception))

    def test_unparseable_amount_raises(self) -> None:
        broken = ["ROW", 7520, "Makse", "", "02.01.2026", "", "n/a", "84.40"]
        with self.assertRaises(click.ClickException):
            simplbooks._parse_account_ledger(
                [self.HEAD, self.OPENING, broken, self.PREFOOT, self.FOOT])


if __name__ == "__main__":
    unittest.main()
