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
Scope is a column here, not a directory, and it has two values: a record
belongs to one project or to every project, and `project_id` is null in the
second case. The precedence is always project over global, but *how* the
scopes combine is declared per collection, because the standing rules already
contain two different answers and one collection needs a third:

  - MERGE  — per key; a key absent at the higher scope inherits the lower.
    This is rule 17 for the gate ("an absent project entry inherits the global
    entry"), and it is what makes a scope worth having at all. Connections
    take it too, at the entry level: rule 18 keeps an identity atomic — taken
    whole from one scope, never assembled out of two — while letting a project
    add to the set it inherits rather than replace it.

  - FIRST  — the highest scope holding ANY entry wins, whole. Nothing takes
    it by default; it is here for the project that must not see the global
    set at all, where a personal connection showing through would be wrong.

  - EXACT  — no cascade; a record belongs to exactly one scope and is read
    there. This is rule 16: state follows the scope of the credentials that
    minted it, so the global scope is not a fallback for a project's.

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
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

# >>> contract: store (generated — edit contract/store.py, run `capabilities sync-contract`) >>>
# A region carries what it needs. Relying on the host file to have imported
# the right names is a coupling nothing checks and nothing reports: it works in
# whichever capability happened to import them and fails at import time in the
# rest. Re-importing a name the host already has costs nothing.
import json
import os
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4


SCHEMA_VERSION = 4

# Two scopes and no more: a record either belongs to one project or to
# every project. `project_id` is null in the second case, which is what
# "global" means and the only thing it means.
SCOPE_KINDS = ("global", "project")

FIRST = "first"
MERGE = "merge"
EXACT = "exact"

# The collection registry: how each resolves, and who is allowed to write it.
# Adding a collection here is the only place a new record class is declared.
#
# `connection` and `grant` are the same fact split along the seam that matters:
# an entry's identity — where the thing is and how to reach it — is a fact about
# the world, the same in every project forever. What a project may DO with that
# identity is a decision, different per project by definition. Glued together,
# the second cannot be overridden without restating the first; apart, a project
# that needs write access writes one grant row and repeats no address, no host,
# no secret.
#
# An identity is atomic: it merges by entry, never by field, so no connection is
# ever assembled from two scopes. That is what rule 18's "never merged" was
# protecting, and it survives; what it gives up is refusing to let a project add
# to the global set at all.
COLLECTIONS: dict[str, dict[str, str]] = {
    "connection": {"resolve": MERGE, "writer": "human"},
    "grant": {"resolve": MERGE, "writer": "human"},
    "identifier": {"resolve": MERGE, "writer": "capability"},
    "setting": {"resolve": MERGE, "writer": "human"},
    "policy": {"resolve": MERGE, "writer": "manager"},
    # A document row holds no text — it holds the hash of the version currently
    # in force. Keeping the pin here rather than in the documents table is what
    # makes it scoped and merged like everything else, so a project may run a
    # different version of a globally declared prompt without copying it.
    "document": {"resolve": MERGE, "writer": "human"},
}

# Grant fields and what they mean when nothing declares them. `allow_write`
# falls back to the capability's own WRITE_DEFAULT rather than to a value here,
# because "does a write leave the system" is the capability's fact.
GRANT_FIELDS = ("enabled", "allow_write")

# A document version is named by the first twelve hex characters of its sha256.
# Forty-eight bits is far more than a project's worth of prose needs, and a name
# a person can read at a glance is worth more than headroom nobody will reach.
# What makes the truncation safe is not the arithmetic but `document_put`, which
# refuses a hash that already names different text rather than keeping the older
# body and serving it under the newer name.
HASH_LENGTH = 12

# A key is a label a human chose, and for a mailbox that label is naturally an
# email address — so `@` and `+` belong here. What stays out is whitespace,
# separators and anything that would make a key ambiguous to address.
KEY_RE = re.compile(r"[a-z0-9][a-z0-9._@+-]{0,127}", re.IGNORECASE)
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


def _now_exact() -> str:
    """Microsecond resolution, for the one place order matters. A revision log
    read newest-first used to break ties on a monotonic integer id; a uuid does
    not order, so the timestamp has to carry the ordering itself."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    """Which scopes a capability resolves against, highest precedence first.

    Carries the project's SLUG, because that is the handle a person and a
    project file both use. The store turns it into the project's id once and
    everything downstream joins on that: the name is the way in, the id is what
    rows hold."""

    def __init__(self, project: str | None = None, include_global: bool = True):
        self.project = project or None
        self.include_global = bool(include_global)

    @classmethod
    def from_env(cls, project: str | None = None,
                 include_global: bool = True) -> "Scopes":
        return cls(project=project or os.environ.get("CAPABILITIES_PROJECT") or None,
                   include_global=include_global)

    def write_target(self, scope: str | None) -> tuple[str, str | None]:
        """Where a write lands, as (scope, project slug). A write never guesses
        a scope it was given no name for."""
        kind = scope or "project"
        if kind == "global":
            return ("global", None)
        if kind == "project":
            if not self.project:
                raise StoreError("no_project_scope", "no project scope is in effect",
                                 "set CAPABILITIES_PROJECT or pass --scope global")
            return ("project", self.project)
        raise StoreError("bad_scope", f"scope {kind!r} must be one of {', '.join(SCOPE_KINDS)}")

    def __repr__(self) -> str:
        return (f"Scopes(project={self.project!r}, "
                f"include_global={self.include_global!r})")



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
    # A project is looked up by the slug it declares for itself; everything
    # else refers to it by id. The slug is the handle a person types, the id is
    # what a row holds, and neither has to do the other's job.
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          TEXT PRIMARY KEY,
        slug        TEXT NOT NULL UNIQUE,
        name        TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    # Where a project sits is a fact about a machine, not about the project.
    """
    CREATE TABLE IF NOT EXISTS project_paths (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id),
        instance    TEXT NOT NULL,
        path        TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (project_id, instance)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config (
        id          TEXT PRIMARY KEY,
        scope       TEXT NOT NULL,
        project_id  TEXT REFERENCES projects(id),
        capability  TEXT NOT NULL,
        collection  TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       {json} NOT NULL,
        note        TEXT,
        updated_at  TEXT NOT NULL,
        updated_by  TEXT
    )
    """,
    # NULL compares unequal to NULL in a UNIQUE constraint, in both engines, so
    # a global row would not collide with another global row. The COALESCE makes
    # the absence of a project a value like any other.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS config_identity_idx ON config (
        scope, COALESCE(project_id, ''), capability, collection, key
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS config_lookup_idx
        ON config (capability, collection, scope, project_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS state (
        id          TEXT PRIMARY KEY,
        scope       TEXT NOT NULL,
        project_id  TEXT REFERENCES projects(id),
        capability  TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       {json} NOT NULL,
        updated_at  TEXT NOT NULL,
        expires_at  TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS state_identity_idx ON state (
        scope, COALESCE(project_id, ''), capability, key
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS state_expiry_idx ON state (expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions (
        id          TEXT PRIMARY KEY,
        scope       TEXT NOT NULL,
        project_id  TEXT REFERENCES projects(id),
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
    # Long text of any kind — a prompt, a reference, an automation's script.
    # Versions accumulate and are named by the hash of their own content; which
    # one is in force is the `active` flag on the row itself, so the answer sits
    # in the table anyone would look in rather than in a pin somewhere else.
    """
    CREATE TABLE IF NOT EXISTS context (
        id          TEXT PRIMARY KEY,
        scope       TEXT NOT NULL,
        project_id  TEXT REFERENCES projects(id),
        capability  TEXT NOT NULL,
        key         TEXT NOT NULL,
        hash        TEXT NOT NULL,
        body        TEXT NOT NULL,
        media_type  TEXT,
        active      INTEGER NOT NULL DEFAULT 0,
        author      TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS context_version_idx ON context (
        scope, COALESCE(project_id, ''), capability, key, hash
    )
    """,
    # At most one version of one item is in force in one scope, and the database
    # is what says so — not a convention the writers are trusted to keep.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS context_one_active_idx ON context (
        scope, COALESCE(project_id, ''), capability, key
    ) WHERE active = 1
    """,
    """
    CREATE INDEX IF NOT EXISTS context_history_idx
        ON context (capability, key, created_at)
    """,
]


def _uuid() -> str:
    return str(uuid4())


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

    def __enter__(self) -> "Store":
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

    # -- scope resolution ------------------------------------------------------

    def _project_id(self, slug: str | None) -> str | None:
        """A slug in, an id out. Looked up once and remembered, because every
        query in a session asks the same question about the same project."""
        if not slug:
            return None
        cache = getattr(self, "_project_ids", None)
        if cache is None:
            cache = self._project_ids = {}
        if slug not in cache:
            cur = self._execute("SELECT id FROM projects WHERE slug = ?", (slug,))
            row = cur.fetchone()
            cache[slug] = row[0] if row else None
        return cache[slug]

    def _chain(self, scopes: Scopes) -> list[tuple[str, str | None]]:
        """The scopes to read, highest first. A project nobody registered
        contributes nothing rather than matching everything."""
        project_id = self._project_id(scopes.project)
        out: list[tuple[str, str | None]] = []
        if project_id:
            out.append(("project", project_id))
        if scopes.include_global:
            out.append(("global", None))
        return out

    def _target(self, scope: tuple) -> tuple[str, str | None]:
        """A write target as (scope, project id), from a scope and a slug."""
        kind, slug = (list(scope) + [None])[:2]
        if kind not in SCOPE_KINDS:
            raise StoreError("bad_scope",
                             f"scope {kind!r} must be one of {', '.join(SCOPE_KINDS)}")
        if kind == "global":
            if slug:
                raise StoreError("bad_scope", "the global scope takes no project")
            return ("global", None)
        project_id = self._project_id(slug)
        if not project_id:
            raise StoreError("unknown_project", f"no project registered as {slug!r}",
                             "register it before writing records scoped to it")
        return ("project", project_id)

    @staticmethod
    def _chain_clause(chain: list) -> tuple[str, list]:
        if not chain:
            return "0", []
        clause = " OR ".join(["(scope = ? AND COALESCE(project_id,'') = ?)"] * len(chain))
        params: list[Any] = []
        for kind, pid in chain:
            params += [kind, pid or ""]
        return clause, params

    # -- config: read ---------------------------------------------------------

    def config_get(self, capability: str, collection: str, key: str,
                   scopes: Scopes) -> Any:
        """One resolved value, or None. Follows the collection's semantics."""
        entry = self.config_resolve(capability, collection, scopes).get(key)
        return entry["value"] if entry else None

    def config_resolve(self, capability: str, collection: str,
                       scopes: Scopes) -> dict[str, dict[str, Any]]:
        """The whole collection as the capability should see it, each entry
        carrying the scope it came from so `connections` can report resolution
        without a second query."""
        _check("capability", capability, CAPABILITY_RE)
        semantics = self._semantics(collection)
        if semantics == EXACT:
            raise StoreError("bad_collection",
                             f"collection {collection!r} is state, not config",
                             "read it with state_get")
        chain = self._chain(scopes)
        clause, params = self._chain_clause(chain)
        cur = self._execute(
            "SELECT id, scope, project_id, capability, collection, key, value, "
            f"note, updated_at, updated_by FROM config WHERE capability = ? AND collection = ? "
            f"AND ({clause})", [capability, collection] + params)
        rows = [self._row(r) for r in cur.fetchall()]
        by_scope: dict[tuple, dict[str, dict[str, Any]]] = {}
        for row in rows:
            by_scope.setdefault(
                (row["scope"], row["project_id"] or ""), {}
            )[row["key"]] = row

        out: dict[str, dict[str, Any]] = {}
        for kind, pid in chain:
            entries = by_scope.get((kind, pid or ""))
            if not entries:
                continue
            if semantics == FIRST:
                return entries
            for key, row in entries.items():
                out.setdefault(key, row)
        return out

    def config_origin(self, capability: str, collection: str, key: str,
                      scopes: Scopes) -> tuple[str, str | None] | None:
        entry = self.config_resolve(capability, collection, scopes).get(key)
        return (entry["scope"], entry["project_id"]) if entry else None

    def config_list(self, capability: str, collection: str,
                    scope: tuple | None = None) -> list[dict[str, Any]]:
        """Every stored row, unresolved — the view for editing rather than use."""
        _check("capability", capability, CAPABILITY_RE)
        self._semantics(collection)
        sql = ("SELECT id, scope, project_id, capability, collection, key, value, "
               "note, updated_at, updated_by FROM config WHERE capability = ? AND collection = ?")
        params: list[Any] = [capability, collection]
        if scope:
            kind, pid = self._target(scope)
            sql += " AND scope = ? AND COALESCE(project_id,'') = ?"
            params += [kind, pid or ""]
        cur = self._execute(sql + " ORDER BY scope, key", params)
        return [self._row(r) for r in cur.fetchall()]

    def _row(self, r: Sequence[Any]) -> dict[str, Any]:
        return {
            "id": r[0], "scope": r[1], "project_id": r[2],
            "capability": r[3], "collection": r[4], "key": r[5],
            "value": self._decode(r[6]), "note": r[7],
            "updated_at": r[8], "updated_by": r[9],
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
                   scope: tuple, actor: str | None = None,
                   note: str | None = None) -> str:
        """Write one entry at one scope and record the revision. Returns the
        row's id. A write always names its scope; nothing infers one."""
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        self._semantics(collection)
        kind, pid = self._target(scope)
        with self.transaction():
            cur = self._execute(
                "SELECT id, value FROM config WHERE scope = ? AND COALESCE(project_id,'') = ? "
                "AND capability = ? AND collection = ? AND key = ?",
                (kind, pid or "", capability, collection, key))
            existing = cur.fetchone()
            before = self._decode(existing[1]) if existing else None
            if existing:
                row_id = existing[0]
                self._execute(
                    "UPDATE config SET value = ?, note = ?, updated_at = ?, updated_by = ? "
                    "WHERE id = ?",
                    (self._encode(value), note, _now(), actor, row_id))
            else:
                row_id = _uuid()
                self._execute(
                    "INSERT INTO config (id, scope, project_id, capability, "
                    "collection, key, value, note, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_id, kind, pid, capability, collection, key,
                     self._encode(value), note, _now(), actor))
            self._revise(kind, pid, capability, collection, key, before, value, actor)
        return row_id

    def config_delete(self, capability: str, collection: str, key: str,
                      scope: tuple, actor: str | None = None) -> bool:
        _check("capability", capability, CAPABILITY_RE)
        self._semantics(collection)
        kind, pid = self._target(scope)
        with self.transaction():
            cur = self._execute(
                "SELECT id, value FROM config WHERE scope = ? AND COALESCE(project_id,'') = ? "
                "AND capability = ? AND collection = ? AND key = ?",
                (kind, pid or "", capability, collection, key))
            existing = cur.fetchone()
            if not existing:
                return False
            self._execute("DELETE FROM config WHERE id = ?", (existing[0],))
            self._revise(kind, pid, capability, collection, key,
                         self._decode(existing[1]), None, actor)
        return True

    def _revise(self, scope: str, project_id: str | None, capability: str, collection: str, key: str,
                old: Any, new: Any, actor: str | None) -> None:
        self._execute(
            "INSERT INTO revisions (id, scope, project_id, capability, collection, "
            "key, old_value, new_value, actor, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_uuid(), scope, project_id, capability, collection, key,
             self._encode(old), self._encode(new), actor, _now_exact()))

    def revisions(self, capability: str, collection: str | None = None,
                  key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """What the repository's history used to answer: who changed this, when,
        and what it was before."""
        sql = ("SELECT scope, project_id, collection, key, old_value, new_value, "
               "actor, written_at FROM revisions WHERE capability = ?")
        params: list[Any] = [capability]
        if collection:
            sql += " AND collection = ?"
            params.append(collection)
        if key:
            sql += " AND key = ?"
            params.append(key)
        cur = self._execute(sql + " ORDER BY written_at DESC LIMIT ?", params + [limit])
        return [{"scope": r[0], "project_id": r[1], "collection": r[2],
                 "key": r[3], "old_value": self._decode(r[4]),
                 "new_value": self._decode(r[5]), "actor": r[6], "written_at": r[7]}
                for r in cur.fetchall()]

    # -- connections, with their grants applied --------------------------------

    def connections_effective(self, capability: str, scopes: Scopes,
                              write_default: bool = False,
                              include_disabled: bool = False) -> dict[str, dict[str, Any]]:
        """Every connection this project may use, already carrying the decision
        made about it — the one read a capability needs before it acts."""
        identities = self.config_resolve(capability, "connection", scopes)
        grants = self.config_resolve(capability, "grant", scopes)
        out: dict[str, dict[str, Any]] = {}
        for cid, entry in identities.items():
            grant = grants.get(cid)
            decided = grant["value"] if grant else {}
            if not isinstance(decided, dict):
                raise StoreError("bad_grant",
                                 f"grant {cid!r} must be a table, got {type(decided).__name__}")
            unknown = set(decided) - set(GRANT_FIELDS)
            if unknown:
                raise StoreError("bad_grant",
                                 f"grant {cid!r} has unknown field(s): {', '.join(sorted(unknown))}",
                                 f"known: {', '.join(GRANT_FIELDS)}")
            enabled = bool(decided.get("enabled", True))
            if not enabled and not include_disabled:
                continue
            out[cid] = {
                "id": cid,
                "value": entry["value"],
                "scope": (entry["scope"], entry["project_id"]),
                "enabled": enabled,
                "allow_write": bool(decided.get("allow_write", write_default)),
                "grant_scope": (grant["scope"], grant["project_id"]) if grant else None,
            }
        return out

    def grant_orphans(self, capability: str, scopes: Scopes) -> list[dict[str, Any]]:
        """Grants naming a connection that does not resolve. A grant is the only
        record that refers to another, so it is the only one that can be aimed at
        nothing, and dropping it quietly would read exactly like permission
        deliberately withheld."""
        identities = self.config_resolve(capability, "connection", scopes)
        grants = self.config_resolve(capability, "grant", scopes)
        return [{"key": key, "scope": (row["scope"], row["project_id"]),
                 "value": row["value"]}
                for key, row in sorted(grants.items()) if key not in identities]

    # -- context: long text, versioned by content, one version in force --------

    def context_put(self, capability: str, key: str, body: str, scope: tuple,
                    author: str | None = None, media_type: str | None = None,
                    activate: bool = False) -> str:
        """Record a version and return its hash. Saving is not deploying: this
        changes nothing that runs unless `activate` says so, or until
        `context_activate` names the hash later."""
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        kind, pid = self._target(scope)
        if not isinstance(body, str):
            raise StoreError("bad_context", "a context body must be text")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:HASH_LENGTH]
        with self.transaction():
            cur = self._execute(
                "SELECT id, body FROM context WHERE scope = ? AND COALESCE(project_id,'') = ? "
                "AND capability = ? AND key = ? AND hash = ?",
                (kind, pid or "", capability, key, digest))
            existing = cur.fetchone()
            if existing and existing[1] != body:
                raise StoreError(
                    "hash_collision",
                    f"version {digest} of {capability}/{key} already names different text",
                    "this is what the truncated hash is checked for; report it")
            if not existing:
                self._execute(
                    "INSERT INTO context (id, scope, project_id, capability, key, "
                    "hash, body, media_type, active, author, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (_uuid(), kind, pid, capability, key, digest, body,
                     media_type, author, _now()))
            if activate:
                self._activate(kind, pid, capability, key, digest, author)
        return digest

    def context_activate(self, capability: str, key: str, digest: str, scope: tuple,
                         actor: str | None = None) -> None:
        """Put one version in force — the deploy step. At most one version of one
        item is active in one scope, and the unique index is what says so rather
        than a convention the writers are trusted to keep."""
        kind, pid = self._target(scope)
        with self.transaction():
            self._activate(kind, pid, capability, key, digest, actor)

    def _activate(self, kind: str, pid: str | None, capability: str, key: str, digest: str, actor: str | None) -> None:
        cur = self._execute(
            "SELECT id, hash FROM context WHERE scope = ? AND COALESCE(project_id,'') = ? "
            "AND capability = ? AND key = ? AND active = 1",
            (kind, pid or "", capability, key))
        current = cur.fetchone()
        target = self._execute(
            "SELECT id FROM context WHERE scope = ? AND COALESCE(project_id,'') = ? "
            "AND capability = ? AND key = ? AND hash = ?",
            (kind, pid or "", capability, key, digest)).fetchone()
        if not target:
            raise StoreError("unknown_version",
                             f"no recorded version {digest} of {capability}/{key} in this scope",
                             "record it with context_put first")
        if current and current[0] == target[0]:
            return
        # Clear first: the partial unique index would otherwise refuse the second
        # active row before the old one is stood down.
        if current:
            self._execute("UPDATE context SET active = 0 WHERE id = ?", (current[0],))
        self._execute("UPDATE context SET active = 1 WHERE id = ?", (target[0],))
        self._revise(kind, pid, capability, "context", key,
                     current[1] if current else None, digest, actor)

    def context_read(self, capability: str, key: str,
                     scopes: Scopes) -> dict[str, Any] | None:
        """The version in force here, found at the highest scope that has one."""
        for kind, pid in self._chain(scopes):
            cur = self._execute(
                "SELECT id, hash, body, media_type, author, created_at, scope, project_id "
                "FROM context WHERE scope = ? AND COALESCE(project_id,'') = ? "
                "AND capability = ? AND key = ? AND active = 1",
                (kind, pid or "", capability, key))
            row = cur.fetchone()
            if row:
                return {"id": row[0], "key": key, "hash": row[1], "body": row[2],
                        "media_type": row[3], "author": row[4], "created_at": row[5],
                        "scope": (row[6], row[7])}
        return None

    def context_versions(self, capability: str, key: str,
                         scopes: Scopes) -> list[dict[str, Any]]:
        """Every version this project can see, newest first — its own chain and
        nothing else. Bodies are omitted; a listing should not carry the whole of
        what it lists."""
        clause, params = self._chain_clause(self._chain(scopes))
        cur = self._execute(
            "SELECT id, hash, media_type, author, created_at, length(body), active, scope "
            f"FROM context WHERE capability = ? AND key = ? AND ({clause}) "
            "ORDER BY created_at DESC, hash", [capability, key] + params)
        return [{"id": r[0], "hash": r[1], "media_type": r[2], "author": r[3],
                 "created_at": r[4], "bytes": r[5], "active": bool(r[6]), "scope": r[7]}
                for r in cur.fetchall()]

    def context_keys(self, capability: str, scopes: Scopes) -> list[str]:
        clause, params = self._chain_clause(self._chain(scopes))
        cur = self._execute(
            f"SELECT DISTINCT key FROM context WHERE capability = ? AND ({clause}) ORDER BY key",
            [capability] + params)
        return [r[0] for r in cur.fetchall()]

    # -- the project registry --------------------------------------------------

    def project_register(self, project_id: str, slug: str, name: str | None = None) -> None:
        """Claim a slug for a project id. Re-registering the same id is how a
        second machine joins a project it already belongs to. A slug held by a
        different id is refused: that is another project, and taking its label
        would take its rows."""
        _check("slug", slug, CAPABILITY_RE)
        if not isinstance(project_id, str) or not project_id.strip():
            raise StoreError("bad_project_id", "a project id is required",
                             "generate one once and keep it in the project's own config")
        with self.transaction():
            holder = self._execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
            if holder and holder[0] != project_id:
                raise StoreError(
                    "slug_taken", f"the label {slug!r} belongs to project {holder[0]}",
                    "pick another label; this is a different project, and sharing "
                    "the label would share its rows")
            existing = self._execute(
                "SELECT slug FROM projects WHERE id = ?", (project_id,)).fetchone()
            if existing and existing[0] != slug:
                raise StoreError(
                    "slug_immutable",
                    f"project {project_id} is already labelled {existing[0]!r}",
                    "rows are scoped by the id, but the label is how it is found, "
                    "so renaming one is a migration rather than an edit")
            if existing:
                self._execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
            else:
                self._execute(
                    "INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                    (project_id, slug, name, _now()))
        getattr(self, "_project_ids", {}).pop(slug, None)

    def project_get(self, slug: str) -> dict[str, Any] | None:
        row = self._execute(
            "SELECT id, slug, name, created_at FROM projects WHERE slug = ?", (slug,)).fetchone()
        return {"id": row[0], "slug": row[1], "name": row[2], "created_at": row[3]} if row else None

    def project_by_id(self, project_id: str) -> dict[str, Any] | None:
        row = self._execute(
            "SELECT id, slug, name, created_at FROM projects WHERE id = ?",
            (project_id,)).fetchone()
        return {"id": row[0], "slug": row[1], "name": row[2], "created_at": row[3]} if row else None

    def project_list(self) -> list[dict[str, Any]]:
        cur = self._execute("SELECT id, slug, name, created_at FROM projects ORDER BY slug")
        return [{"id": r[0], "slug": r[1], "name": r[2], "created_at": r[3]}
                for r in cur.fetchall()]

    def project_bind_path(self, slug: str, instance: str, path: str) -> None:
        """Record where this project sits on this machine."""
        project_id = self._project_id(slug)
        if not project_id:
            raise StoreError("unknown_project", f"no project registered as {slug!r}",
                             "register it first")
        with self.transaction():
            existing = self._execute(
                "SELECT id FROM project_paths WHERE project_id = ? AND instance = ?",
                (project_id, instance)).fetchone()
            if existing:
                self._execute("UPDATE project_paths SET path = ?, updated_at = ? WHERE id = ?",
                              (path, _now(), existing[0]))
            else:
                self._execute(
                    "INSERT INTO project_paths (id, project_id, instance, path, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)", (_uuid(), project_id, instance, path, _now()))

    def project_path(self, slug: str, instance: str) -> str | None:
        project_id = self._project_id(slug)
        if not project_id:
            return None
        row = self._execute(
            "SELECT path FROM project_paths WHERE project_id = ? AND instance = ?",
            (project_id, instance)).fetchone()
        return row[0] if row else None

    # -- state ----------------------------------------------------------------

    def state_get(self, capability: str, key: str, scope: tuple) -> Any:
        """Rule 16: state is read at exactly the scope that minted it. A
        project's state is not a fallback for an instance's, so no cascade."""
        _check("capability", capability, CAPABILITY_RE)
        kind, pid = self._target(scope)
        row = self._execute(
            "SELECT value, expires_at FROM state WHERE scope = ? AND COALESCE(project_id,'') = ? "
            "AND capability = ? AND key = ?",
            (kind, pid or "", capability, key)).fetchone()
        if not row or (row[1] and row[1] <= _now()):
            return None
        return self._decode(row[0])

    def state_set(self, capability: str, key: str, value: Any, scope: tuple,
                  ttl_seconds: int | None = None) -> None:
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        kind, pid = self._target(scope)
        expires = None
        if ttl_seconds is not None:
            expires = (datetime.now(timezone.utc)
                       + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self.transaction():
            existing = self._execute(
                "SELECT id FROM state WHERE scope = ? AND COALESCE(project_id,'') = ? "
                "AND capability = ? AND key = ?",
                (kind, pid or "", capability, key)).fetchone()
            if existing:
                self._execute(
                    "UPDATE state SET value = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                    (self._encode(value), _now(), expires, existing[0]))
            else:
                self._execute(
                    "INSERT INTO state (id, scope, project_id, capability, key, "
                    "value, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_uuid(), kind, pid, capability, key,
                     self._encode(value), _now(), expires))

    def state_delete(self, capability: str, key: str, scope: tuple) -> bool:
        kind, pid = self._target(scope)
        with self.transaction():
            cur = self._execute(
                "DELETE FROM state WHERE scope = ? AND COALESCE(project_id,'') = ? "
                "AND capability = ? AND key = ?",
                (kind, pid or "", capability, key))
        return bool(cur.rowcount)

    def state_sweep(self) -> int:
        """Drop what has expired. Cheap enough to call on a daemon tick."""
        with self.transaction():
            cur = self._execute(
                "DELETE FROM state WHERE expires_at IS NOT NULL AND expires_at <= ?", (_now(),))
        return cur.rowcount or 0


# --- Adapters ----------------------------------------------------------------

class SQLiteStore(Store):
    """The single-user local floor. Honest about being that: one writer, no
    `SKIP LOCKED`, JSON stored as text and unindexed. A project that never
    leaves one machine needs nothing more."""

    dialect = "sqlite"

    @classmethod
    def open(cls, path: str) -> "SQLiteStore":
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
    def open(cls, url: str) -> "PostgresStore":
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

def default_store_path() -> str:
    """Where records live when nothing says otherwise.

    A store is not optional the way a config source is. A queue, a ledger, a
    cursor have to go somewhere, and letting each capability invent its own
    database for them is how they came to be scattered in the first place. So
    there is always one, and the first caller to ask for it creates it.

    The fenced body cannot ask the manager where that is — it may not import a
    sibling, and a subprocess per open would be absurd — so both compute the
    same XDG convention independently. That is one convention arrived at twice,
    not one value stored twice, but the two must agree: `capabilities path
    store` is the authority, and anything that overrides the location does it
    through `CAPABILITIES_STORE_URL`, which both read first."""
    home = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(home, "capabilities", "store.db")


def open_store(url: str | None = None) -> "Store":
    """The one entry point. `CAPABILITIES_STORE_URL` is the root pointer that
    cannot itself live in the store, which is why it is an environment variable
    beside the secrets the cascade already resolves that way.

    Absent, the local floor is opened at the default path, and created there if
    nothing has created it yet."""
    url = url or os.environ.get("CAPABILITIES_STORE_URL") or default_store_path()
    scheme = urlparse(url).scheme
    if scheme in ("", "sqlite"):
        path = urlparse(url).path if scheme == "sqlite" else url
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if scheme in ("postgres", "postgresql"):
        return PostgresStore.open(url)
    if scheme == "sqlite":
        return SQLiteStore.open(urlparse(url).path or ":memory:")
    if not scheme:
        return SQLiteStore.open(url)
    raise StoreError("unknown_store", f"unsupported store scheme {scheme!r}",
                     "expected sqlite:// or postgresql://")


# --- records: the two places a project may keep its configuration ------------
#
# Tracking is not on this axis. Runs, queues and cursors live in the store
# always, because a ledger with a files mode is a ledger nothing can query.
# What a project chooses is where its CONFIGURATION is read and written, and
# these two answer that. A capability calls the same verbs either way and is
# never told which answered, which is the whole point: a call site that can
# tell is a call site that will eventually decide.

DOCUMENT_LOCATIONS: tuple[tuple[str | None, str, str], ...] = (
    ("reference", "{cap}/reference", ".md"),
    ("context", "{cap}/service/context", ".md"),
    ("script", "automations/scripts", ".py"),
    (None, "{cap}/service", ".md"),
)


def _document_key(prefix: str | None, stem: str) -> str:
    """The key a file answers to. Underscores fold to hyphens and case is lost,
    so the map from file to key is one-way — which is why finding a document
    searches rather than computes."""
    slug = stem.replace("_", "-").lower()
    return f"{prefix}.{slug}" if prefix else slug


class Records:
    """The surface a capability reads and writes its configuration through.

    Every verb here is answered by both a directory of files and a database.
    Neither is asked which it is."""

    mode = "?"
    source = "?"

    def __enter__(self) -> "Records":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        pass


class StoreRecords(Records):
    """Configuration kept as rows. Thin, because the store already does this —
    what this adds is a fixed project, so no call site carries a scope it could
    forget."""

    mode = "db"

    def __init__(self, store: "Store", scopes: "Scopes", write_scope: tuple):
        self.store, self.scopes, self.write_scope = store, scopes, write_scope
        self.source = getattr(store, "label", "store")

    def close(self) -> None:
        self.store.close()

    def resolve(self, capability: str, collection: str) -> dict[str, dict[str, Any]]:
        return self.store.config_resolve(capability, collection, self.scopes)

    def get(self, capability: str, collection: str, key: str) -> Any:
        return self.store.config_get(capability, collection, key, self.scopes)

    def set(self, capability: str, collection: str, key: str, value: Any,
            actor: str | None = None, note: str | None = None) -> None:
        self.store.config_set(capability, collection, key, value,
                              self.write_scope, actor=actor, note=note)

    def delete(self, capability: str, collection: str, key: str) -> bool:
        return self.store.config_delete(capability, collection, key, self.write_scope)

    def connections(self, capability: str, write_default: bool = False,
                    include_disabled: bool = False) -> dict[str, dict[str, Any]]:
        return self.store.connections_effective(
            capability, self.scopes, write_default=write_default,
            include_disabled=include_disabled)

    def document_read(self, capability: str, key: str) -> dict[str, Any] | None:
        return self.store.context_read(capability, key, self.scopes)

    def document_keys(self, capability: str) -> list[str]:
        return self.store.context_keys(capability, self.scopes)

    def collection_source(self, capability: str, collection: str) -> str:
        """What answered. A row has no path, so the store says so as itself."""
        return self.source

    def document_path(self, capability: str, key: str) -> Path | None:
        """Nothing to open: a version in the store is not a file anywhere. The
        working copy an edit needs is the caller's to make, and `put` is what
        makes it land."""
        return None

    def document_put(self, capability: str, key: str, body: str,
                     author: str | None = None, media_type: str | None = None,
                     base: str | None = None) -> str:
        if base is not None:
            current = self.store.context_read(capability, key, self.scopes)
            live = current["hash"] if current else None
            if live != base:
                raise StoreError(
                    "stale_edit",
                    f"{capability}/{key} was {live or 'absent'} when this landed, "
                    f"not {base} as it was when the edit began",
                    "re-read it and apply the change again")
        return self.store.context_put(capability, key, body, self.write_scope,
                                      author=author, media_type=media_type,
                                      activate=True)


class FileRecords(Records):
    """Configuration kept as the files the envelope has always held.

    The layout is not invented here — it is the one the capabilities already
    read, described in one place instead of re-derived at each call site. Two
    directories with that layout make the scope chain: the project's envelope,
    then the user's config home."""

    mode = "files"

    def __init__(self, envelope: Path, global_dir: Path,
                 project_id: str | None = None, project: str | None = None,
                 include_global: bool = True):
        self.envelope, self.global_dir = Path(envelope), Path(global_dir)
        self.project_id, self.project = project_id, project
        self.include_global = bool(include_global)
        self.source = str(self.envelope)

    # -- where a record lives --------------------------------------------------

    def _chain(self) -> list[tuple[str, str | None, Path]]:
        chain = [("project", self.project_id, self.envelope)]
        if self.include_global:
            chain.append(("global", None, self.global_dir))
        return chain

    @staticmethod
    def _load(path: Path) -> Any:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise StoreError("bad_config", f"cannot read {path}: {exc}") from None

    @staticmethod
    def _save(path: Path, body: Any, sort: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False,
                                   sort_keys=sort) + "\n")

    def _policy_file(self, root: Path) -> Path:
        """The gate. A project keeps it at the envelope root; the config home
        keeps it under the manager's own name, which is the same rule seen from
        outside -- there, `capabilities` is a capability like any other."""
        return (root / "settings.json" if root == self.envelope
                else root / "capabilities" / "settings.json")

    def _file(self, root: Path, capability: str, collection: str,
              key: str | None = None) -> Path:
        if collection == "policy":
            return self._policy_file(root)
        if collection == "identifier":
            return root / capability / "identifiers.json"
        if collection in ("connection", "grant"):
            return root / capability / "connections.json"
        if collection == "setting":
            if key == "connection.default":
                return root / capability / "connections.json"
            return root / capability / "service" / "settings.json"
        raise StoreError("bad_collection",
                         f"collection {collection!r} is not kept in files",
                         "it exists only where the project keeps records in the store")

    # -- reading ---------------------------------------------------------------

    def _entries(self, root: Path, capability: str, collection: str) -> dict[str, Any]:
        """Every entry of one collection held at one scope, keyed as the store
        keys them, so the two agree on what a record is called."""
        if collection == "policy":
            body = self._load(self._policy_file(root)) or {}
            return dict((body.get("capabilities") or {}))
        if collection == "identifier":
            body = self._load(root / capability / "identifiers.json") or {}
            raw = body.get("identifiers") if isinstance(body.get("identifiers"), dict) else body
            out = {}
            for label, entry in raw.items():
                out[label] = entry["value"] if isinstance(entry, dict) and "value" in entry else entry
            return out
        registry = self._load(root / capability / "connections.json") or {}
        if collection in ("connection", "grant"):
            declared = registry.get("connections")
            if not isinstance(declared, dict):
                return {}
            out = {}
            for cid, entry in declared.items():
                if not isinstance(entry, dict):
                    continue
                if collection == "connection":
                    out[cid] = {k: v for k, v in entry.items() if k not in GRANT_FIELDS}
                else:
                    grant = {k: entry[k] for k in GRANT_FIELDS if k in entry}
                    if grant:
                        out[cid] = grant
            return out
        if collection == "setting":
            out = {}
            if registry.get("default"):
                out["connection.default"] = registry["default"]
            service = self._load(root / capability / "service" / "settings.json")
            if isinstance(service, dict):
                out.update(service)
            return out
        raise StoreError("bad_collection", f"unknown collection {collection!r}",
                         f"known: {', '.join(sorted(COLLECTIONS))}")

    def _note(self, root: Path, capability: str, collection: str, key: str) -> str | None:
        if collection != "identifier":
            return None
        body = self._load(root / capability / "identifiers.json") or {}
        raw = body.get("identifiers") if isinstance(body.get("identifiers"), dict) else body
        entry = raw.get(key)
        return entry.get("note") if isinstance(entry, dict) else None

    def resolve(self, capability: str, collection: str) -> dict[str, dict[str, Any]]:
        _check("capability", capability, CAPABILITY_RE)
        semantics = Store._semantics(collection)
        out: dict[str, dict[str, Any]] = {}
        for scope, pid, root in self._chain():
            entries = self._entries(root, capability, collection)
            if not entries:
                continue
            rows = {
                key: {"id": f"{scope}:{capability}/{collection}/{key}", "scope": scope,
                      "project_id": pid, "capability": capability,
                      "collection": collection, "key": key, "value": value,
                      "note": self._note(root, capability, collection, key),
                      "updated_at": None, "updated_by": None}
                for key, value in entries.items()
            }
            if semantics == FIRST:
                return rows
            for key, row in rows.items():
                out.setdefault(key, row)
        return out

    def get(self, capability: str, collection: str, key: str) -> Any:
        entry = self.resolve(capability, collection).get(key)
        return entry["value"] if entry else None

    def collection_source(self, capability: str, collection: str) -> str:
        """The file that answered, named exactly. A report that says only
        "files" makes the reader go and find out which one, and the scope a
        record resolved at is half of what they came to learn."""
        for _scope, _pid, root in self._chain():
            if self._entries(root, capability, collection):
                return str(self._file(root, capability, collection))
        return str(self._file(self.envelope, capability, collection))

    def connections(self, capability: str, write_default: bool = False,
                    include_disabled: bool = False) -> dict[str, dict[str, Any]]:
        """Assembled exactly as the store assembles it, from the same two
        collections, so a project changing where it keeps records does not
        change which connection answers."""
        return Store.connections_effective(
            self, capability, None, write_default=write_default,
            include_disabled=include_disabled)

    def config_resolve(self, capability: str, collection: str, _scopes: Any) -> dict:
        """`connections_effective` reaches for this name; the scopes it passes
        are already fixed here."""
        return self.resolve(capability, collection)

    # -- writing ---------------------------------------------------------------

    def set(self, capability: str, collection: str, key: str, value: Any,
            actor: str | None = None, note: str | None = None) -> None:
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        Store._semantics(collection)
        if not self.envelope.is_dir():
            raise StoreError("no_envelope", "no capabilities/ envelope in this project",
                             "run `capabilities init` first")
        path = self._file(self.envelope, capability, collection, key)
        body = self._load(path)
        body = body if isinstance(body, dict) else {}
        if collection == "policy":
            body.setdefault("capabilities", {})[key] = value
        elif collection == "identifier":
            # Flat at the top level, which is the shape the envelope has always
            # had and the shape `audit` holds this to. A file already carrying
            # the wrapper keeps it: reshaping someone's file on an unrelated
            # write is not this writer's business.
            holder = (body["identifiers"] if isinstance(body.get("identifiers"), dict)
                      else body)
            holder[key] = {"value": value, "note": note or ""}
        elif collection in ("connection", "grant"):
            entry = body.setdefault("connections", {}).setdefault(key, {})
            if collection == "connection":
                for field in list(entry):
                    if field not in GRANT_FIELDS:
                        entry.pop(field)
                entry.update(value if isinstance(value, dict) else {})
            else:
                entry.update({k: v for k, v in (value or {}).items() if k in GRANT_FIELDS})
        elif key == "connection.default":
            body["default"] = value
        else:
            body[key] = value
        self._save(path, body, sort=collection == "identifier")

    def delete(self, capability: str, collection: str, key: str) -> bool:
        path = self._file(self.envelope, capability, collection, key)
        body = self._load(path)
        if not isinstance(body, dict):
            return False
        if collection == "policy":
            gone = (body.get("capabilities") or {}).pop(key, None) is not None
        elif collection == "identifier":
            holder = body.get("identifiers") if isinstance(body.get("identifiers"), dict) else body
            gone = holder.pop(key, None) is not None
        elif collection in ("connection", "grant"):
            entry = (body.get("connections") or {}).get(key)
            if not isinstance(entry, dict):
                return False
            if collection == "connection":
                gone = body["connections"].pop(key, None) is not None
            else:
                gone = any(entry.pop(field, None) is not None for field in GRANT_FIELDS)
        elif key == "connection.default":
            gone = body.pop("default", None) is not None
        else:
            gone = body.pop(key, None) is not None
        if gone:
            self._save(path, body, sort=collection == "identifier")
        return gone

    # -- documents -------------------------------------------------------------

    def _documents(self, root: Path, capability: str) -> dict[str, Path]:
        """Every long-text file this scope holds, by the key it answers to. The
        map is built by walking rather than by computing a path from a key,
        because the key loses case and underscores on the way in."""
        found: dict[str, Path] = {}
        for prefix, shape, suffix in DOCUMENT_LOCATIONS:
            if prefix == "script" and capability != "automations":
                continue
            folder = root / shape.format(cap=capability)
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob(f"*{suffix}")):
                if path.is_file():
                    found.setdefault(_document_key(prefix, path.stem), path)
        return found

    def document_path(self, capability: str, key: str) -> Path | None:
        """The file this key names. It is the truth here, not a copy of it, so
        an edit to what this returns is the edit — which is why `put` validates
        rather than transports."""
        for _scope, _pid, root in self._chain():
            path = self._documents(root, capability).get(key)
            if path:
                return path
        return None

    def document_read(self, capability: str, key: str) -> dict[str, Any] | None:
        for scope, pid, root in self._chain():
            path = self._documents(root, capability).get(key)
            if not path:
                continue
            body = path.read_text()
            return {"id": str(path), "key": key,
                    "hash": hashlib.sha256(body.encode()).hexdigest()[:HASH_LENGTH],
                    "body": body, "media_type": None, "author": None,
                    "created_at": None, "scope": (scope, pid), "path": str(path)}
        return None

    def document_keys(self, capability: str) -> list[str]:
        keys: set[str] = set()
        for _scope, _pid, root in self._chain():
            keys.update(self._documents(root, capability))
        return sorted(keys)

    def document_put(self, capability: str, key: str, body: str,
                     author: str | None = None, media_type: str | None = None,
                     base: str | None = None) -> str:
        path = self._documents(self.envelope, capability).get(key)
        if path is None:
            path = self._new_document_path(capability, key)
        if base is not None and path.is_file():
            current = path.read_text()
            live = hashlib.sha256(current.encode()).hexdigest()[:HASH_LENGTH]
            # In files mode `context edit` deliberately hands out the truth,
            # not a copy. By put-time the live hash is therefore expected to
            # differ from the checkout hash; equality of the submitted body and
            # the file proves the edit landed at the path the adapter named.
            if live != base and current != body:
                raise StoreError(
                    "stale_edit",
                    f"{path} is {live} now, not {base} as it was when the edit began",
                    "re-read it and apply the change again")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return hashlib.sha256(body.encode()).hexdigest()[:HASH_LENGTH]

    def _new_document_path(self, capability: str, key: str) -> Path:
        """Where a document this project does not have yet would go. The prefix
        chooses the folder; what follows it is the file."""
        for prefix, shape, suffix in DOCUMENT_LOCATIONS:
            if prefix and key.startswith(prefix + "."):
                return self.envelope / shape.format(cap=capability) / (key[len(prefix) + 1:] + suffix)
        prefix, shape, suffix = DOCUMENT_LOCATIONS[-1]
        return self.envelope / shape.format(cap=capability) / (key + suffix)

    # -- what a directory cannot answer ---------------------------------------

    def _refuse(self, what: str) -> None:
        raise StoreError("files_mode", f"{what} needs records kept in the store",
                         "this project keeps them in files, where there is no history")

    def revisions(self, *_a: Any, **_k: Any) -> list:
        self._refuse("a record's history")
        return []

    def document_versions(self, *_a: Any, **_k: Any) -> list:
        self._refuse("a document's earlier versions")
        return []


