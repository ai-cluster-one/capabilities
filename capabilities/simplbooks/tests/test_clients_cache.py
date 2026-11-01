from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
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
    name = "simplbooks_clients_cache_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


def _client(cid: int, name: str) -> dict:
    return {"id": cid, "name": name, "reg_no": "", "vat_no": "", "address_country": ""}


class ClientsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "clients.json"
        self.cache.write_text(json.dumps([_client(1, "Alpha"), _client(2, "Beta"),
                                          _client(3, "Gamma")]))
        self.rows: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": self.rows,
                                             "recordsTotal": len(self.rows)})

        for patcher in (
            mock.patch.object(simplbooks, "CLIENTS_CACHE", self.cache),
            mock.patch.object(simplbooks, "CONFIG_DIR", Path(tmp.name)),
            mock.patch.object(simplbooks, "session_or_die", return_value=({}, "acct")),
            mock.patch.object(simplbooks, "fetch_fresh_csrf", return_value="t"),
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

    def test_search_keeps_clients_that_are_not_among_the_hits(self) -> None:
        self.rows = [_client(2, "Beta renamed")]
        self.run_cli("clients", "list", "--search", "Beta")
        cache = simplbooks.load_clients_cache()
        self.assertEqual(sorted(cache), ["1", "2", "3"])
        self.assertEqual(cache["2"]["name"], "Beta renamed")

    def test_unfiltered_list_replaces_the_cache(self) -> None:
        self.rows = [_client(4, "Delta")]
        self.run_cli("clients", "list")
        self.assertEqual(sorted(simplbooks.load_clients_cache()), ["4"])


if __name__ == "__main__":
    unittest.main()
