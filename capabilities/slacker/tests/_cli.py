#!/usr/bin/env python3
"""Load the capability's executable as a module, for tests.

The capability is one script, so a test reaches its internals the way the
manager installs it: as that file. The Slack client and the websocket engine
are imported lazily by the script, so everything below the network — the store,
the parser, the event handler, the sync cursor — is reachable with no Slack
dependency present and no network.

The capture lives in Postgres. The store-backed cases read SLACKER_TEST_DSN, a
throwaway database's URL, given to the capability as AGENTKIT_DB_URL, and skip
when it is unset; each case writes under an account of its own, so cases never
see each other's rows.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next(
    (path for path in (CAPABILITY_DIR / "bin" / "slacker",
                       CAPABILITY_DIR / "slacker")
     if path.is_file()),
    CAPABILITY_DIR / "bin" / "slacker")


class _Unused:
    """Stands in for a dependency the loaded module names but these tests never
    exercise. Reaching for anything on it yields another one, so import-time use
    resolves and call-time use would be visible immediately."""

    def __getattr__(self, name):
        return _Unused()

    def __call__(self, *args, **kwargs):
        return _Unused()


def load() -> object:
    """The executable as a module, with the lazily-imported Slack and websocket
    dependencies stubbed so no network library is needed."""
    for name in ("slack_sdk", "slack_sdk.errors", "websocket"):
        if name not in sys.modules:
            sys.modules[name] = _Unused()
    spec = importlib.util.spec_from_loader("slacker_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text()
    # Strip the __main__ guard (which imports traceback and runs the CLI).
    marker = source.index('if __name__ == "__main__":')
    source = source[:marker]
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


STORE_DSN = os.environ.get("SLACKER_TEST_DSN")

# Every key the database cascade reads, so a case can hide each one it inherits.
from capabilities_contract.db import KEYS as DB_KEYS  # noqa: E402


def needs_store(case):
    """Skip a store-backed case where no throwaway database is named."""
    return unittest.skipUnless(STORE_DSN, "SLACKER_TEST_DSN is unset")(case)


def no_db_env() -> dict:
    """Every database key emptied, which the cascade reads as unset, and a
    config home of the case's own, so no database of the machine running the
    suite answers."""
    return {**{key: "" for key in DB_KEYS}, "XDG_CONFIG_HOME": tempfile.mkdtemp()}


def store_env() -> dict:
    """The environment a store-backed case runs under: the throwaway database as
    the environment's database, and nothing reachable behind it."""
    return {**no_db_env(), "AGENTKIT_DB_URL": STORE_DSN or ""}


def store_cfg(**extra) -> dict:
    """A connection whose account is this case's alone."""
    return {"id": "test", "account_binding": f"test-{uuid.uuid4().hex[:12]}", **extra}
