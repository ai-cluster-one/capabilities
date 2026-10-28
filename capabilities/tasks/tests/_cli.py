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
    # The store's own read of workers, kept for when a case serves its worker
    # documents through a store it binds.
    module._REAL_STORED_WORKERS = module.__dict__.get("_stored_workers")
    module._REAL_STORED_PROFILES = module.__dict__.get("_stored_profiles")
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


# A transaction-only store, as a store whose one address is a known transaction
# pooler reads to the connection library: no session address, so a session
# connection to it is refused before anything is sent, while a transaction
# connection reaches the throwaway database behind it.
POOLER = ("aws-0-<region>.pooler.supabase.com", 6543)


def transaction_only(setting):
    """`setting` as a transaction-only store."""
    import dataclasses

    from capabilities_contract import db

    class TransactionOnly(db.Setting):
        def _transaction_pooler_endpoint(self):
            return POOLER

    return TransactionOnly(**{one.name: getattr(setting, one.name)
                              for one in dataclasses.fields(setting)})


def bind_store(mod, monkeypatch, schema: str, **where):
    """Make `schema` of the throwaway database the store every verb called in
    this process reaches, as `main` makes the machine's store the one it does."""
    setting = store_setting(schema, **where)
    monkeypatch.setattr(mod, "_store_setting", lambda raising=False, root=None: setting)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    return setting


def make_tables(mod, schema: str) -> None:
    """Create the capability's tables in `schema`, through its own steps, after
    the harness library's profile table they reference, through the library's."""
    from capabilities_contract import db

    steps, major, minor = mod._tables()
    owner, profile_steps, profile_major, profile_minor = mod._profile_tables()
    conn = db.connect(application_name="tasks-test", setting=store_setting(schema))
    try:
        db.migrate(conn, owner, profile_steps, major=profile_major, minor=profile_minor)
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


# A project's workers live in the store. A test keeps the documents it saves
# there in a folder of its own beside the project, outside the envelope -
# worker files left in the envelope are refused as not imported - and either
# serves them to the module in this process as the store's active versions,
# or saves them into a store a child process reads.

def worker_docs(root: Path | str) -> Path:
    """Where a test keeps the worker documents of the project at `root`."""
    return Path(root) / "worker-docs"


# A project's profiles live in the store too, kept by the same tests the same
# way: as files in a folder of their own beside the project, served or saved at
# the project's scope with its workers.

def profile_docs(root: Path | str) -> Path:
    """Where a test keeps the profile files of the project at `root`."""
    return Path(root) / "profile-docs"


def _profiles(mod, root: Path | str) -> list[tuple[str, dict, str]]:
    import tomllib

    # Read as written, not judged: what the store holds is judged again where it
    # is read, and a case may serve a spec the library refuses to show that.
    folder = profile_docs(root)
    found = []
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        text = path.read_text()
        found.append((path.stem, tomllib.loads(text), text))
    return found


def _served_profiles(mod, root) -> list:
    """The profile files at `root`, as the active versions a save of each would
    leave: at the project's scope, or the global one for a module standing in
    no project with an id, which reads only those."""
    import datetime
    import uuid

    from callva import harness_runner

    project = mod.PROJECT or None
    return [harness_runner.StoredProfile(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"profile\n{name}\n{text}")),
                scope="project" if project else "global", scope_id=project, name=name,
                spec=spec,
                active=True,
                created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                created_by="test")
            for name, spec, text in _profiles(mod, root)]


