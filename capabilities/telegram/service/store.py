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
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

# >>> contract: store (generated — edit contract/store.py, run `capabilities sync-contract`) >>>

SCHEMA_VERSION = 3

SCOPE_KINDS = ("global", "project", "instance")
SCOPE_RANK = {"global": 1, "project": 2, "instance": 3}

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
    def from_env(cls, project: str | None = None) -> "Scopes":
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
    # Long text — a prompt, a reference, an automation's script — addressed by
    # the hash of its own content, so a version is immutable and identical text
    # is stored once. Versions accumulate; none of them is "current". Which one
    # is in force is a pin, and it lives in `config`.
    #
    # Saving is therefore not deploying: an edit adds a row here and changes
    # nothing that runs until the pin moves. That separation is the reason this
    # is safe to hold executable text at all.
    """
    CREATE TABLE IF NOT EXISTS documents (
        scope_kind  TEXT NOT NULL,
        scope_name  TEXT NOT NULL,
        capability  TEXT NOT NULL,
        key         TEXT NOT NULL,
        hash        TEXT NOT NULL,
        body        TEXT NOT NULL,
        media_type  TEXT,
        author      TEXT,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (scope_kind, scope_name, capability, key, hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS documents_history_idx
        ON documents (capability, key, created_at)
    """,
    # A project is addressed by a slug it declares for itself, never by the name
    # of the directory it happens to sit in: renaming a folder must not orphan
    # every row scoped to it.
    #
    # The slug is the key rows are scoped by, so they stay readable without a
    # join. What keeps it honest is `id`: a value the project generates once and
    # carries in its own config, so two unrelated repositories that both want to
    # be called "marvin" are told the label is taken instead of silently sharing
    # each other's rows. A label collision is loud and costs a rename; an
    # identity collision is silent and costs everything.
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          TEXT PRIMARY KEY,
        slug        TEXT NOT NULL UNIQUE,
        name        TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    # Where a project sits is not a fact about the project — it is a fact about
    # the project ON ONE MACHINE. The same slug lives at /Users/kz/dev/marvin on
    # a laptop and /opt/marvin on a server, so the path is keyed by both.
    """
    CREATE TABLE IF NOT EXISTS project_paths (
        slug        TEXT NOT NULL,
        instance    TEXT NOT NULL,
        path        TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (slug, instance)
    )
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

    # -- connections, with their grants applied --------------------------------

    def connections_effective(self, capability: str, scopes: Scopes,
                              write_default: bool = False,
                              include_disabled: bool = False) -> dict[str, dict[str, Any]]:
        """Every connection this project may use, already carrying the decision
        made about it — the one read a capability needs before it acts.

        `write_default` is the capability's own WRITE_DEFAULT, used where no
        grant declares writability, so the answer never changes shape depending
        on whether anyone bothered to write a grant."""
        identities = self.config_resolve(capability, "connection", scopes)
        grants = self.config_resolve(capability, "grant", scopes)
        out: dict[str, dict[str, Any]] = {}
        for cid, entry in identities.items():
            grant = grants.get(cid)
            decided = grant["value"] if grant else {}
            if not isinstance(decided, dict):
                raise StoreError("bad_grant", f"grant {cid!r} must be a table, got {type(decided).__name__}")
            unknown = set(decided) - set(GRANT_FIELDS)
            if unknown:
                raise StoreError("bad_grant", f"grant {cid!r} has unknown field(s): {', '.join(sorted(unknown))}",
                                 f"known: {', '.join(GRANT_FIELDS)}")
            enabled = bool(decided.get("enabled", True))
            if not enabled and not include_disabled:
                continue
            out[cid] = {
                "id": cid,
                "value": entry["value"],
                "scope": (entry["scope_kind"], entry["scope_name"]),
                "enabled": enabled,
                "allow_write": bool(decided.get("allow_write", write_default)),
                "grant_scope": (grant["scope_kind"], grant["scope_name"]) if grant else None,
            }
        return out

    def grant_orphans(self, capability: str, scopes: Scopes) -> list[dict[str, Any]]:
        """Grants naming a connection that does not resolve.

        A grant is the only record that refers to another, so it is the only one
        that can be aimed at nothing. Dropping it quietly is the failure this
        whole move exists to stop: a mistyped id would mean permission silently
        not granted, which looks exactly like permission correctly withheld."""
        identities = self.config_resolve(capability, "connection", scopes)
        grants = self.config_resolve(capability, "grant", scopes)
        return [{"key": key, "scope": (row["scope_kind"], row["scope_name"]),
                 "value": row["value"]}
                for key, row in sorted(grants.items()) if key not in identities]

    # -- documents: long text, versioned by content, one version in force ------

    def document_put(self, capability: str, key: str, body: str,
                     scope: tuple[str, str], author: str | None = None,
                     media_type: str | None = None) -> str:
        """Record a version at one scope and return its hash. Saving is not
        deploying: this changes nothing that runs until `document_pin` names it.

        A version belongs to the scope that wrote it, like every other record.
        Without that, every project's drafts of `telegram/voice-agent` would
        pool under one key and each would be reading the others' history."""
        _check("capability", capability, CAPABILITY_RE)
        _check("key", key, KEY_RE)
        kind, name = _check_scope(*scope)
        if not isinstance(body, str):
            raise StoreError("bad_document", "a document body must be text")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:HASH_LENGTH]
        with self.transaction():
            cur = self._execute(
                "SELECT body FROM documents WHERE scope_kind = ? AND scope_name = ? "
                "AND capability = ? AND key = ? AND hash = ?",
                (kind, name, capability, key, digest))
            existing = cur.fetchone()
            if existing and existing[0] != body:
                raise StoreError(
                    "hash_collision",
                    f"version {digest} of {capability}/{key} already names different text",
                    "this is what the truncated hash is checked for; report it")
            if not existing:
                self._execute(
                    "INSERT INTO documents (scope_kind, scope_name, capability, key, hash, "
                    "body, media_type, author, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind, name, capability, key, digest, body, media_type, author, _now()),
                )
        return digest

    def document_pin(self, capability: str, key: str, digest: str,
                     scope: tuple[str, str], actor: str | None = None,
                     scopes: Scopes | None = None) -> None:
        """Put one version in force at one scope — the deploy step, recorded in
        `revisions` like any other decision. A hash nobody recorded is refused
        rather than pinned: a pin aimed at nothing is the same silent failure a
        grant aimed at nothing was.

        The version is looked for down the whole chain, so a project may put a
        globally declared version in force without copying it."""
        chain = (scopes or Scopes(project=scope[1] if scope[0] == "project" else None,
                                  instance=scope[1] if scope[0] == "instance" else None))
        if not self._document_body(capability, key, digest, chain):
            raise StoreError("unknown_version",
                             f"no recorded version {digest} of {capability}/{key}",
                             "record it with document_put first")
        self.config_set(capability, "document", key, {"hash": digest}, scope, actor=actor)

    def _document_body(self, capability: str, key: str, digest: str,
                       scopes: Scopes) -> tuple | None:
        """The version's row, found at the highest scope in the chain holding it."""
        for kind, name in scopes.chain():
            cur = self._execute(
                "SELECT body, media_type, author, created_at, scope_kind, scope_name "
                "FROM documents WHERE scope_kind = ? AND scope_name = ? "
                "AND capability = ? AND key = ? AND hash = ?",
                (kind, name, capability, key, digest))
            row = cur.fetchone()
            if row:
                return row
        return None

    def document_read(self, capability: str, key: str,
                      scopes: Scopes) -> dict[str, Any] | None:
        """The version in force here, or None where nothing is pinned."""
        pin = self.config_get(capability, "document", key, scopes)
        if not pin:
            return None
        digest = pin.get("hash") if isinstance(pin, dict) else None
        if not digest:
            raise StoreError("bad_pin", f"the pin for {capability}/{key} names no hash")
        row = self._document_body(capability, key, digest, scopes)
        if not row:
            raise StoreError(
                "missing_version",
                f"{capability}/{key} is pinned to version {digest}, which is "
                "not recorded in this project's scopes",
                "pin a version that exists, or record this one")
        return {"key": key, "hash": digest, "body": row[0], "media_type": row[1],
                "author": row[2], "created_at": row[3],
                "version_scope": (row[4], row[5]),
                "scope": self.config_origin(capability, "document", key, scopes)}

    def document_versions(self, capability: str, key: str,
                          scopes: Scopes) -> list[dict[str, Any]]:
        """Every version this project can see, newest first — its own chain and
        nothing else, so one project's drafts stay out of another's history.
        Bodies are omitted; a listing should not carry the whole of what it
        lists."""
        chain = scopes.chain()
        clause = " OR ".join(["(scope_kind = ? AND scope_name = ?)"] * len(chain))
        params: list[Any] = [capability, key]
        for kind, name in chain:
            params += [kind, name]
        cur = self._execute(
            "SELECT hash, media_type, author, created_at, length(body), scope_kind, "
            f"scope_name FROM documents WHERE capability = ? AND key = ? AND ({clause}) "
            "ORDER BY created_at DESC, hash", params)
        return [{"hash": r[0], "media_type": r[1], "author": r[2], "created_at": r[3],
                 "bytes": r[4], "scope": (r[5], r[6])} for r in cur.fetchall()]

    def document_keys(self, capability: str, scopes: Scopes) -> list[str]:
        chain = scopes.chain()
        clause = " OR ".join(["(scope_kind = ? AND scope_name = ?)"] * len(chain))
        params: list[Any] = [capability]
        for kind, name in chain:
            params += [kind, name]
        cur = self._execute(
            f"SELECT DISTINCT key FROM documents WHERE capability = ? AND ({clause}) "
            "ORDER BY key", params)
        return [r[0] for r in cur.fetchall()]

    # -- the project registry --------------------------------------------------

    def project_register(self, project_id: str, slug: str, name: str | None = None) -> None:
        """Claim a slug for a project id. Re-registering the same id is how a
        second machine joins a project it already belongs to — the id travels in
        the repository, so a laptop and a server agree without being told.

        A slug already held by a DIFFERENT id is refused: that is another
        project, and taking its label would take its rows."""
        _check("slug", slug, CAPABILITY_RE)
        if not isinstance(project_id, str) or not project_id.strip():
            raise StoreError("bad_project_id", "a project id is required",
                             "generate one once and keep it in the project's own config")
        with self.transaction():
            holder = self._execute(
                "SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
            if holder and holder[0] != project_id:
                raise StoreError(
                    "slug_taken", f"the label {slug!r} belongs to project {holder[0]}",
                    "pick another label; this is a different project, and sharing "
                    "the label would share its rows",
                )
            existing = self._execute(
                "SELECT slug FROM projects WHERE id = ?", (project_id,)).fetchone()
            if existing and existing[0] != slug:
                raise StoreError(
                    "slug_immutable",
                    f"project {project_id} is already labelled {existing[0]!r}",
                    "rows are scoped by the label, so renaming one is a migration, "
                    "not an edit",
                )
            if existing:
                self._execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
            else:
                self._execute(
                    "INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                    (project_id, slug, name, _now()),
                )

    def project_get(self, slug: str) -> dict[str, Any] | None:
        cur = self._execute(
            "SELECT id, slug, name, created_at FROM projects WHERE slug = ?", (slug,))
        row = cur.fetchone()
        return {"id": row[0], "slug": row[1], "name": row[2], "created_at": row[3]} if row else None

    def project_by_id(self, project_id: str) -> dict[str, Any] | None:
        cur = self._execute(
            "SELECT id, slug, name, created_at FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        return {"id": row[0], "slug": row[1], "name": row[2], "created_at": row[3]} if row else None

    def project_list(self) -> list[dict[str, Any]]:
        cur = self._execute("SELECT id, slug, name, created_at FROM projects ORDER BY slug")
        return [{"id": r[0], "slug": r[1], "name": r[2], "created_at": r[3]}
                for r in cur.fetchall()]

    def project_bind_path(self, slug: str, instance: str, path: str) -> None:
        """Record where this project sits on this machine. Binding an unknown
        slug is refused: a path is a fact about a project, not a way to invent
        one."""
        if not self.project_get(slug):
            raise StoreError("unknown_project", f"no project registered as {slug!r}",
                             "register it first")
        with self.transaction():
            self._execute(
                "INSERT INTO project_paths (slug, instance, path, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (slug, instance) DO UPDATE "
                "SET path = EXCLUDED.path, updated_at = EXCLUDED.updated_at",
                (slug, instance, path, _now()),
            )

    def project_path(self, slug: str, instance: str) -> str | None:
        cur = self._execute(
            "SELECT path FROM project_paths WHERE slug = ? AND instance = ?", (slug, instance))
        row = cur.fetchone()
        return row[0] if row else None

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
