"""Canonical store tier — the one true copy of the shared persistence layer.

This is a THIRD fenced tier alongside `capability core` and `connections` in
`contract/preamble.py`, and it obeys the same law: the helpers between the fence
markers are COPIED into each `bin/<name>`, never imported, so a manager update
can never break a deployed capability (SHEBANG.md "spec, never a shared runtime
library"). Only a capability that keeps records outside its own process carries
this fence; omitting it is the normal state for a capability that keeps none.

WHAT THIS TIER OWNS
===================
The records a *running* capability reads and writes: connections, identifiers,
settings, the policy gate, and operational state. It does not own authoring
material — a guide, a reference, a doctrine file — which versions in lockstep
with the code that it describes and stays in the repository.

The test is whether the material must change on a deployed host *without* a
redeploy. A channel prompt served per request: yes. A capability's own guide:
no, it is meaningless apart from the version of the code that ships it.

THREE RESOLUTION SEMANTICS, ALREADY IN THE DOCTRINE
===================================================
Scope is a column here, not a directory. The precedence is always
instance > project > global, but *how* the scopes combine is declared per
collection, because the standing rules already contain two different answers
and one collection needs a third:

  - FIRST  — the highest scope holding ANY entry wins, whole, never merged.
    This is rule 18 for connections: a half-project, half-global identity is
    not an identity. Selection stays deterministic and refuses ambiguity.

  - MERGE  — per key; a key absent at the higher scope inherits the lower.
    This is rule 17 for the gate ("an absent project entry inherits the global
    entry"), and it is what makes a setting worth scoping at all.

  - EXACT  — no cascade; a record belongs to exactly one scope and is read
    there. This is rule 16: state follows the scope of the credentials that
    minted it, so a project's state is not a fallback for an instance's.

ONE WRITER PER SHARED FILE BECOMES ONE WRITER PER COLLECTION
============================================================
Rule 15 survives the move intact, addressed by `(capability, collection)`
instead of by path: the manager alone writes `policy`, a capability alone
writes its own `identifier` and `state` rows, and `connection` and `setting`
rows are human-written through a CLI verb. The `revisions` table records who
wrote what, which is what the repository's history provided before.

MIGRATIONS ARE ADDITIVE
=======================
Two hosts running different versions of the same capability will share one
database. A column added by a newer version must therefore be readable-by-
absence to the older one: expand in one release, contract only in a later
release that no live host predates. `migrate()` refuses to run a step that
drops or narrows, so the rule is mechanical rather than remembered.

EXCEPTIONS ARE RAISED, NEVER PRINTED
====================================
This tier is a library, not a command. It raises `StoreError`; the carrying
capability maps that onto its own `_die` envelope and exit codes, so the error
surface stays the capability's own.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

# >>> contract: store (generated — edit contract/store.py, run `capabilities sync-contract`) >>>

SCHEMA_VERSION = 1

SCOPE_KINDS = ("global", "project", "instance")
SCOPE_RANK = {"global": 1, "project": 2, "instance": 3}

FIRST = "first"
MERGE = "merge"
EXACT = "exact"

# The collection registry: how each resolves, and who is allowed to write it.
# Adding a collection here is the only place a new record class is declared.
COLLECTIONS: dict[str, dict[str, str]] = {
    "connection": {"resolve": FIRST, "writer": "human"},
    "identifier": {"resolve": MERGE, "writer": "capability"},
    "setting": {"resolve": MERGE, "writer": "human"},
    "policy": {"resolve": MERGE, "writer": "manager"},
}

KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.IGNORECASE)
CAPABILITY_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")


class StoreError(Exception):
    """Every failure this tier reports. Carries a slug the caller maps to an
    exit code, so the CLI's error envelope stays the capability's own."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug = slug
        self.message = message
        self.hint = hint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check(name: str, value: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise StoreError("bad_name", f"{name} {value!r} must match {pattern.pattern}")
    return value


def _check_scope(kind: str, name: str) -> tuple[str, str]:
    if kind not in SCOPE_KINDS:
        raise StoreError("bad_scope", f"scope kind {kind!r} must be one of {', '.join(SCOPE_KINDS)}")
    if kind == "global":
        if name:
            raise StoreError("bad_scope", "the global scope takes no name")
        return kind, ""
    if not name:
        raise StoreError("bad_scope", f"the {kind} scope requires a name")
    return kind, name


class Scopes:
    """The scope chain a capability resolves against, highest first.

    Built once per process from the environment and the project on disk, then
    passed to every read. Holding it explicitly keeps resolution testable and
    keeps the ambient environment out of the query path."""

    def __init__(self, project: str | None = None, instance: str | None = None):
        self.project = project or None
        self.instance = instance or None

    @classmethod
    def from_env(cls, project: str | None = None) -> Scopes:
        return cls(
            project=project or os.environ.get("CAPABILITIES_PROJECT") or None,
            instance=os.environ.get("CAPABILITIES_INSTANCE") or None,
        )

    def chain(self) -> list[tuple[str, str]]:
        """Highest precedence first."""
        out: list[tuple[str, str]] = []
        if self.instance:
            out.append(("instance", self.instance))
        if self.project:
            out.append(("project", self.project))
        out.append(("global", ""))
        return out

    def write_target(self, scope: str | None) -> tuple[str, str]:
        """Resolve a write's scope name from its kind. A write never guesses a
        scope it was not given a name for."""
        kind = scope or "project"
        if kind == "global":
            return ("global", "")
        if kind == "project":
            if not self.project:
                raise StoreError("no_project_scope", "no project scope is in effect",
                                 "set CAPABILITIES_PROJECT or pass --scope global")
            return ("project", self.project)
        if kind == "instance":
            if not self.instance:
                raise StoreError("no_instance_scope", "no instance scope is in effect",
                                 "set CAPABILITIES_INSTANCE")
            return ("instance", self.instance)
        raise StoreError("bad_scope", f"scope kind {kind!r} must be one of {', '.join(SCOPE_KINDS)}")

    def __repr__(self) -> str:
        return f"Scopes(project={self.project!r}, instance={self.instance!r})"


# --- Schema ------------------------------------------------------------------

# Portable DDL. `{json}` and `{now}` are the only dialect substitutions; every
# other construct below is common to SQLite >= 3.35 and PostgreSQL >= 12.
_DDL = [
    """
    CREATE TABLE IF NOT EXISTS store_meta (
        namespace   TEXT PRIMARY KEY,
        version     INTEGER NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config (
        scope_kind  TEXT NOT NULL,
        scope_name  TEXT NOT NULL,
        capability  TEXT NOT NULL,
        collection  TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       {json} NOT NULL,
        note        TEXT,
        updated_at  TEXT NOT NULL,
        updated_by  TEXT,
        PRIMARY KEY (scope_kind, scope_name, capability, collection, key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS config_lookup_idx
        ON config (capability, collection, scope_kind, scope_name)
    """,
    """
    CREATE TABLE IF NOT EXISTS state (
        scope_kind  TEXT NOT NULL,
        scope_name  TEXT NOT NULL,
        capability  TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       {json} NOT NULL,
        updated_at  TEXT NOT NULL,
        expires_at  TEXT,
        PRIMARY KEY (scope_kind, scope_name, capability, key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS state_expiry_idx ON state (expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions (
        id          {serial},
        scope_kind  TEXT NOT NULL,
        scope_name  TEXT NOT NULL,
        capability  TEXT NOT NULL,
        collection  TEXT NOT NULL,
        key         TEXT NOT NULL,
        old_value   {json},
        new_value   {json},
        actor       TEXT,
        written_at  TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS revisions_target_idx
        ON revisions (capability, collection, key, written_at)
    """,
]


# --- The repository ----------------------------------------------------------

class Store:
    """The persistence interface every capability sees. Two adapters implement
    it; no capability ever writes SQL, which is the single discipline that keeps
    both backends honest."""

    dialect = "abstract"

    # -- lifecycle ------------------------------------------------------------

    def __init__(self, conn: Any):
        self._conn = conn

    @property
    def connection(self) -> Any:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- dialect hooks --------------------------------------------------------

    def _sql(self, sql: str) -> str:
        raise NotImplementedError

    def _encode(self, value: Any) -> Any:
        raise NotImplementedError

    def _decode(self, raw: Any) -> Any:
        raise NotImplementedError

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self._conn.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

    # -- schema ---------------------------------------------------------------

    def migrate(self, namespace: str = "core", version: int = SCHEMA_VERSION,
                steps: Sequence[str] | None = None) -> int:
        """Bring `namespace` up to `version`. The core namespace owns the tables
        above; a capability passes its own namespace and additive steps.

        Refuses a step that drops or narrows, because two hosts on different
        capability versions share this database and the older one must survive
        the newer one's migration."""
        _check("namespace", namespace, CAPABILITY_RE)
        with self.transaction():
            self._execute(_DDL[0].format(**self._ddl_subs()))
            cur = self._execute("SELECT version FROM store_meta WHERE namespace = ?", (namespace,))
            row = cur.fetchone()
            have = int(row[0]) if row else 0
            if have >= version:
                return have
            body = list(_DDL[1:]) if namespace == "core" and steps is None else list(steps or [])
            for step in body:
                self._guard_additive(step)
                self._execute(step.format(**self._ddl_subs()))
            if row:
                self._execute("UPDATE store_meta SET version = ?, updated_at = ? WHERE namespace = ?",
                              (version, _now(), namespace))
            else:
                self._execute("INSERT INTO store_meta (namespace, version, updated_at) VALUES (?, ?, ?)",
                              (namespace, version, _now()))
        return version

    @staticmethod
    def _guard_additive(step: str) -> None:
        head = " ".join(step.split()).upper()
        for banned in ("DROP TABLE", "DROP COLUMN", "ALTER COLUMN", "DROP INDEX", "RENAME "):
            if banned in head:
                raise StoreError(
                    "destructive_migration",
                    f"migration step contains {banned.strip()!r}",
                    "expand in one release and contract in a later one; a host "
                    "running the previous version still reads this database",
                )

    def _ddl_subs(self) -> dict[str, str]:
        raise NotImplementedError

    def schema_version(self, namespace: str = "core") -> int:
        cur = self._execute("SELECT version FROM store_meta WHERE namespace = ?", (namespace,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def health(self) -> dict[str, Any]:
        """What `doctor` reports: reachable, which dialect, which versions."""
        started = time.monotonic()
        self._execute("SELECT 1").fetchone()
        rtt = round((time.monotonic() - started) * 1000, 1)
        cur = self._execute("SELECT namespace, version FROM store_meta ORDER BY namespace")
        return {
            "dialect": self.dialect,
            "roundtrip_ms": rtt,
            "namespaces": {n: int(v) for n, v in cur.fetchall()},
        }

    # -- config: read ---------------------------------------------------------

    def config_get(self, capability: str, collection: str, key: str,
                   scopes: Scopes) -> Any:
        """One resolved value, or None. Follows the collection's semantics."""
        resolved = self.config_resolve(capability, collection, scopes)
        entry = resolved.get(key)
        return entry["value"] if entry else None

    def config_resolve(self, capability: str, collection: str,
                       scopes: Scopes) -> dict[str, dict[str, Any]]:
        """The whole collection as the capability should see it, each entry
        carrying the scope it came from so `connections` can report resolution
        without a second query."""
        _check("capability", capability, CAPABILITY_RE)
        semantics = self._semantics(collection)
        rows = self._config_rows(capability, collection, scopes)
        by_scope: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for row in rows:
            by_scope.setdefault((row["scope_kind"], row["scope_name"]), {})[row["key"]] = row

        if semantics == EXACT:
            raise StoreError("bad_collection",
                             f"collection {collection!r} is state, not config",
                             "read it with state_get")

        out: dict[str, dict[str, Any]] = {}
        for scope in scopes.chain():
            entries = by_scope.get(scope)
            if not entries:
                continue
            if semantics == FIRST:
                # Rule 18: the highest scope holding anything wins whole.
                return entries
            for key, row in entries.items():
                out.setdefault(key, row)  # higher scope came first, so it stands
        return out

    def config_origin(self, capability: str, collection: str, key: str,
                      scopes: Scopes) -> tuple[str, str] | None:
        """Which scope a resolved key actually came from — what makes a
        `connections`-style report possible without guessing."""
        resolved = self.config_resolve(capability, collection, scopes)
        entry = resolved.get(key)
        return (entry["scope_kind"], entry["scope_name"]) if entry else None

    def config_list(self, capability: str, collection: str,
                    scope: tuple[str, str] | None = None) -> list[dict[str, Any]]:
        """Every stored row, unresolved — the view for editing rather than use."""
        _check("capability", capability, CAPABILITY_RE)
        self._semantics(collection)
        sql = ("SELECT scope_kind, scope_name, capability, collection, key, value, note, "
               "updated_at, updated_by FROM config WHERE capability = ? AND collection = ?")
        params: list[Any] = [capability, collection]
        if scope:
            kind, name = _check_scope(*scope)
            sql += " AND scope_kind = ? AND scope_name = ?"
            params += [kind, name]
        cur = self._execute(sql + " ORDER BY scope_kind, scope_name, key", params)
        return [self._row(r) for r in cur.fetchall()]

    def _config_rows(self, capability: str, collection: str,
                     scopes: Scopes) -> list[dict[str, Any]]:
        chain = scopes.chain()
        clause = " OR ".join(["(scope_kind = ? AND scope_name = ?)"] * len(chain))
        params: list[Any] = [capability, collection]
        for kind, name in chain:
            params += [kind, name]
        cur = self._execute(
            "SELECT scope_kind, scope_name, capability, collection, key, value, note, "
            f"updated_at, updated_by FROM config WHERE capability = ? AND collection = ? AND ({clause})",
            params,
        )
        return [self._row(r) for r in cur.fetchall()]

    def _row(self, r: Sequence[Any]) -> dict[str, Any]:
        return {
            "scope_kind": r[0], "scope_name": r[1], "capability": r[2],
            "collection": r[3], "key": r[4], "value": self._decode(r[5]),
            "note": r[6], "updated_at": r[7], "updated_by": r[8],
        }

    @staticmethod
    def _semantics(collection: str) -> str:
        spec = COLLECTIONS.get(collection)
        if not spec:
            raise StoreError("bad_collection", f"unknown collection {collection!r}",
                             f"known: {', '.join(sorted(COLLECTIONS))}")
        return spec["resolve"]

    # -- config: write --------------------------------------------------------

    def config_set(self, capability: str, collection: str, key: str, value: Any,
                   scope: tuple[str, str], actor: str | None = None,
                   note: str | None = None) -> None:
        """Write one entry at one scope and record the revision. A write always
        names its scope; nothing here infers one from a cascade."""
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        self._semantics(collection)
        kind, name = _check_scope(*scope)
        with self.transaction():
            before = self._current(capability, collection, key, kind, name)
            self._execute(
                "INSERT INTO config (scope_kind, scope_name, capability, collection, key, "
                "value, note, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (scope_kind, scope_name, capability, collection, key) DO UPDATE "
                "SET value = EXCLUDED.value, note = EXCLUDED.note, "
                "updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by",
                (kind, name, capability, collection, key, self._encode(value), note,
                 _now(), actor),
            )
            self._revise(kind, name, capability, collection, key, before, value, actor)

    def config_delete(self, capability: str, collection: str, key: str,
                      scope: tuple[str, str], actor: str | None = None) -> bool:
        _check("capability", capability, CAPABILITY_RE)
        self._semantics(collection)
        kind, name = _check_scope(*scope)
        with self.transaction():
            before = self._current(capability, collection, key, kind, name)
            if before is None:
                return False
            self._execute(
                "DELETE FROM config WHERE scope_kind = ? AND scope_name = ? AND capability = ? "
                "AND collection = ? AND key = ?",
                (kind, name, capability, collection, key),
            )
            self._revise(kind, name, capability, collection, key, before, None, actor)
        return True

    def _current(self, capability: str, collection: str, key: str,
                 kind: str, name: str) -> Any:
        cur = self._execute(
            "SELECT value FROM config WHERE scope_kind = ? AND scope_name = ? "
            "AND capability = ? AND collection = ? AND key = ?",
            (kind, name, capability, collection, key),
        )
        row = cur.fetchone()
        return self._decode(row[0]) if row else None

    def _revise(self, kind: str, name: str, capability: str, collection: str,
                key: str, old: Any, new: Any, actor: str | None) -> None:
        self._execute(
            "INSERT INTO revisions (scope_kind, scope_name, capability, collection, key, "
            "old_value, new_value, actor, written_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, name, capability, collection, key,
             self._encode(old), self._encode(new), actor, _now()),
        )

    def revisions(self, capability: str, collection: str | None = None,
                  key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """What the repository's history used to answer: who changed this, when,
        and what it was before."""
        sql = ("SELECT scope_kind, scope_name, collection, key, old_value, new_value, "
               "actor, written_at FROM revisions WHERE capability = ?")
        params: list[Any] = [capability]
        if collection:
            sql += " AND collection = ?"
            params.append(collection)
        if key:
            sql += " AND key = ?"
            params.append(key)
        cur = self._execute(sql + " ORDER BY written_at DESC, id DESC LIMIT ?", params + [limit])
        return [{
            "scope_kind": r[0], "scope_name": r[1], "collection": r[2], "key": r[3],
            "old_value": self._decode(r[4]), "new_value": self._decode(r[5]),
            "actor": r[6], "written_at": r[7],
        } for r in cur.fetchall()]

    # -- state ----------------------------------------------------------------

    def state_get(self, capability: str, key: str, scope: tuple[str, str]) -> Any:
        """Rule 16: state is read at exactly the scope that minted it. A project's
        state is not a fallback for an instance's, so there is no cascade here."""
        _check("capability", capability, CAPABILITY_RE)
        kind, name = _check_scope(*scope)
        cur = self._execute(
            "SELECT value, expires_at FROM state WHERE scope_kind = ? AND scope_name = ? "
            "AND capability = ? AND key = ?",
            (kind, name, capability, key),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row[1] and row[1] <= _now():
            return None
        return self._decode(row[0])

    def state_set(self, capability: str, key: str, value: Any, scope: tuple[str, str],
                  ttl_seconds: int | None = None) -> None:
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        kind, name = _check_scope(*scope)
        expires = None
        if ttl_seconds is not None:
            expires = (datetime.now(timezone.utc)
                       + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self.transaction():
            self._execute(
                "INSERT INTO state (scope_kind, scope_name, capability, key, value, "
                "updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (scope_kind, scope_name, capability, key) DO UPDATE "
                "SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at, "
                "expires_at = EXCLUDED.expires_at",
                (kind, name, capability, key, self._encode(value), _now(), expires),
            )

    def state_delete(self, capability: str, key: str, scope: tuple[str, str]) -> bool:
        kind, name = _check_scope(*scope)
        with self.transaction():
            cur = self._execute(
                "DELETE FROM state WHERE scope_kind = ? AND scope_name = ? "
                "AND capability = ? AND key = ?",
                (kind, name, capability, key),
            )
        return bool(cur.rowcount)

    def state_sweep(self) -> int:
        """Drop what has expired. Cheap enough to call on daemon tick."""
        with self.transaction():
            cur = self._execute(
                "DELETE FROM state WHERE expires_at IS NOT NULL AND expires_at <= ?", (_now(),)
            )
        return cur.rowcount or 0


# --- Adapters ----------------------------------------------------------------

class SQLiteStore(Store):
    """The single-user local floor. Honest about being that: one writer, no
    `SKIP LOCKED`, JSON stored as text and unindexed. A project that never
    leaves one machine needs nothing more."""

    dialect = "sqlite"

    @classmethod
    def open(cls, path: str) -> SQLiteStore:
        conn = sqlite3.connect(path, isolation_level=None, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return cls(conn)

    def _sql(self, sql: str) -> str:
        return sql

    def _encode(self, value: Any) -> Any:
        return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _decode(self, raw: Any) -> Any:
        return None if raw is None else json.loads(raw)

    def _ddl_subs(self) -> dict[str, str]:
        return {"json": "TEXT", "serial": "INTEGER PRIMARY KEY AUTOINCREMENT"}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # BEGIN IMMEDIATE is SQLite's stand-in for the row locks Postgres gives
        # us: it takes the write lock up front rather than discovering the
        # conflict at commit.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")


class PostgresStore(Store):
    """The shared backend. Everything the local floor cannot do — concurrent
    writers, real jsonb, `SKIP LOCKED` for the run queue that lands next — is
    here, and one host's outage is every host's outage, which is the trade."""

    dialect = "postgres"
    _PLACEHOLDER = re.compile(r"\?")

    @classmethod
    def open(cls, url: str) -> PostgresStore:
        try:
            import psycopg2  # imported lazily: a SQLite-only host never pays
        except ImportError as exc:  # pragma: no cover - depends on host wheels
            raise StoreError("driver_missing", "psycopg2 is not installed",
                             "add psycopg2-binary to the capability's script "
                             "dependencies") from exc
        try:
            conn = psycopg2.connect(url, connect_timeout=10)
        except psycopg2.OperationalError as exc:
            raise StoreError("store_unreachable", f"cannot reach the store: {exc}",
                             "the daemon holds rather than fires while the store "
                             "is unreachable") from exc
        conn.autocommit = False
        return cls(conn)

    def _sql(self, sql: str) -> str:
        return self._PLACEHOLDER.sub("%s", sql)

    def _encode(self, value: Any) -> Any:
        from psycopg2.extras import Json
        return None if value is None else Json(value)

    def _decode(self, raw: Any) -> Any:
        # psycopg2 already adapts jsonb into Python objects.
        return raw

    def _ddl_subs(self) -> dict[str, str]:
        return {"json": "JSONB", "serial": "BIGSERIAL PRIMARY KEY"}


# --- Resolution --------------------------------------------------------------

def open_store(url: str | None = None) -> Store:
    """The one entry point. `CAPABILITIES_STORE_URL` is the root pointer that
    cannot itself live in the store, which is why it is an environment variable
    beside the secrets the cascade already resolves that way.

    Absent, the local floor is used at the caller's default path."""
    url = url or os.environ.get("CAPABILITIES_STORE_URL")
    if not url:
        raise StoreError("no_store_url", "no store URL is configured",
                         "set CAPABILITIES_STORE_URL, or pass an explicit "
                         "sqlite path")
    scheme = urlparse(url).scheme
    if scheme in ("postgres", "postgresql"):
        return PostgresStore.open(url)
    if scheme == "sqlite":
        return SQLiteStore.open(urlparse(url).path or ":memory:")
    if not scheme:
        return SQLiteStore.open(url)
    raise StoreError("unknown_store", f"unsupported store scheme {scheme!r}",
                     "expected sqlite:// or postgresql://")

# <<< contract: store <<<
