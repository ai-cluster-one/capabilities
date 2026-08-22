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
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

# >>> contract: store (generated — edit contract/store.py, run `capabilities sync-contract`) >>>

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

    def __init__(self, project: str | None = None):
        self.project = project or None

    @classmethod
    def from_env(cls, project: str | None = None) -> "Scopes":
        return cls(project=project or os.environ.get("CAPABILITIES_PROJECT") or None)

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
        return f"Scopes(project={self.project!r})"



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

def open_store(url: str | None = None) -> "Store":
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