def records_mode(envelope: Path | str) -> tuple[str, str]:
    """Where this project keeps its configuration, and what said so.

    This is the only place the choice is read. Everything downstream takes an
    adapter and cannot tell which it got, because a call site that can tell is
    a call site that will eventually decide for itself."""
    override = os.environ.get("CAPABILITIES_STORE_MODE")
    if override:
        if override not in ("files", "db"):
            raise StoreError("bad_store_mode",
                             f"CAPABILITIES_STORE_MODE must be files or db, got {override!r}")
        return (override, "CAPABILITIES_STORE_MODE")
    identity_file = Path(envelope) / "project.json"
    try:
        identity = json.loads(identity_file.read_text())
    except (OSError, ValueError):
        return ("files", "no project identity")
    mode = identity.get("store", "files")
    if mode not in ("files", "db"):
        raise StoreError("bad_store_mode", f"{identity_file} declares store {mode!r}",
                         'expected "files" or "db"')
    return (mode, str(identity_file))


def open_records(envelope: Path | str, global_dir: Path | str,
                 url: str | None = None, project_only: bool = False) -> Records:
    """The adapter this project's configuration is read and written through."""
    envelope, global_dir = Path(envelope), Path(global_dir)
    mode, source = records_mode(envelope)
    identity = {}
    try:
        identity = json.loads((envelope / "project.json").read_text())
    except (OSError, ValueError):
        pass
    slug, project_id = identity.get("slug"), identity.get("id")
    if mode == "files":
        adapter = FileRecords(envelope, global_dir, project_id, slug,
                              include_global=not project_only)
        adapter.source = source if source == "CAPABILITIES_STORE_MODE" else str(envelope)
        return adapter
    if not slug:
        raise StoreError("no_project_identity",
                         f"{envelope / 'project.json'} keeps this project's records "
                         "in the store but declares no slug to read them under",
                         "run `capabilities init` in the project")
    store = open_store(url)
    return StoreRecords(store, Scopes(project=slug, include_global=not project_only),
                        ("project", slug))

# <<< contract: store <<<
