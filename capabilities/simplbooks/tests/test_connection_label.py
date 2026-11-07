"""The store keys a connection by its id; what the connection check says of a
connection that is not fully configured names it by its label."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "simplbooks", CAPABILITY / "simplbooks")
    if path.is_file()), CAPABILITY / "bin" / "simplbooks")


def _load_module():
    name = "simplbooks_connection_label_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()


class _Died(Exception):
    pass


def _raise(exit_code, code, message, hint=None):
    raise _Died(message)


class ConnectionLabelTests(unittest.TestCase):
    def _resolve(self, entry: dict) -> str:
        reg = {"connections": {"0a1b2c3d4e5f": entry}}
        with mock.patch.object(simplbooks, "_connections_registry", lambda: (reg, None)), \
                mock.patch.object(simplbooks, "_select_connection",
                                  lambda r, w: ("0a1b2c3d4e5f", entry)), \
                mock.patch.object(simplbooks, "_die", _raise), \
                self.assertRaises(_Died) as caught:
            simplbooks._resolve_connection(None)
        return str(caught.exception)

    def test_a_missing_email_names_the_connection_by_its_label(self) -> None:
        self.assertEqual(self._resolve({"label": "books"}),
                         "connection 'books' is missing 'email'")

    def test_a_missing_secret_env_names_the_connection_by_its_label(self) -> None:
        self.assertEqual(self._resolve({"label": "books", "email": "user@example.com"}),
                         "connection 'books' is missing 'secret_env'")


if __name__ == "__main__":
    unittest.main()
