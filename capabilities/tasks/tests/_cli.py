#!/usr/bin/env python3
"""Load the capability's executable as a module, for tests.

The capability is one script, so a test reaches its internals the way the
manager installs it: as that file. The store driver is imported where a verb
connects, so the parser, the identity resolver and the schema plumbing load
with no store and nothing installed.

A source checkout keeps that file under `bin/` and an installed bundle keeps
it at the bundle root, so the path is resolved rather than named: naming one
layout passes where the test was written and fails where the capability is
installed.

The store-backed checks read TASKS_TEST_DSN, a libpq URL for a throwaway
database, and each case binds a schema of its own as the machine's store: a
verb called in this process reads the store setting `bind_store` hands it, and
a CLI run as a child reads the setting file `write_store_setting` puts in its
config home.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next(
    (path for path in (CAPABILITY_DIR / "bin" / "tasks",
                       CAPABILITY_DIR / "tasks")
     if path.is_file()),
    CAPABILITY_DIR / "bin" / "tasks")


def load() -> object:
    """The executable as a module."""
    spec = importlib.util.spec_from_loader("tasks_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace(
        'if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


DSN = os.environ.get("TASKS_TEST_DSN")

# What would name another store to a child: the environment level the shared
# library reads before the setting file.
STORE_OVERRIDES = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
                   "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
                   "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")


def _where(host: str | None = None, port: int | None = None) -> dict:
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN or "postgresql://nobody@127.0.0.1:5432/none")
    found = {"host": host or info.get("host") or "127.0.0.1",
             "port": int(port or info.get("port") or 5432),
             "database": info.get("dbname"), "user": info.get("user"),
             "sslmode": info.get("sslmode") or "disable"}
    if info.get("password"):
        found["password"] = info["password"]
    return found


def store_setting(schema: str, *, host: str | None = None, port: int | None = None):
    """The throwaway database as the store in force, bound to `schema`, reached
    at `host` and `port` when a case puts a relay in front of it."""
    from capabilities_contract import db

    return db.Setting(schema=schema, level="machine", sources=("TASKS_TEST_DSN",),
                      **_where(host, port))


def bind_store(mod, monkeypatch, schema: str, **where):
    """Make `schema` of the throwaway database the store every verb called in
    this process reaches, as `main` makes the machine's store the one it does."""
    setting = store_setting(schema, **where)
    monkeypatch.setattr(mod, "_store_setting", lambda raising=False, root=None: setting)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    return setting


def make_tables(mod, schema: str) -> None:
    """Create the capability's tables in `schema`, through its own steps."""
    from capabilities_contract import db

    steps, major, minor = mod._tables()
    conn = db.connect(application_name="tasks-test", setting=store_setting(schema))
    try:
        db.migrate(conn, mod.NAME, steps, major=major, minor=minor)
    finally:
        conn.close()


def write_store_setting(config_home: Path | str, schema: str, *, database: str | None = None,
                        **where) -> Path:
    """The machine's store setting in `config_home`, naming `schema` of the
    throwaway database - or of `database` on the same server - as `capabilities
    store set` writes it."""
    path = Path(config_home) / "agentkit" / "store.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    found = _where(**where)
    if database is not None:
        found["database"] = database
    path.write_text(json.dumps({"schema": "agentkit.store.v1", **found,
                                "db_schema": schema}))
    path.chmod(0o600)
    return path


def child_env(env: dict) -> dict:
    """`env` without an override that would name another store."""
    return {key: value for key, value in env.items() if key not in STORE_OVERRIDES}
