"""`accounts ledgers` — the derived cashbook ⇄ chart join.

The point of these tests is that the pairing is DERIVED from each cashbook's own
edit form and never inferred from the two listings' names, which collide.
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


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "simplbooks"


def _load_module():
    name = "simplbooks_ledger_surface_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


# Deliberately name-hostile, mirroring the live account: the cashbook "Swedbank"
# matches no chart row exactly and two of them by prefix, "Tasaarveldus" matches
# none, and "Kassa" matches exactly one — so a name join would be right once and
# wrong or ambiguous everywhere else.
CHART_ROWS = [
    {"id": 1, "code": "1010", "name": "Kassa"},
    {"id": 3, "code": "1030", "name": "Raha teel"},
    {"id": 206, "code": "9999", "name": "Tasaarveldused"},
    {"id": 211, "code": "1025", "name": "Swedbank pangakonto 3368"},
    {"id": 236, "code": "1028", "name": "Swedbank krediidikonto 8705"},
    {"id": 248, "code": "1024", "name": "Stripe Juko"},
]
CASHBOOK_ROWS = [
    {"id": 1, "code": "", "name": "Kassa"},
    {"id": 3, "code": "", "name": "Swedbank"},
    {"id": 4, "code": "", "name": "Tasaarveldus"},
]
LINKS = {"1": "1", "3": "211", "4": "206"}


EDIT_FORM = """
<form>
  <select name="data[IncomeAccount][financial_account_id]">
    <option value="1">1010 Kassa</option>
    <option value="3">1030 Raha teel</option>
    <option value="211" selected="selected">1025 Swedbank pangakonto 3368</option>
  </select>
</form>
"""


class _Resp:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.is_redirect = False


class _Http:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.urls: list[str] = []

    def get(self, url: str) -> _Resp:
        self.urls.append(url)
        return self._resp


class LinkScrapeTests(unittest.TestCase):
    """The link is the SELECTED option's VALUE — the label is never parsed."""

    def test_selected_option_value_is_returned(self) -> None:
        http = _Http(_Resp(EDIT_FORM))
        self.assertEqual(
            simplbooks._cashbook_linked_coa_id(http, "acct", "3"), "211")
        self.assertTrue(http.urls[0].endswith("/income_accounts/edit/3"))

    def test_an_unlinked_cashbook_yields_none(self) -> None:
        html = EDIT_FORM.replace(' selected="selected"', "")
        self.assertIsNone(
            simplbooks._cashbook_linked_coa_id(_Http(_Resp(html)), "acct", "3"))

    def test_a_missing_select_refuses_rather_than_guesses(self) -> None:
        with self.assertRaises(click.ClickException) as caught:
            simplbooks._cashbook_linked_coa_id(_Http(_Resp("<form></form>")), "acct", "3")
        self.assertIn("Refusing to guess", str(caught.exception))

    def test_a_non_200_refuses(self) -> None:
        with self.assertRaises(click.ClickException):
            simplbooks._cashbook_linked_coa_id(_Http(_Resp("", 500)), "acct", "3")


class DerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for patcher in (
            mock.patch.object(simplbooks, "_chart_index",
                              return_value=simplbooks._index_accounts(CHART_ROWS)),
            mock.patch.object(simplbooks, "_chart_index_all",
                              return_value=simplbooks._index_accounts(CHART_ROWS)),
            mock.patch.object(simplbooks, "_cashbook_index",
                              return_value=simplbooks._index_accounts(CASHBOOK_ROWS)),
            mock.patch.object(simplbooks, "session_or_die",
                              return_value=({}, "acct")),
            mock.patch.object(simplbooks, "build_http",
                              return_value=mock.MagicMock(
                                  __enter__=lambda s: None, __exit__=lambda *a: False)),
            mock.patch.object(simplbooks, "_cashbook_linked_coa_id",
                              side_effect=lambda http, acct, cid: LINKS.get(cid)),
        ):
            self.stack.enter_context(patcher)

    def test_every_cashbook_gets_its_chart_code(self) -> None:
        data = simplbooks._derive_ledger_map()
        self.assertEqual(
            [(r["cashbook_id"], r["coa_id"], r["coa_code"]) for r in data["ledgers"]],
            [(1, 1, "1010"), (3, 211, "1025"), (4, 206, "9999")],
        )

    def test_the_join_is_not_by_name(self) -> None:
        # Cashbook 4 is "Tasaarveldus"; no chart row carries that name, and the
        # form links it to 9999 "Tasaarveldused". A name join would find nothing.
        row = next(r for r in simplbooks._derive_ledger_map()["ledgers"]
                   if r["cashbook_id"] == 4)
        self.assertEqual(row["coa_code"], "9999")
        self.assertNotEqual(row["cashbook_name"], row["coa_name"])

    def test_an_unlinked_cashbook_carries_nulls(self) -> None:
        with mock.patch.object(simplbooks, "_cashbook_linked_coa_id",
                               side_effect=lambda http, acct, cid: None):
            row = simplbooks._derive_ledger_map()["ledgers"][0]
        self.assertIsNone(row["coa_id"])
        self.assertIsNone(row["coa_code"])
        self.assertFalse(row["coa_active"])

    def test_the_overlap_is_stated_per_cashbook_id(self) -> None:
        collisions = {c["value"]: c for c in
                      simplbooks._derive_ledger_map()["coa_id_collisions"]}
        # 1 is the same ledger in both spaces — the coincidence that made the
        # wrong mental model test clean.
        self.assertTrue(collisions[1]["same_ledger"])
        self.assertEqual(collisions[1]["coa_code"], "1010")
        # 3 is not: as a cashbook it is Swedbank, as a chart id it is Raha teel.
        self.assertFalse(collisions[3]["same_ledger"])
        self.assertEqual(collisions[3]["coa_code"], "1030")

    def test_a_cashbook_id_outside_the_chart_reports_no_collision(self) -> None:
        collisions = {c["value"]: c for c in
                      simplbooks._derive_ledger_map()["coa_id_collisions"]}
        self.assertIsNone(collisions[4]["coa_code"])  # no chart row with id 4

    def test_the_source_is_named_in_the_payload(self) -> None:
        self.assertIn("data[IncomeAccount][financial_account_id]",
                      simplbooks._derive_ledger_map()["source"])


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from contextlib import ExitStack
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            simplbooks, "_state_dir", return_value=Path(self.tmp.name)))
        self.stack.enter_context(mock.patch.object(
            simplbooks, "session_or_die", return_value=({}, "acct")))
        self.derived = {"account": "acct", "derived_at": "now",
                        "source": "s", "ledgers": [{"cashbook_id": 1}],
                        "coa_id_collisions": []}

    def test_first_call_derives_and_writes_the_cache(self) -> None:
        with mock.patch.object(simplbooks, "_derive_ledger_map",
                               return_value=self.derived) as derive:
            data, from_cache = simplbooks._load_ledger_map()
        self.assertFalse(from_cache)
        self.assertEqual(derive.call_count, 1)
        self.assertEqual(
            json.loads((Path(self.tmp.name) / simplbooks.LEDGER_MAP_FILE).read_text()),
            self.derived)

    def test_second_call_reads_the_cache(self) -> None:
        with mock.patch.object(simplbooks, "_derive_ledger_map",
                               return_value=self.derived) as derive:
            simplbooks._load_ledger_map()
            data, from_cache = simplbooks._load_ledger_map()
        self.assertTrue(from_cache)
        self.assertEqual(derive.call_count, 1)
        self.assertEqual(data["ledgers"], self.derived["ledgers"])

    def test_refresh_re_derives(self) -> None:
        with mock.patch.object(simplbooks, "_derive_ledger_map",
                               return_value=self.derived) as derive:
            simplbooks._load_ledger_map()
            _, from_cache = simplbooks._load_ledger_map(refresh=True)
        self.assertFalse(from_cache)
        self.assertEqual(derive.call_count, 2)

    def test_another_accounts_cache_is_not_read(self) -> None:
        (Path(self.tmp.name) / simplbooks.LEDGER_MAP_FILE).write_text(
            json.dumps({**self.derived, "account": "someone-else"}))
        with mock.patch.object(simplbooks, "_derive_ledger_map",
                               return_value=self.derived) as derive:
            _, from_cache = simplbooks._load_ledger_map()
        self.assertFalse(from_cache)
        self.assertEqual(derive.call_count, 1)

    def test_a_corrupt_cache_re_derives(self) -> None:
        (Path(self.tmp.name) / simplbooks.LEDGER_MAP_FILE).write_text("{not json")
        with mock.patch.object(simplbooks, "_derive_ledger_map",
                               return_value=self.derived) as derive:
            _, from_cache = simplbooks._load_ledger_map()
        self.assertFalse(from_cache)
        self.assertEqual(derive.call_count, 1)


if __name__ == "__main__":
    unittest.main()
