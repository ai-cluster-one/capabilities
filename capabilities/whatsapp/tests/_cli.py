#!/usr/bin/env python3
"""Load the capability's executable as a module, for tests.

The capability is one script, so a test reaches its internals the way the
manager installs it: as that file. The engine is imported lazily by the script,
so everything below the network — the store, the parser, the envelope — is
reachable with no engine present and no dependency to install.

The capture lives in Postgres. The store-backed cases read WHATSAPP_TEST_DSN, a
throwaway database's URL, given to the capability as AGENTKIT_DB_URL, and skip
when it is unset; each case writes under an account of its own, so cases never
see each other's rows.

A source checkout keeps that file under `bin/` and an installed bundle keeps
it at the bundle root, so the path is resolved rather than named: naming one
layout passes where the test was written and fails where the capability is
installed.
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
    (path for path in (CAPABILITY_DIR / "bin" / "whatsapp",
                       CAPABILITY_DIR / "whatsapp")
     if path.is_file()),
    CAPABILITY_DIR / "bin" / "whatsapp")


class _Unused:
    """Stands in for a dependency the loaded module names but these tests never
    exercise. Reaching for anything on it yields another one, so import-time use
    resolves and call-time use would be visible immediately."""

    def __getattr__(self, name):
        return _Unused()

    def __call__(self, *args, **kwargs):
        return _Unused()


def load(*, require: tuple[str, ...] = ()) -> object:
    """The executable as a module. Names in `require` must import for real."""
    for name in ("httpx", "puremagic"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            if name in require:
                raise
            sys.modules[name] = _Unused()
    spec = importlib.util.spec_from_loader("whatsapp_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace(
        'if __name__ == "__main__":\n    _run()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


def engine_available(module) -> bool:
    """Whether the compiled engine loads on this host. It ships per platform, so
    the chunk-parsing tests skip rather than fail where it was not built."""
    try:
        module._engine()
        return True
    except BaseException:
        return False


STORE_DSN = os.environ.get("WHATSAPP_TEST_DSN")

# Every key the database cascade reads, so a case can hide each one it inherits.
from capabilities_contract.db import KEYS as DB_KEYS  # noqa: E402


def needs_store(case):
    """Skip a store-backed case where no throwaway database is named."""
    return unittest.skipUnless(STORE_DSN, "WHATSAPP_TEST_DSN is unset")(case)


def no_db_env() -> dict:
    """Every database key emptied, which the cascade reads as unset, and a
    config home of the case's own, so no database of the machine running the
    suite answers."""
    return {**{key: "" for key in DB_KEYS}, "XDG_CONFIG_HOME": tempfile.mkdtemp()}


def without_db_env() -> dict:
    """The process environment without any database key, and a config home of
    the case's own: a whole environment for `mock.patch.dict(..., clear=True)`
    or a child process."""
    env = {k: v for k, v in os.environ.items() if k not in DB_KEYS}
    env["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
    return env


def store_env() -> dict:
    """The environment a store-backed case runs under: the throwaway database
    as the environment's database, and nothing reachable behind it."""
    return {**no_db_env(), "AGENTKIT_DB_URL": STORE_DSN or ""}


def store_cfg(**extra) -> dict:
    """A connection whose home and account are this case's alone."""
    return {"id": "test", "home": tempfile.mkdtemp(),
            "account_key": f"test-{uuid.uuid4().hex[:12]}", **extra}
