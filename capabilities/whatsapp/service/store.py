"""Canonical store tier — the one true copy of the records layer.

This is the required records tier alongside `capability core` and the optional
`connections` tier in `contract/preamble.py`, and it obeys the same law: the
helpers between the fence markers are COPIED into each `bin/<name>`, never
imported, so a manager update can never break a deployed capability (SHEBANG.md
"spec, never a shared runtime library"). Every capability carries it because the
core identifiers and policy surface resolve through the records adapter. A
bundled service carries the same body whole as `service/store.py`.

WHAT THIS TIER OWNS
===================
The records a *running* capability reads and writes: connections, grants,
identifiers, settings, the policy gate and documents. They are kept in files -
the project's envelope, then the user's config home - and nowhere else. This
tier decides nothing about a database: runtime state a capability keeps in
PostgreSQL is reached through the shared database library,
`capabilities_contract.db`, which alone resolves which database a project uses.

THREE RESOLUTION SEMANTICS, ALREADY IN THE DOCTRINE
===================================================
The precedence is always project over global, but *how* the scopes combine is
declared per collection:

  - MERGE  — per key; a key absent at the higher scope inherits the lower.
    This is rule 17 for the gate ("an absent project entry inherits the global
    entry"). Connections take it too, at the entry level: rule 18 keeps an
    identity atomic — taken whole from one scope, never assembled out of two —
    while letting a project add to the set it inherits rather than replace it.

  - FIRST  — the highest scope holding ANY entry wins, whole.

  - EXACT  — no cascade; a record belongs to exactly one scope and is read
    there (rule 16).

ONE WRITER PER COLLECTION
=========================
Rule 15 is addressed by `(capability, collection)` instead of by path: the
manager alone writes `policy`, a capability alone writes its own `identifier`
records, and `connection`, `grant`, `setting` and `document` records are
human-written through a CLI verb.

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
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# >>> contract: store (generated — edit contract/store.py, run `capabilities sync-contract`) >>>
# A region carries what it needs. Relying on the host file to have imported
# the right names is a coupling nothing checks and nothing reports: it works in
# whichever capability happened to import them and fails at import time in the
# rest. Re-importing a name the host already has costs nothing.
import json
import os
import hashlib
import re
# The capability core region reads the clock through `time` and imports nothing
# of its own, so this region keeps carrying it.
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


# Two scopes and no more: a record either belongs to one project (the
# project's envelope) or to every project (the user's config home).
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
# that needs write access writes one grant and repeats no address, no host,
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
    "document": {"resolve": MERGE, "writer": "human"},
}

# Grant fields and what they mean when nothing declares them. `allow_write`
# falls back to the capability's own WRITE_DEFAULT rather than to a value here,
# because "does a write leave the system" is the capability's fact.
GRANT_FIELDS = ("enabled", "allow_write")

# A document version is named by the first twelve hex characters of its sha256.
# Forty-eight bits is far more than a project's worth of prose needs, and a name
# a person can read at a glance is worth more than headroom nobody will reach.
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


# The process-wide read-only switch. A caller hands a whole process tree to
# something that may read through the capabilities but change nothing through
# them by setting this one variable, which every descendant inherits. It is
# decided here and nowhere else, because this tier is the one every capability
# carries and the one the manager and the bundled services import. It only ever
# closes: nothing it says makes a connection writable, and unset, `0` or
# `false` leave every record and every gate exactly as declared.
READ_ONLY_ENV = "CAPABILITIES_READ_ONLY"
READ_ONLY_EFFECT = ("every connection resolves read-only whatever its grant or "
                    "WRITE_DEFAULT; write verbs, project-record writes and "
                    "service activation exit 4")


def read_only_switch() -> bool:
    """True when this process runs under the read-only switch: the variable
    holds `1` or `true`, in any case."""
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in ("1", "true")


def _refuse_record_write(capability: str, collection: str) -> None:
    """A project record is the human's or the manager's, so under the switch
    nothing writes one. Operational state (a capability's state directory and
    its tables in the database) is not a record and keeps working."""
    if read_only_switch():
        raise StoreError(
            "read_only_switch",
            f"writing {capability} {collection} records is refused: "
            f"{READ_ONLY_ENV} is set, so this process changes no project record",
            "Do not lift the switch yourself — ask the user; the change has to "
            f"run in a process without {READ_ONLY_ENV}.")


def _store_check(name: str, value: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise StoreError("bad_name", f"{name} {value!r} must match {pattern.pattern}")
    return value


def _semantics(collection: str) -> str:
    spec = COLLECTIONS.get(collection)
    if not spec:
        raise StoreError("bad_collection", f"unknown collection {collection!r}",
                         f"known: {', '.join(sorted(COLLECTIONS))}")
    return spec["resolve"]


def _grant_decisions(cid: str, rows: Sequence[dict[str, Any]]) -> tuple[dict, dict]:
    """One grant assembled field by field, highest scope first.

    This is the line COLLECTIONS already draws between the two records. An
    identity is a fact about the world, so it is atomic: taken whole from
    one scope, never assembled out of two. A grant is a decision, and a
    decision a lower scope made about one field stands until some higher
    scope decides that same field — so a machine-wide `allow_write: false`
    survives a project that only says `enabled: true`, and write permission
    is never acquired by saying something else.

    Returns the decided fields and, per field, the scope that decided it."""
    decided: dict[str, Any] = {}
    decided_at: dict[str, str] = {}
    for row in rows:
        value = row["value"]
        if not isinstance(value, dict):
            raise StoreError("bad_grant",
                             f"grant {cid!r} must be a table, got {type(value).__name__}")
        unknown = set(value) - set(GRANT_FIELDS)
        if unknown:
            raise StoreError("bad_grant",
                             f"grant {cid!r} has unknown field(s): {', '.join(sorted(unknown))}",
                             f"known: {', '.join(GRANT_FIELDS)}")
        for field, decision in value.items():
            if field not in decided:
                decided[field] = decision
                decided_at[field] = row["scope"]
    return decided, decided_at


# --- records: the files a project keeps its configuration in ------------------
#
# Runtime state is not on this axis: runs, queues and cursors a capability's
# processes share live in its tables in the database. What this answers is
# where CONFIGURATION is read and written. A capability calls the verbs below
# and never a path, so the layout is described in one place.

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
    """The surface a capability reads and writes its configuration through."""

    mode = "?"
    source = "?"

    def __enter__(self) -> "Records":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        pass

    def resolve_scope(self, capability: str, collection: str,
                      scope: str) -> dict[str, dict[str, Any]]:
        """Resolve exactly one scope, without inheritance."""
        raise NotImplementedError


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
                         f"collection {collection!r} is not kept in files")

    # -- reading ---------------------------------------------------------------

    def _entries(self, root: Path, capability: str, collection: str) -> dict[str, Any]:
        """Every entry of one collection held at one scope, keyed by the name a
        record answers to."""
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

    def config_layers(self, capability: str, collection: str,
                      _scopes: Any = None) -> list[dict[str, dict[str, Any]]]:
        """Each scope's own entries, highest scope first, uncollapsed."""
        _store_check("capability", capability, CAPABILITY_RE)
        _semantics(collection)
        layers: list[dict[str, dict[str, Any]]] = []
        for scope, pid, root in self._chain():
            entries = self._entries(root, capability, collection)
            if not entries:
                continue
            layers.append({
                key: {"id": f"{scope}:{capability}/{collection}/{key}", "scope": scope,
                      "project_id": pid, "capability": capability,
                      "collection": collection, "key": key, "value": value,
                      "note": self._note(root, capability, collection, key),
                      "updated_at": None, "updated_by": None}
                for key, value in entries.items()
            })
        return layers

    def resolve(self, capability: str, collection: str) -> dict[str, dict[str, Any]]:
        semantics = _semantics(collection)
        out: dict[str, dict[str, Any]] = {}
        for rows in self.config_layers(capability, collection):
            if semantics == FIRST:
                return rows
            for key, row in rows.items():
                prior = out.get(key)
                if prior is None:
                    out[key] = row
                elif (collection == "connection" and not prior["value"]
                        and row["value"]):
                    # One file holds both records here, so an entry carrying
                    # only grant fields yields an identity with no fields. That
                    # is a decision about an inherited connection, never a
                    # replacement for it: it must not blank the identity the
                    # lower scope declared. Where nothing is inherited the
                    # fieldless identity stands, which is the only reading a
                    # glued file leaves for it.
                    out[key] = row
        return out

    def _scope_root(self, scope: str) -> tuple[Path, str | None]:
        """The directory one named scope keeps its records in, and the project
        it answers for. Named once so asking a scope for its rows and asking it
        for its file can never land in different trees."""
        if scope == "project":
            return self.envelope, self.project_id
        if scope == "global":
            return self.global_dir, None
        raise StoreError("bad_scope",
                         f"scope {scope!r} must be one of {', '.join(SCOPE_KINDS)}")

    def resolve_scope(self, capability: str, collection: str,
                      scope: str) -> dict[str, dict[str, Any]]:
        _store_check("capability", capability, CAPABILITY_RE)
        root, pid = self._scope_root(scope)
        return {
            key: {"id": f"{scope}:{capability}/{collection}/{key}",
                  "scope": scope, "project_id": pid,
                  "capability": capability, "collection": collection,
                  "key": key, "value": value,
                  "note": self._note(root, capability, collection, key),
                  "updated_at": None, "updated_by": None}
            for key, value in self._entries(root, capability, collection).items()
        }

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

    def scope_source(self, capability: str, collection: str, scope: str) -> str:
        """The file one named scope keeps this collection in. What a record
        resolved *to* is a question about that record, so the caller that knows
        which scope answered asks here rather than for the collection's own
        file: one file per scope, and the chain is not walked again."""
        _store_check("capability", capability, CAPABILITY_RE)
        root, _pid = self._scope_root(scope)
        return str(self._file(root, capability, collection))

    def connections(self, capability: str, write_default: bool = False,
                    include_disabled: bool = False,
                    machine_read: bool = False) -> dict[str, dict[str, Any]]:
        """Every connection this project may use, already carrying the decision
        made about it — the one read a capability needs before it acts.

        `machine_read` is the caller's word that this is one of the
        capability's declared machine reads outside any project: an identity
        the machine itself declares is then usable, read-only, unless its grant
        resolves to `enabled: false`."""
        identities = self.resolve(capability, "connection")
        layers = self.config_layers(capability, "grant")
        # Under the read-only switch every connection is a source, whatever its
        # grant or the capability's WRITE_DEFAULT says.
        writable = not read_only_switch()
        out: dict[str, dict[str, Any]] = {}
        for cid, entry in identities.items():
            rows = [layer[cid] for layer in layers if cid in layer]
            decided, decided_at = _grant_decisions(cid, rows)
            # Where the identity resolves decides whether it is this project's
            # to use at all. A project that declares an identity locally has
            # already said yes by declaring it; an identity inherited from the
            # global scope is withheld until a grant *resolving at project
            # scope* says `enabled: true`. A global `enabled: true` can never be
            # the blessing — one global line would otherwise hand every project
            # on the machine the same connection back.
            declared = decided.get("enabled")
            identity_is_project = entry["scope"] == "project"
            blessed_here = decided_at.get("enabled") == "project"
            if declared is None:
                enabled = identity_is_project
            else:
                enabled = bool(declared) and (identity_is_project or blessed_here)
            # A machine read lends the machine's own connection and nothing
            # more: never a write, and never one a grant switched off.
            lent = (machine_read and not enabled and entry["scope"] == "global"
                    and (declared is None or bool(declared)))
            if lent:
                enabled = True
            if not enabled and not include_disabled:
                continue
            grant = rows[0] if rows else None
            out[cid] = {
                "id": cid,
                "value": entry["value"],
                "scope": (entry["scope"], entry["project_id"]),
                "enabled": enabled,
                "allow_write": (writable and not lent
                                and bool(decided.get("allow_write", write_default))),
                "grant_scope": (grant["scope"], grant["project_id"]) if grant else None,
            }
            if lent:
                out[cid]["machine_read"] = True
        return out

    # -- writing ---------------------------------------------------------------

    def set(self, capability: str, collection: str, key: str, value: Any,
            actor: str | None = None, note: str | None = None,
            scope: str | None = None) -> None:
        _store_check("capability", capability, CAPABILITY_RE)
        _store_check("key", key, KEY_RE)
        _semantics(collection)
        _refuse_record_write(capability, collection)
        target_scope = scope or "project"
        if target_scope not in SCOPE_KINDS:
            raise StoreError("bad_scope",
                             f"scope {target_scope!r} must be one of {', '.join(SCOPE_KINDS)}")
        root = self.envelope if target_scope == "project" else self.global_dir
        if target_scope == "project" and not self.envelope.is_dir():
            raise StoreError("no_envelope", "no capabilities/ envelope in this project",
                             "run `capabilities init` first")
        path = self._file(root, capability, collection, key)
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

    def delete(self, capability: str, collection: str, key: str,
               scope: str | None = None) -> bool:
        _refuse_record_write(capability, collection)
        target_scope = scope or "project"
        if target_scope not in SCOPE_KINDS:
            raise StoreError("bad_scope",
                             f"scope {target_scope!r} must be one of {', '.join(SCOPE_KINDS)}")
        root = self.envelope if target_scope == "project" else self.global_dir
        path = self._file(root, capability, collection, key)
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
        _refuse_record_write(capability, "document")
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
        raise StoreError("files_mode", f"{what} is not kept: records are files",
                         "their history is the repository's; read it with git")

    def revisions(self, *_a: Any, **_k: Any) -> list:
        self._refuse("a record's history")
        return []

    def document_versions(self, *_a: Any, **_k: Any) -> list:
        self._refuse("a document's earlier versions")
        return []


def open_records(envelope: Path | str, global_dir: Path | str,
                 project_only: bool = False) -> Records:
    """The adapter this project's configuration is read and written through:
    the project's envelope, then `global_dir` unless `project_only`.

    Records are files only. A `project.json` that declares a `store` other than
    `files` is refused rather than read as files, since the records it points
    at are not where it says."""
    envelope, global_dir = Path(envelope), Path(global_dir)
    identity_file = envelope / "project.json"
    try:
        identity = json.loads(identity_file.read_text())
    except (OSError, ValueError):
        identity = {}
    if not isinstance(identity, dict):
        identity = {}
    declared = identity.get("store", "files")
    if declared != "files":
        raise StoreError("bad_store_mode",
                         f"{identity_file} declares store {declared!r}; records are "
                         "kept only in files",
                         'remove "store" from project.json, or set it to "files"')
    return FileRecords(envelope, global_dir, identity.get("id"), identity.get("slug"),
                       include_global=not project_only)

# <<< contract: store <<<