def save_profiles(mod, root: Path | str, schema: str, project: str) -> dict:
    """Make the store's active profiles of `project` in `schema` the files in
    `profile_docs(root)`, as `save_workers` does for workers: written straight
    into the library's table, so a case can hold a spec the library would refuse
    and see how it is read. Answers each name's active version id."""
    from capabilities_contract import db

    make_tables(mod, schema)
    docs = {name: spec for name, spec, _text in _profiles(mod, root)}
    conn = db.connect(application_name="tasks-test", setting=store_setting(schema))
    try:
        with conn.cursor() as cur:
            cur.execute(f"""select id, name, spec from "{schema}".harness_profiles
                             where scope = 'project' and scope_id = %s and active""",
                        (project,))
            active = {row[1]: row for row in cur.fetchall()}
            for name, row in active.items():
                if name not in docs or row[2] != docs[name]:
                    cur.execute(f'update "{schema}".harness_profiles set active = false '
                                "where id = %s", (row[0],))
            versions = {}
            for name, spec in docs.items():
                row = active.get(name)
                if row is not None and row[2] == spec:
                    versions[name] = str(row[0])
                    continue
                cur.execute(f"""insert into "{schema}".harness_profiles
                                  (scope, scope_id, name, spec, active, created_by)
                                values ('project', %s, %s, %s::jsonb, true, 'test')
                                returning id""", (project, name, json.dumps(spec)))
                versions[name] = str(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    return versions


def _documents(mod, root: Path | str) -> list[tuple[str, dict, str, str]]:
    folder = worker_docs(root)
    found = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        text = path.read_text()
        spec, body = mod._parse_worker(text, str(path))
        found.append((path.stem, spec, body, text))
    return found


def serve_workers(mod, monkeypatch, root=None) -> None:
    """Answer the module's store reads of workers from the documents in
    `worker_docs`, read afresh at every ask, each as the active version a save
    of it would leave. `root` is the project, else the one the module stands in
    when it asks."""
    import datetime
    import uuid

    def rows(fresh: bool = False, **how) -> list[dict]:
        where = root if root is not None else mod._project_root()
        schema = getattr(mod, "_TEST_WORKERS_SCHEMA", None)
        if schema is not None:
            return _saved_rows(mod, where, schema, fresh, **how)
        return [{"id": uuid.uuid5(uuid.NAMESPACE_URL, f"{name}\n{text}"), "name": name,
                 "spec": spec, "body": body,
                 "created_at": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                 "created_by": "test"}
                for name, spec, body, text in _documents(mod, where)]

    monkeypatch.setattr(mod, "_stored_workers", rows)

    def profiles(fresh: bool = False, **how) -> list:
        where = root if root is not None else mod._project_root()
        schema = getattr(mod, "_TEST_WORKERS_SCHEMA", None)
        if schema is not None:
            _saved_rows(mod, where, schema, fresh, **how)
            return mod._REAL_STORED_PROFILES(fresh=True, **how)
        return _served_profiles(mod, where)

    monkeypatch.setattr(mod, "_stored_profiles", profiles)


def save_workers(mod, root: Path | str, schema: str, project: str) -> dict:
    """Make the store's active workers of `project` in `schema` the documents in
    `worker_docs(root)`: a document whose active version differs gets a new one,
    and an active worker with no document is removed. The profiles in
    `profile_docs(root)` are saved first, the same way. Answers each name's
    active worker version id."""
    from capabilities_contract import db

    make_tables(mod, schema)
    save_profiles(mod, root, schema, project)
    docs = {name: (spec, body) for name, spec, body, _text in _documents(mod, root)}
    conn = db.connect(application_name="tasks-test", setting=store_setting(schema))
    try:
        with conn.cursor() as cur:
            cur.execute(f"""select id, name, spec, body from "{schema}".tasks_workers
                             where project_id = %s and active""", (project,))
            active = {row[1]: row for row in cur.fetchall()}
            for name, row in active.items():
                if name not in docs or (row[2], row[3]) != docs[name]:
                    cur.execute(f'update "{schema}".tasks_workers set active = false '
                                "where id = %s", (row[0],))
            versions = {}
            for name, (spec, body) in docs.items():
                row = active.get(name)
                if row is not None and (row[2], row[3]) == (spec, body):
                    versions[name] = str(row[0])
                    continue
                cur.execute(f"""insert into "{schema}".tasks_workers
                                  (project_id, name, spec, body, active, created_by)
                                values (%s, %s, %s::jsonb, %s, true, 'test')
                                returning id""", (project, name, json.dumps(spec), body))
                versions[name] = str(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    return versions


_LAST_SAVED: dict = {}


def keep_served_workers_in_store(mod, monkeypatch, schema: str) -> None:
    """Make what `serve_workers` serves the store's own: before each read the
    documents are saved into `schema` for the project the module stands in, and
    the read is the module's own, so a raise names a version the store holds.
    For in-process cases with a bound store; order against `serve_workers`
    does not matter."""
    monkeypatch.setattr(mod, "_TEST_WORKERS_SCHEMA", schema, raising=False)


def _texts(mod, root) -> tuple:
    """What a project's workers and profiles are written as, to tell whether
    they changed since this process last saved them."""
    return (tuple((name, text) for name, _spec, _body, text in _documents(mod, root)),
            tuple((name, text) for name, _spec, text in _profiles(mod, root)))


def _saved_rows(mod, root, schema: str, fresh: bool, **how) -> list[dict]:
    project = mod.PROJECT
    if not project:
        return []
    texts = _texts(mod, root)
    key = (schema, project)
    saved = _LAST_SAVED.get(key) != texts
    if saved:
        save_workers(mod, root, schema, project)
        _LAST_SAVED[key] = texts
    return mod._REAL_STORED_WORKERS(fresh=fresh or saved, **how)


def save_declared_workers(mod, project: Path | str, schema: str) -> dict:
    """`save_workers` for the project at `project`, as the id it is bound to -
    its .env.local's CAPABILITIES_PROJECT_ID, or the id a project.json not yet
    moved still carries; nothing when it declares none, or when the documents
    are the ones this process last saved there."""
    found = None
    try:
        for line in (Path(project) / ".env.local").read_text().splitlines():
            if line.startswith("CAPABILITIES_PROJECT_ID="):
                found = line.split("=", 1)[1].strip()
    except OSError:
        pass
    if not found:
        try:
            found = json.loads((Path(project) / "capabilities" / "project.json")
                               .read_text()).get("id")
        except (OSError, ValueError):
            found = None
    if not found:
        return {}
    texts = _texts(mod, project)
    key = (schema, found)
    if _LAST_SAVED.get(key) == texts:
        return {}
    versions = save_workers(mod, project, schema, found)
    _LAST_SAVED[key] = texts
    return versions
