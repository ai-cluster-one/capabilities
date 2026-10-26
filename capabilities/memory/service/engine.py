"""The memory engine: its tables, the store it keeps them in, the block it
renders, the grooming pass, the folder reader `adopt` uses, and the service loop.

The CLI owns everything a project is - its identity, its policy, its settings,
where its envelope sits - and hands this module the answers. This module owns
what is done with them: entries and their trail in the project's store, the
context block rendered from them, a grooming pass checked and applied, and one
process re-rendering every project it serves when the store says something
changed.

Project mode is the case of a set of one: `memory service run` serves the
project it is started in, `memory service run --machine` every project on the
machine's opt-in list, through the same code.

Runtime state lives in the PostgreSQL the project resolves to, under the
`memory_` prefix and the `memory` migration ledger: `memory_entries`, one row
per entry, whose text never changes once written, and `memory_events`, the
append-only trail of every change, which raises the project's version of the
`memory_changed` channel in the store's change versions as it commits.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

# ── the tables ───────────────────────────────────────────────────────────────

LEDGER_OWNER = "memory"
LEDGER_MAJOR = 1
LEDGER_MINOR = 1
CHANNEL = "memory_changed"

LEDGER_STEPS = [
    ("0001-entries", """
    CREATE TABLE memory_entries (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        seq            bigserial NOT NULL UNIQUE,
        project_id     text NOT NULL CHECK (project_id <> ''),
        scope          text CHECK (scope <> ''),
        title          text NOT NULL CHECK (title <> '' AND strpos(title, E'\\n') = 0),
        body           text NOT NULL CHECK (body <> ''),
        pinned         boolean NOT NULL DEFAULT false,
        status         text NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'superseded', 'dropped')),
        superseded_by  uuid,
        destination    text,
        created_at     timestamptz NOT NULL DEFAULT now(),
        created_by     text NOT NULL,
        node           text NOT NULL,
        origin         text NOT NULL CHECK (origin IN ('agent', 'groom')),
        body_hash      text NOT NULL,
        CHECK ((status = 'superseded') = (superseded_by IS NOT NULL))
    )
    """),
    ("0002-entries-indexes", """
    CREATE INDEX memory_entries_project ON memory_entries (project_id, status, created_at DESC);
    CREATE INDEX memory_entries_search ON memory_entries
        USING gin (to_tsvector('simple', title || ' ' || body));
    """),
    ("0003-events", """
    CREATE TABLE memory_events (
        seq       bigserial PRIMARY KEY,
        project_id text NOT NULL CHECK (project_id <> ''),
        entry_id  uuid,
        kind      text NOT NULL CHECK (kind IN ('add', 'supersede', 'drop', 'reactivate',
                                                'pin', 'unpin', 'purge', 'groom',
                                                'groom_undo', 'adopt')),
        actor     text NOT NULL,
        node      text NOT NULL,
        at        timestamptz NOT NULL DEFAULT now(),
        reason    text,
        pass_id   uuid,
        detail    jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX memory_events_project ON memory_events (project_id, seq DESC);
    CREATE INDEX memory_events_entry ON memory_events (entry_id);
    CREATE INDEX memory_events_pass ON memory_events (pass_id) WHERE pass_id IS NOT NULL;
    CREATE UNIQUE INDEX memory_events_adopt_key ON memory_events
        (project_id, (detail->>'adopt_key')) WHERE kind = 'add' AND detail ? 'adopt_key';
    """),
    ("0004-guards", """
    CREATE FUNCTION memory_entries_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF tg_op = 'DELETE' THEN
            IF current_setting('memory.purge', true) IS DISTINCT FROM old.id::text THEN
                RAISE EXCEPTION 'memory entries are never deleted: `memory drop --purge` is the one hard delete'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN old;
        END IF;
        IF new.id <> old.id OR new.seq <> old.seq OR new.project_id <> old.project_id
                OR new.scope IS DISTINCT FROM old.scope OR new.title <> old.title
                OR new.body <> old.body OR new.created_at <> old.created_at
                OR new.created_by <> old.created_by OR new.node <> old.node
                OR new.origin <> old.origin OR new.body_hash <> old.body_hash THEN
            RAISE EXCEPTION 'a memory entry''s text is immutable: supersede it with a new entry'
                USING ERRCODE = 'check_violation';
        END IF;
        IF new.status <> old.status AND NOT (
                (old.status = 'active' AND new.status IN ('superseded', 'dropped'))
                OR (old.status IN ('superseded', 'dropped') AND new.status = 'active')) THEN
            RAISE EXCEPTION 'a memory entry does not move from % to %', old.status, new.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN new;
    END $$;
    CREATE TRIGGER memory_entries_guard BEFORE UPDATE OR DELETE ON memory_entries
        FOR EACH ROW EXECUTE FUNCTION memory_entries_guard();

    CREATE FUNCTION memory_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'the memory trail is append-only' USING ERRCODE = 'check_violation';
    END $$;
    CREATE TRIGGER memory_events_guard BEFORE UPDATE OR DELETE ON memory_events
        FOR EACH ROW EXECUTE FUNCTION memory_events_guard();
    CREATE TRIGGER memory_events_truncate BEFORE TRUNCATE ON memory_events
        FOR EACH STATEMENT EXECUTE FUNCTION memory_events_guard();

    CREATE FUNCTION memory_notify() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_notify('memory_changed', json_build_object(
            'schema', tg_table_schema, 'project', new.project_id, 'kind', new.kind,
            'entry', new.entry_id, 'event', new.seq)::text);
        RETURN NULL;
    END $$;
    CREATE TRIGGER memory_notify AFTER INSERT ON memory_events
        FOR EACH ROW EXECUTE FUNCTION memory_notify();
    """),
    # Every event raises its project's version of `memory_changed` in the
    # store's change versions, in the writer's own transaction, so an event any
    # release writes moves the version the service follows. The notification
    # stays beside it for a service of an earlier release that still listens.
    ("0005-changed-versions", """
    CREATE OR REPLACE FUNCTION memory_notify() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_notify('memory_changed', json_build_object(
            'schema', tg_table_schema, 'project', new.project_id, 'kind', new.kind,
            'entry', new.entry_id, 'event', new.seq)::text);
        IF to_regclass(format('%I.schema_channel', tg_table_schema)) IS NOT NULL THEN
            EXECUTE format('INSERT INTO %I.schema_channel AS c (channel, scope, version) '
                           'VALUES (%L, $1, 1) ON CONFLICT (channel, scope) '
                           'DO UPDATE SET version = c.version + 1',
                           tg_table_schema, 'memory_changed')
                USING new.project_id;
        END IF;
        RETURN NULL;
    END $$;
    """),
]

STATUSES = ("active", "superseded", "dropped")
WRITE_KINDS = ("add", "supersede", "drop", "pin", "unpin", "purge")
TITLE_MAX = 200
BODY_MAX = 20000


# ── settings ─────────────────────────────────────────────────────────────────

# How much the window may hold before a pass is due, and when a pass runs,
# project over global, through the records adapter's `setting` collection.
SETTINGS_DEFAULTS: dict[str, Any] = {
    "window_days": 30,
    "min_entries": 5,
    "max_entries": 30,
    "block_chars": 12000,
    "entry_chars": 1200,
    "consolidate": True,
    "quiet_minutes": 30,
    "groom_interval_hours": 24,
    "groom_profile": "claude-read",
    "groom_budget_usd": 1.0,
    "groom_max_turns": 4,
    "groom_timeout_seconds": 900,
}
SETTINGS_BOUNDS: dict[str, tuple] = {
    "window_days": (1, 3650),
    "min_entries": (0, 200),
    "max_entries": (1, 500),
    "block_chars": (2000, 16000),
    "entry_chars": (200, 8000),
    "quiet_minutes": (0, 1440),
    "groom_interval_hours": (1, 720),
    "groom_budget_usd": (0.01, 50.0),
    "groom_max_turns": (1, 50),
    "groom_timeout_seconds": (60, 7200),
}
SETTINGS_KEYS = ("version", *SETTINGS_DEFAULTS)
# The ceiling the manifest declares for the rendered block, in UTF-8 bytes; the
# block carries no entry and stays far below it.
CONTRIBUTION_MAX_BYTES = 32768


class Refusal(Exception):
    """A refusal the CLI turns into its error envelope: exit code, stable code,
    message, and the one command that resolves it."""

    def __init__(self, exit_code: int, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.exit_code, self.code, self.message, self.hint = exit_code, code, message, hint


def read_settings(raw: dict, where: str) -> dict:
    """The settings in force: the defaults, with every key `raw` declares
    checked against its type and bounds. Anything wrong is refused whole."""
    def wrong(sentence: str) -> Refusal:
        return Refusal(6, "settings_invalid", f"{where}: {sentence}",
                       "fix the value, or remove the key to take its default; "
                       "`memory help` SETTINGS states each key and its bounds")

    out = dict(SETTINGS_DEFAULTS)
    unknown = sorted(set(raw) - set(SETTINGS_KEYS))
    if unknown:
        raise wrong(f"{', '.join(map(repr, unknown))} is no setting memory reads")
    if "version" in raw and (raw["version"] != 1 or isinstance(raw["version"], bool)):
        raise wrong("`version` is 1")
    for key, value in raw.items():
        if key == "version":
            continue
        default = SETTINGS_DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise wrong(f"`{key}` is true or false")
        elif isinstance(default, str):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                                                               value):
                raise wrong(f"`{key}` is a profile name")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise wrong(f"`{key}` is a number")
            low, high = SETTINGS_BOUNDS[key]
            if not low <= value <= high:
                raise wrong(f"`{key}` is between {low} and {high}")
            value = float(value)
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise wrong(f"`{key}` is a whole number")
            low, high = SETTINGS_BOUNDS[key]
            if not low <= value <= high:
                raise wrong(f"`{key}` is between {low} and {high}")
        out[key] = value
    if out["max_entries"] < out["min_entries"]:
        raise wrong("`max_entries` is at least `min_entries`")
    if out["entry_chars"] * 2 > out["block_chars"]:
        raise wrong("`entry_chars` is at most half of `block_chars`")
    return out


def fingerprint_of(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(hashlib.sha256(part).digest())
    return digest.hexdigest()[:12]


def settings_fingerprint(settings: dict) -> str:
    return fingerprint_of(json.dumps(settings, sort_keys=True).encode())


# ── values ───────────────────────────────────────────────────────────────────

def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(moment) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, _dt.datetime):
        return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(moment)


def day(moment) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d")


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def short(entry_id) -> str:
    return str(entry_id).replace("-", "")[:8]


def check_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        raise Refusal(6, "input", "an entry needs a title: one line, the line the index shows",
                      "pass --title TEXT")
    if "\n" in title or "\r" in title:
        raise Refusal(6, "input", "a title is one line", "move the rest into the text")
    if len(title) > TITLE_MAX:
        raise Refusal(6, "input", f"a title is at most {TITLE_MAX} characters "
                      f"(this one is {len(title)})", "move the rest into the text")
    return title


def check_body(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise Refusal(6, "input", "an entry needs text", "pass the text, or - to read stdin")
    if len(body) > BODY_MAX:
        raise Refusal(6, "input", f"an entry's text is at most {BODY_MAX} characters "
                      f"(this one is {len(body)})",
                      "memory holds conclusions; keep the evidence where it lives and "
                      "name it in the entry")
    return body


REF_RE = re.compile(r"[0-9a-fA-F-]{6,36}")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ── the store ────────────────────────────────────────────────────────────────

class StoreUnavailable(Exception):
    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug, self.message, self.hint = slug, message, hint


def database():
    try:
        from capabilities_contract import db
    except ImportError as exc:  # pragma: no cover - the header pins it
        raise StoreUnavailable("driver_missing", "the shared database library is not installed",
                               "run memory through its own script header, which pins it") from exc
    return db


def resolve_store(root: Path | None):
    """The store setting in force for `root`, as the shared library resolves it."""
    db = database()
    try:
        return db.resolve_setting(root)
    except db.DbError as exc:
        raise StoreUnavailable(exc.slug, exc.message, exc.hint) from None


def store_label(setting) -> str:
    """Where the store is named and which schema, with nothing secret in it."""
    return f"{setting.level} store setting, schema {setting.schema}"


ENTRY_COLUMNS = ("id, seq, project_id, scope, title, body, pinned, status, superseded_by, "
                 "destination, created_at, created_by, node, origin, body_hash")


def entry_out(row: dict, *, with_body: bool = True) -> dict:
    out = {"id": str(row["id"]), "short": short(row["id"]), "seq": row["seq"],
           "project_id": row["project_id"], "scope": row["scope"], "title": row["title"],
           "pinned": row["pinned"], "status": row["status"],
           "superseded_by": str(row["superseded_by"]) if row["superseded_by"] else None,
           "destination": row["destination"], "created_at": iso(row["created_at"]),
           "created_by": row["created_by"], "node": row["node"], "origin": row["origin"]}
    if with_body:
        out["body"] = row["body"]
        out["body_hash"] = row["body_hash"]
    else:
        out["chars"] = len(row["body"])
    if row.get("merged") is not None:
        out["merged"] = row["merged"]
    if row.get("rank") is not None:
        out["rank"] = round(float(row["rank"]), 4)
    return out


def event_out(row: dict) -> dict:
    out = {"seq": row["seq"], "kind": row["kind"],
           "entry_id": str(row["entry_id"]) if row["entry_id"] else None,
           "actor": row["actor"], "node": row["node"], "at": iso(row["at"]),
           "reason": row["reason"],
           "pass_id": str(row["pass_id"]) if row["pass_id"] else None,
           "detail": row["detail"] or {}}
    if "title" in row:
        out["title"] = row["title"]
    return out


class Store:
    """One connection to the store a project resolves, bound to its schema.
    Every write runs in one transaction, serialised per project, and leaves
    an event; each event raises the project's version of `memory_changed`,
    committed with it.

    It is a transaction connection, at the store's transaction address where
    one is named and carrying no session state, unless `session` asks for a
    session connection: a grooming pass does, for the lock it holds across its
    transactions."""

    def __init__(self, setting, application_name: str = "memory", session: bool = False):
        self.setting = setting
        self.application_name = application_name
        self.session = session
        self.conn = None

    @property
    def schema(self) -> str:
        return self.setting.schema

    def open(self, migrate: bool = True) -> "Store":
        db = database()
        try:
            self.conn = db.connect(application_name=self.application_name,
                                   setting=self.setting, transaction=not self.session)
            if migrate:
                db.migrate(self.conn, LEDGER_OWNER, LEDGER_STEPS, major=LEDGER_MAJOR,
                           minor=LEDGER_MINOR)
            self.conn.autocommit = True
        except db.DbError as exc:
            self.close()
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from None
        return self

    def close(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.close()
        self.conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def cursor(self):
        from psycopg.rows import dict_row
        return self.conn.cursor(row_factory=dict_row)

    def rows(self, sql: str, params=()) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def one(self, sql: str, params=()) -> dict | None:
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    @contextlib.contextmanager
    def writing(self, project: str):
        """One write transaction, serialised with every other write to the
        project's memory on any machine."""
        with self.conn.transaction():
            with self.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('memory write'), "
                            "hashtext(%s))", (project,))
                yield cur

    # -- writing ---------------------------------------------------------------

    @staticmethod
    def _insert(cur, project: str, title: str, body: str, *, scope: str | None, pinned: bool,
                origin: str, actor: str, node: str, created_at=None) -> dict:
        cur.execute(
            f"INSERT INTO memory_entries (project_id, scope, title, body, pinned, origin, "
            f"created_by, node, body_hash, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now())) "
            f"RETURNING {ENTRY_COLUMNS}",
            (project, scope, title, body, pinned, origin, actor, node, body_hash(body),
             created_at))
        return cur.fetchone()

    @staticmethod
    def _event(cur, project: str, kind: str, *, actor: str, node: str, entry_id=None,
               reason: str | None = None, pass_id=None, detail: dict | None = None) -> int:
        from psycopg.types.json import Jsonb
        cur.execute("INSERT INTO memory_events (project_id, entry_id, kind, actor, node, "
                    "reason, pass_id, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING seq",
                    (project, entry_id, kind, actor, node, reason, pass_id,
                     Jsonb(detail or {})))
        return cur.fetchone()["seq"]

    @staticmethod
    def _locked(cur, entry_id) -> dict | None:
        cur.execute(f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE id = %s FOR UPDATE",
                    (entry_id,))
        return cur.fetchone()

    def add(self, project: str, title: str, body: str, *, scope: str | None, pinned: bool,
            actor: str, node: str) -> tuple[dict, bool]:
        """A new entry, or the active one already holding this title and text in
        this scope: repeating an add records nothing twice."""
        with self.writing(project) as cur:
            cur.execute(f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE project_id = %s "
                        "AND status = 'active' AND title = %s AND body_hash = %s "
                        "AND scope IS NOT DISTINCT FROM %s", (project, title, body_hash(body),
                                                               scope))
            found = cur.fetchone()
            if found:
                return found, False
            row = self._insert(cur, project, title, body, scope=scope, pinned=pinned,
                               origin="agent", actor=actor, node=node)
            self._event(cur, project, "add", actor=actor, node=node, entry_id=row["id"],
                        detail={"pinned": pinned, "scope": scope, "origin": "agent"})
            return row, True

    def resolve(self, project: str | None, ref: str) -> dict:
        """The entry `ref` names: a whole id anywhere, or an id prefix of six or
        more characters among `project`'s entries."""
        ref = (ref or "").strip().lower()
        if not REF_RE.fullmatch(ref):
            raise Refusal(6, "input", f"{ref!r} is no entry id",
                          "an entry is named by its id or the first characters of it, as "
                          "`memory list` shows them")
        if UUID_RE.fullmatch(ref):
            row = self.one(f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE id = %s", (ref,))
            if row is None:
                raise Refusal(3, "not_found", f"no memory entry {ref}",
                              "`memory list --status all` lists this project's entries")
            return row
        prefix = ref.replace("-", "")
        found = self.rows(f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE project_id = %s "
                          "AND replace(id::text, '-', '') LIKE %s ORDER BY seq LIMIT 3",
                          (project, prefix + "%"))
        if not found:
            raise Refusal(3, "not_found", f"no memory entry of this project starts with {ref}",
                          "`memory list --status all` lists this project's entries")
        if len(found) > 1:
            raise Refusal(6, "ambiguous_id", f"{ref} names more than one entry: "
                          + ", ".join(short(r["id"]) + "…" for r in found),
                          "give more characters of the id")
        return found[0]

    @staticmethod
    def _own(row: dict, project: str, action: str) -> None:
        if row["project_id"] != project:
            raise Refusal(4, "foreign_project",
                          f"entry {short(row['id'])} belongs to project {row['project_id']}, "
                          f"and {action} writes only this project's memory",
                          "run it from the project that owns the entry; reading it from "
                          "here stays open")

    def supersede(self, project: str, refs: list[str], news: list[tuple[str, str]], *,
                  reason: str | None, pin: bool, actor: str, node: str) -> dict:
        """New entries that replace `refs`: each replaced entry points at the first
        new one, and a replaced pin carries over to the new entries."""
        with self.writing(project) as cur:
            olds = []
            for ref in refs:
                row = self.resolve(project, ref)
                self._own(row, project, "supersede")
                row = self._locked(cur, row["id"])
                if row["status"] != "active":
                    raise Refusal(6, "not_active", f"entry {short(row['id'])} is "
                                  f"{row['status']}; only an active entry is superseded",
                                  f"`memory show {short(row['id'])}` shows what replaced it")
                if any(o["id"] == row["id"] for o in olds):
                    continue
                olds.append(row)
            pinned = pin or any(o["pinned"] for o in olds)
            scope = olds[0]["scope"] if len({o["scope"] for o in olds}) == 1 else None
            made = []
            for title, body in news:
                row = self._insert(cur, project, title, body, scope=scope, pinned=pinned,
                                   origin="agent", actor=actor, node=node)
                self._event(cur, project, "add", actor=actor, node=node, entry_id=row["id"],
                            reason=reason, detail={"supersedes": [str(o["id"]) for o in olds],
                                                   "pinned": pinned, "origin": "agent"})
                made.append(row)
            replaced = []
            for old in olds:
                cur.execute("UPDATE memory_entries SET status = 'superseded', superseded_by = %s "
                            "WHERE id = %s RETURNING " + ENTRY_COLUMNS, (made[0]["id"], old["id"]))
                replaced.append(cur.fetchone())
                self._event(cur, project, "supersede", actor=actor, node=node,
                            entry_id=old["id"], reason=reason,
                            detail={"by": [str(m["id"]) for m in made]})
        return {"superseded": [entry_out(o, with_body=False) for o in replaced],
                "created": [entry_out(m) for m in made]}

    def drop(self, project: str, ref: str, *, reason: str, to: str | None, purge: bool,
             actor: str, node: str) -> dict:
        with self.writing(project) as cur:
            row = self.resolve(project, ref)
            self._own(row, project, "drop")
            if purge:
                if not UUID_RE.fullmatch((ref or "").strip()):
                    raise Refusal(6, "purge_needs_id",
                                  "--purge deletes the entry's text for good, so it takes the "
                                  "whole id, not a prefix",
                                  f"`memory drop {row['id']} --purge --reason ...`")
                self._locked(cur, row["id"])
                cur.execute("SELECT set_config('memory.purge', %s, true)", (str(row["id"]),))
                cur.execute("DELETE FROM memory_entries WHERE id = %s", (row["id"],))
                self._event(cur, project, "purge", actor=actor, node=node, entry_id=row["id"],
                            reason=reason, detail={"status_was": row["status"]})
                return {"purged": str(row["id"]), "short": short(row["id"]),
                        "note": "the entry's text is gone; its trail keeps who, when and why"}
            row = self._locked(cur, row["id"])
            if row["status"] != "active":
                raise Refusal(6, "not_active", f"entry {short(row['id'])} is already "
                              f"{row['status']}", "nothing to drop")
            cur.execute("UPDATE memory_entries SET status = 'dropped', destination = %s "
                        "WHERE id = %s RETURNING " + ENTRY_COLUMNS, (to, row["id"]))
            dropped = cur.fetchone()
            self._event(cur, project, "drop", actor=actor, node=node, entry_id=row["id"],
                        reason=reason, detail={"to": to} if to else {})
        return {"dropped": entry_out(dropped, with_body=False)}

    def set_pin(self, project: str, ref: str, pinned: bool, *, actor: str, node: str) -> dict:
        with self.writing(project) as cur:
            row = self.resolve(project, ref)
            self._own(row, project, "pin" if pinned else "unpin")
            row = self._locked(cur, row["id"])
            if row["status"] != "active":
                raise Refusal(6, "not_active", f"entry {short(row['id'])} is {row['status']}; "
                              "only an active entry is pinned or unpinned")
            if row["pinned"] == pinned:
                return {"changed": False, "entry": entry_out(row, with_body=False)}
            cur.execute("UPDATE memory_entries SET pinned = %s WHERE id = %s RETURNING "
                        + ENTRY_COLUMNS, (pinned, row["id"]))
            row = cur.fetchone()
            self._event(cur, project, "pin" if pinned else "unpin", actor=actor, node=node,
                        entry_id=row["id"])
        return {"changed": True, "entry": entry_out(row, with_body=False)}

    def adopt(self, project: str, title: str, body: str, *, created_at, key: str, source: str,
              pinned: bool, actor: str, node: str, memory_id: str | None = None
              ) -> tuple[dict | None, bool]:
        """One entry carried in from a memory folder, once per key per project
        whichever machine carries it."""
        with self.writing(project) as cur:
            cur.execute("SELECT entry_id FROM memory_events WHERE project_id = %s AND kind = 'add' "
                        "AND detail ? 'adopt_key' AND detail->>'adopt_key' = %s", (project, key))
            found = cur.fetchone()
            if found:
                return {"id": str(found["entry_id"])}, False
            row = self._insert(cur, project, title, body, scope=None, pinned=pinned,
                               origin="agent", actor=actor, node=node, created_at=created_at)
            self._event(cur, project, "add", actor=actor, node=node, entry_id=row["id"],
                        reason="adopted from a memory folder",
                        detail={"adopt_key": key, "adopted_from": source, "pinned": pinned,
                                "origin": "agent",
                                **({"contextkit_memory_id": memory_id} if memory_id else {})})
            return row, True

    def adopted(self, project: str, carried: int, skipped: int, root: str, *, actor: str,
                node: str) -> None:
        with self.writing(project) as cur:
            self._event(cur, project, "adopt", actor=actor, node=node,
                        reason="memory folder adopted",
                        detail={"carried": carried, "already": skipped, "root": root})

    # -- reading ---------------------------------------------------------------

    def listing(self, project: str, *, status: str, pinned: bool | None, scope: str | None,
                since, limit: int) -> tuple[list[dict], int]:
        where, params = ["project_id = %s"], [project]
        if status != "all":
            where.append("status = %s")
            params.append(status)
        if pinned is not None:
            where.append("pinned = %s")
            params.append(pinned)
        if scope is not None:
            where.append("scope = %s" if scope != "-" else "scope IS NULL")
            if scope != "-":
                params.append(scope)
        if since is not None:
            where.append("created_at >= %s")
            params.append(since)
        clause = " AND ".join(where)
        total = self.one(f"SELECT count(*) AS n FROM memory_entries WHERE {clause}",
                         params)["n"]
        rows = self.rows(f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE {clause} "
                         "ORDER BY created_at DESC, seq DESC LIMIT %s", [*params, limit])
        return self._with_merged(rows), total

    def trail(self, entry_id) -> list[dict]:
        return self.rows("SELECT seq, kind, entry_id, actor, node, at, reason, pass_id, detail "
                         "FROM memory_events WHERE entry_id = %s ORDER BY seq", (entry_id,))

    def search(self, project: str, query: str, *, everything: bool, limit: int
               ) -> tuple[list[dict], int]:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clause = ("project_id = %(project)s AND (status = 'active' OR %(all)s) AND ("
                  "to_tsvector('simple', title || ' ' || body) @@ "
                  "websearch_to_tsquery('simple', %(query)s) "
                  "OR title ILIKE %(like)s OR body ILIKE %(like)s)")
        params = {"project": project, "all": everything, "query": query, "like": like,
                  "limit": limit}
        total = self.one(f"SELECT count(*) AS n FROM memory_entries WHERE {clause}", params)["n"]
        rows = self.rows(
            f"SELECT {ENTRY_COLUMNS}, ts_rank(to_tsvector('simple', title || ' ' || body), "
            f"websearch_to_tsquery('simple', %(query)s)) AS rank FROM memory_entries "
            f"WHERE {clause} ORDER BY rank DESC, created_at DESC LIMIT %(limit)s", params)
        return self._with_merged(rows), total

    def log(self, project: str, *, since, limit: int) -> tuple[list[dict], int]:
        where, params = "e.project_id = %s", [project]
        if since is not None:
            where += " AND e.at >= %s"
            params.append(since)
        total = self.one(f"SELECT count(*) AS n FROM memory_events e WHERE {where}",
                         params)["n"]
        rows = self.rows(
            "SELECT e.seq, e.kind, e.entry_id, e.actor, e.node, e.at, e.reason, e.pass_id, "
            f"e.detail, m.title FROM memory_events e LEFT JOIN memory_entries m "
            f"ON m.id = e.entry_id WHERE {where} ORDER BY e.seq DESC LIMIT %s",
            [*params, limit])
        return rows, total

    def counts(self, project: str) -> dict:
        row = self.one(
            "SELECT count(*) FILTER (WHERE status = 'active') AS active, "
            "count(*) FILTER (WHERE status = 'active' AND pinned) AS pinned, "
            "count(*) FILTER (WHERE status = 'superseded') AS superseded, "
            "count(*) FILTER (WHERE status = 'dropped') AS dropped, "
            "count(*) FILTER (WHERE status = 'active' AND scope IS NOT NULL) AS scoped, "
            "count(*) FILTER (WHERE status = 'active' AND origin = 'groom') AS merged "
            "FROM memory_entries WHERE project_id = %s", (project,))
        writes = self.one(
            "SELECT count(*) FILTER (WHERE at >= now() - interval '7 days') AS last_7_days, "
            "count(*) FILTER (WHERE at >= now() - interval '30 days') AS last_30_days, "
            "max(at) AS last_write FROM memory_events WHERE project_id = %s AND kind = 'add' "
            "AND pass_id IS NULL AND NOT (detail ? 'adopt_key')", (project,))
        passes = self.one(
            "SELECT count(*) AS passes, max(at) AS last_pass FROM memory_events "
            "WHERE project_id = %s AND kind = 'groom'", (project,))
        last = self.one("SELECT max(seq) AS seq FROM memory_events WHERE project_id = %s",
                        (project,))
        return {"entries": {k: row[k] for k in ("active", "pinned", "superseded", "dropped",
                                                "scoped", "merged")},
                "writes": {"last_7_days": writes["last_7_days"],
                           "last_30_days": writes["last_30_days"],
                           "last_write": iso(writes["last_write"])},
                "grooming": {"passes": passes["passes"], "last_pass": iso(passes["last_pass"])},
                "last_event": last["seq"]}

    def last_event(self, project: str) -> int | None:
        return self.one("SELECT max(seq) AS seq FROM memory_events WHERE project_id = %s",
                        (project,))["seq"]

    def last_write_at(self, project: str):
        """When anyone but a grooming pass last changed this project's memory."""
        return self.one("SELECT max(at) AS at FROM memory_events WHERE project_id = %s "
                        "AND pass_id IS NULL AND kind IN ('add', 'supersede', 'drop', 'pin', "
                        "'unpin', 'purge')", (project,))["at"]

    def last_pass_at(self, project: str):
        return self.one("SELECT max(at) AS at FROM memory_events WHERE project_id = %s "
                        "AND kind = 'groom'", (project,))["at"]

    def _with_merged(self, rows: list[dict]) -> list[dict]:
        """Each merged entry carries how many entries it holds and their dates."""
        merged = [r["id"] for r in rows if r["origin"] == "groom"]
        if not merged:
            return rows
        found = {r["entry_id"]: r["detail"] for r in self.rows(
            "SELECT entry_id, detail FROM memory_events WHERE kind = 'add' "
            "AND entry_id = ANY(%s) AND pass_id IS NOT NULL", (merged,))}
        for row in rows:
            detail = found.get(row["id"]) or {}
            if row["origin"] == "groom" and detail.get("merged"):
                row["merged"] = {"count": detail["merged"], "from": detail.get("from"),
                                 "to": detail.get("to")}
        return rows

    def render_rows(self, project: str) -> list[dict]:
        """Every active project-wide entry, newest first."""
        return self._with_merged(self.rows(
            f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE project_id = %s "
            "AND status = 'active' AND scope IS NULL ORDER BY created_at DESC, seq DESC",
            (project,)))

    def export(self, project: str) -> dict:
        entries = self._with_merged(self.rows(
            f"SELECT {ENTRY_COLUMNS} FROM memory_entries WHERE project_id = %s ORDER BY seq",
            (project,)))
        events = self.rows("SELECT seq, kind, entry_id, actor, node, at, reason, pass_id, detail "
                           "FROM memory_events WHERE project_id = %s ORDER BY seq", (project,))
        return {"entries": [entry_out(e) for e in entries],
                "events": [event_out(e) for e in events]}

    # -- grooming --------------------------------------------------------------

    def candidates(self, project: str, window_days: int) -> tuple[list[dict], list[dict]]:
        """What a pass may touch - the active, unpinned project-wide entries in
        the window - and the pinned ones it reads for context only."""
        rows = self.render_rows(project)
        since = now_utc() - _dt.timedelta(days=window_days)
        pinned = [r for r in rows if r["pinned"]]
        movable = [r for r in rows if not r["pinned"] and r["created_at"] >= since]
        return movable, pinned

    def try_groom_lock(self, project: str) -> bool:
        """The store lock that keeps a second pass for this project out, on any
        machine, for as long as this connection holds it."""
        row = self.one("SELECT pg_try_advisory_lock(hashtext('memory groom ' || %s), "
                       "hashtext(%s)) AS got", (self.schema, project))
        return bool(row["got"])

    def groom_unlock(self, project: str) -> None:
        with contextlib.suppress(Exception):
            self.one("SELECT pg_advisory_unlock(hashtext('memory groom ' || %s), "
                     "hashtext(%s)) AS done", (self.schema, project))

    def apply_pass(self, project: str, plan: dict, *, actor: str, node: str,
                   summary: dict) -> dict:
        """Apply a checked plan in one transaction: each merge a new entry that
        supersedes its sources, each drop a status change, every one an event
        under one pass id. An operation whose entries changed since the plan
        was read is skipped and said."""
        pass_id = uuid.uuid4()
        applied: dict = {"merges": [], "drops": [], "skipped": []}
        with self.writing(project) as cur:
            for merge in plan["merges"]:
                rows = [self._locked(cur, sid) for sid in merge["sources"]]
                stale = [sid for sid, row in zip(merge["sources"], rows)
                         if row is None or row["status"] != "active" or row["pinned"]
                         or row["project_id"] != project]
                if stale:
                    applied["skipped"].append({"merge": merge["title"],
                                               "why": "its sources changed since the plan was "
                                                      "read: " + ", ".join(map(short, stale))})
                    continue
                count = 0
                dates = []
                for row in rows:
                    held = (self._merged_detail(cur, row["id"]) or {})
                    count += int(held.get("merged") or 1)
                    dates += [held.get("from") or day(row["created_at"]),
                              held.get("to") or day(row["created_at"])]
                made = self._insert(cur, project, merge["title"], merge["body"], scope=None,
                                    pinned=False, origin="groom", actor=actor, node=node)
                self._event(cur, project, "add", actor=actor, node=node, entry_id=made["id"],
                            reason=merge.get("reason"), pass_id=pass_id,
                            detail={"sources": [str(r["id"]) for r in rows], "merged": count,
                                    "from": min(dates), "to": max(dates), "origin": "groom"})
                for row in rows:
                    cur.execute("UPDATE memory_entries SET status = 'superseded', "
                                "superseded_by = %s WHERE id = %s", (made["id"], row["id"]))
                    self._event(cur, project, "supersede", actor=actor, node=node,
                                entry_id=row["id"], reason=merge.get("reason"), pass_id=pass_id,
                                detail={"by": [str(made["id"])]})
                applied["merges"].append({"entry": entry_out(made, with_body=False),
                                          "sources": [short(r["id"]) for r in rows],
                                          "merged": count})
            for drop in plan["drops"]:
                row = self._locked(cur, drop["id"])
                if row is None or row["status"] != "active" or row["pinned"] \
                        or row["project_id"] != project:
                    applied["skipped"].append({"drop": short(drop["id"]),
                                               "why": "it changed since the plan was read"})
                    continue
                cur.execute("UPDATE memory_entries SET status = 'dropped' WHERE id = %s",
                            (row["id"],))
                self._event(cur, project, "drop", actor=actor, node=node, entry_id=row["id"],
                            reason=drop.get("reason"), pass_id=pass_id)
                applied["drops"].append({"entry": short(row["id"]), "title": row["title"],
                                         "reason": drop.get("reason")})
            self._event(cur, project, "groom", actor=actor, node=node, pass_id=pass_id,
                        reason=summary.get("trigger"),
                        detail={**summary, "merges": len(applied["merges"]),
                                "drops": len(applied["drops"]),
                                "skipped": len(applied["skipped"])})
        return {"pass": str(pass_id), **applied}

    @staticmethod
    def _merged_detail(cur, entry_id) -> dict | None:
        cur.execute("SELECT detail FROM memory_events WHERE kind = 'add' AND entry_id = %s "
                    "AND pass_id IS NOT NULL ORDER BY seq LIMIT 1", (entry_id,))
        row = cur.fetchone()
        return row["detail"] if row else None

    def resolve_pass(self, project: str, ref: str) -> uuid.UUID:
        ref = (ref or "").strip().lower()
        if not REF_RE.fullmatch(ref):
            raise Refusal(6, "input", f"{ref!r} is no pass id",
                          "`memory log` names each pass by its id")
        rows = self.rows("SELECT DISTINCT pass_id FROM memory_events WHERE project_id = %s "
                         "AND kind = 'groom' AND replace(pass_id::text, '-', '') LIKE %s",
                         (project, ref.replace("-", "") + "%"))
        if not rows:
            raise Refusal(3, "not_found", f"no grooming pass of this project matches {ref}",
                          "`memory log` names each pass by its id")
        if len(rows) > 1:
            raise Refusal(6, "ambiguous_id", f"{ref} names more than one pass",
                          "give more characters of the id")
        return rows[0]["pass_id"]

    def undo_pass(self, project: str, ref: str, *, actor: str, node: str) -> dict:
        """Reactivate what a pass superseded or dropped, and drop what it made."""
        with self.writing(project) as cur:
            pass_id = self.resolve_pass(project, ref)
            cur.execute("SELECT 1 FROM memory_events WHERE project_id = %s AND kind = 'groom_undo' "
                        "AND detail->>'undoes' = %s", (project, str(pass_id)))
            if cur.fetchone():
                raise Refusal(6, "already_undone", f"pass {pass_id} is already undone")
            cur.execute("SELECT kind, entry_id FROM memory_events WHERE pass_id = %s "
                        "AND project_id = %s AND kind IN ('add', 'supersede', 'drop') "
                        "ORDER BY seq", (pass_id, project))
            events = cur.fetchall()
            made = [e["entry_id"] for e in events if e["kind"] == "add"]
            back = [e["entry_id"] for e in events if e["kind"] in ("supersede", "drop")]
            for entry_id in made:
                row = self._locked(cur, entry_id)
                if row is not None and row["status"] != "active":
                    cur.execute("SELECT pass_id FROM memory_events WHERE entry_id = %s "
                                "AND kind IN ('supersede', 'drop') ORDER BY seq DESC LIMIT 1",
                                (entry_id,))
                    later = cur.fetchone()
                    hint = (f"undo pass {later['pass_id']} first" if later and later["pass_id"]
                            else f"its entry {short(entry_id)} was changed by hand since")
                    raise Refusal(6, "pass_overtaken",
                                  f"entry {short(entry_id)} this pass made is {row['status']} now",
                                  hint)
            undo_id = uuid.uuid4()
            for entry_id in made:
                if self._locked(cur, entry_id) is None:
                    continue
                cur.execute("UPDATE memory_entries SET status = 'dropped' WHERE id = %s",
                            (entry_id,))
                self._event(cur, project, "drop", actor=actor, node=node, entry_id=entry_id,
                            reason=f"undo of grooming pass {pass_id}", pass_id=undo_id)
            restored = []
            for entry_id in back:
                row = self._locked(cur, entry_id)
                if row is None or row["status"] == "active":
                    continue
                cur.execute("UPDATE memory_entries SET status = 'active', superseded_by = NULL "
                            "WHERE id = %s", (entry_id,))
                self._event(cur, project, "reactivate", actor=actor, node=node,
                            entry_id=entry_id, reason=f"undo of grooming pass {pass_id}",
                            pass_id=undo_id)
                restored.append(short(entry_id))
            self._event(cur, project, "groom_undo", actor=actor, node=node, pass_id=undo_id,
                        reason=f"undo of grooming pass {pass_id}",
                        detail={"undoes": str(pass_id), "dropped": len(made),
                                "reactivated": len(restored)})
        return {"undone": str(pass_id), "dropped": [short(m) for m in made],
                "reactivated": restored}


# ── the block ────────────────────────────────────────────────────────────────

def _indent(text: str) -> str:
    """An entry's text under its index line, as the window's measure writes it
    out. A line that would open a Markdown heading is escaped."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            out.append("")
            continue
        if line.lstrip().startswith("#"):
            line = line.replace("#", "\\#", 1)
        out.append("  " + line)
    return "\n".join(out)


def neutralise(text: str) -> str:
    """Entry text is anyone's, and a host finds the generated capabilities block
    by HTML comment markers: no comment opener survives into the block."""
    return text.replace("<!--", "&lt;!--")


def _heading(row: dict) -> str:
    line = f"- [{short(row['id'])}] {day(row['created_at'])} · {row['title']}"
    merged = row.get("merged")
    if merged and merged.get("count"):
        span = merged.get("from") or ""
        if merged.get("to") and merged.get("to") != span:
            span = f"{span}..{merged['to']}"
        line += f" (merged {merged['count']}, {span})"
    return line


def _full(row: dict, entry_chars: int) -> str:
    body = row["body"].strip()
    if len(body) > entry_chars:
        body = (body[:entry_chars].rstrip() + f" … (`memory show {short(row['id'])}` "
                "for the rest)")
    return _heading(row) + "\n" + _indent(body)


DIRECTIVE = ("This is the project's memory, and it takes the place of the harness's built-in "
             "memory: record here what should outlast this session, and look here before "
             "relying on what seems known. Do not keep memory in the harness's own memory "
             "files or memory tool in this project.")

RECORD = ("Record first, the moment a conclusion is reached: "
          "`memory add \"<the conclusion, its reason and its evidence>\" "
          "--title \"<the conclusion in one line>\"`. An entry is one conclusion that should "
          "govern work not yet done and has no other home yet - a decision, a correction, a "
          "fact learned the hard way. It is not an account of what a run did, which belongs "
          "in that run's own record; not a procedure or a settled rule, which belong in the "
          "project's routines and doctrine; not what the code or a tool already answers; and "
          "never a secret.")

SEARCH = ("Search before relying on what seems known: `memory search \"<terms>\"` gives the "
          "index line of every active entry that matches.")

DEEP = ("Search deeply by handing a broad question to a subagent: it searches, lists and "
        "reads the entries and returns only its conclusion, so the reading does not crowd "
        "this session.")

READ = ("`memory show <id>` reads one entry whole, with its trail; `memory help` gives every "
        "command.")


def _moment(moment) -> str:
    """A moment `memory list --since` takes, cut to the second, so the list it
    names holds the newest entry itself."""
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aggregate(active: int, newest) -> str:
    if not active:
        return ("No entries yet: this project's memory is empty, and the first `memory add` "
                "starts it.")
    stamp = _moment(newest)
    return (f"{active} active entr{'y' if active == 1 else 'ies'}; the newest written "
            f"{stamp}. `memory list --since {stamp}` lists it and anything newer, for a "
            "session that has run long since this block was rendered.")


def render_block(rows: list[dict], settings: dict, *, now: _dt.datetime, provenance: str,
                 max_bytes: int = CONTRIBUTION_MAX_BYTES) -> tuple[str, dict]:
    """The block for one project's context, and what it measured.

    The block carries no entry: a directive that this memory replaces the
    harness's own, how to record and what belongs, how to search, how to
    search deeply, the count of active project-wide entries with the moment
    of the newest and the command listing anything newer, and how to read
    one entry and find every command. Its size does not grow with the
    entries. The report adds the window's measure, which decides when the
    service consolidates."""
    newest = max((r["created_at"] for r in rows), default=None)
    text = neutralise("\n\n".join([DIRECTIVE, RECORD, SEARCH, DEEP,
                                     _aggregate(len(rows), newest), READ, provenance]) + "\n")
    size = len(text.encode())
    if size > max_bytes:
        raise ValueError(f"the memory block is {size} bytes, past its ceiling of {max_bytes}")
    report = {"chars": len(text), "bytes": size, "active": len(rows),
              "pinned": sum(1 for r in rows if r["pinned"]), "newest": iso(newest),
              **measure_window(rows, settings, now=now)}
    return text, report


def measure_window(rows: list[dict], settings: dict, *, now: _dt.datetime) -> dict:
    """Whether the window still fits: its unpinned project-wide entries, those
    of the last `window_days` topped up to `min_entries` with older ones, are
    at most `max_entries`, and each written out at `entry_chars` they take at
    most `block_chars` characters. A window that does not fit is what a
    grooming pass is for; pinned entries are left out, since no pass touches
    them."""
    since = now - _dt.timedelta(days=int(settings["window_days"]))
    rest = [r for r in rows if not r["pinned"]]
    window = [r for r in rest if r["created_at"] >= since]
    if len(window) < int(settings["min_entries"]):
        older = [r for r in rest if r["created_at"] < since]
        window += older[:int(settings["min_entries"]) - len(window)]
    entry_chars = int(settings["entry_chars"])
    chars = sum(len(_full(row, entry_chars)) + 1 for row in window)
    return {"window": len(window), "window_chars": chars,
            "fits": len(window) <= int(settings["max_entries"])
            and chars <= int(settings["block_chars"])}


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    spare.write_text(text, encoding="utf-8")
    os.replace(spare, path)


def _same_block(context: Path, rows: list[dict], settings: dict, *, now: _dt.datetime,
                max_bytes: int) -> bool:
    """Whether the block already written is the one these rows render, its own
    provenance line put in place of the new one."""
    try:
        written = context.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    provenance = written.rstrip("\n").rpartition("\n")[2]
    if not provenance.startswith("_Rendered "):
        return False
    try:
        again, _report = render_block(rows, settings, now=now, provenance=provenance,
                                      max_bytes=max_bytes)
    except ValueError:
        return False
    return again == written


def render_project(store: Store, project: str, settings: dict, context: Path, *,
                   rendered_by: str, max_bytes: int = CONTRIBUTION_MAX_BYTES) -> dict:
    """Render one project's block from the store into its contribution file,
    with a sidecar saying what it holds and which event it is current to.

    A block that differs from the one already written only in its provenance
    line leaves the file as it is, its render time included, so a context
    built from it does not change; the sidecar is written either way."""
    now = now_utc()
    last = store.last_event(project)
    rows = store.render_rows(project)
    provenance = (f"_Rendered {now.strftime('%Y-%m-%d %H:%M')} UTC by {rendered_by} from the "
                  f"{store_label(store.setting)}; `memory render` renders it again and "
                  "`memory doctor` says what is wrong when it is stale or missing._")
    text, report = render_block(rows, settings, now=now, provenance=provenance,
                                max_bytes=max_bytes)
    if not _same_block(context, rows, settings, now=now, max_bytes=max_bytes):
        write_atomic(context, text)
    sidecar = {"rendered_at": iso(now), "rendered_by": rendered_by, "last_event": last,
               "project_id": project, "store": store_label(store.setting), **report}
    write_atomic(context.with_suffix(".json"), json.dumps(sidecar, indent=2) + "\n")
    return {"path": str(context), **sidecar}


# ── grooming ─────────────────────────────────────────────────────────────────

GROOM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["merges", "drops"],
    "properties": {
        "merges": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["sources", "title", "body", "reason"],
            "properties": {"sources": {"type": "array", "items": {"type": "string"},
                                       "minItems": 2},
                           "title": {"type": "string"}, "body": {"type": "string"},
                           "reason": {"type": "string"}}}},
        "drops": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "reason"],
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}}}},
    },
}

GROOM_RULES = """\
You consolidate the memory of one software project. Each entry below is one
conclusion an agent or a person recorded. Your answer is a plan, as JSON
matching the schema you were given; you change nothing yourself.

- merges: entries that cover the same subject, or that contradict each other,
  become ONE entry that replaces them. A merge names at least two sources by
  their full ids, a one-line title, the merged text, and the reason. The merged
  text keeps every fact still true, and the later entry wins a contradiction.
- Every quotation in a source - text in quotation marks, above all a person's
  own words - appears in the merged text word for word, in the same quotation
  marks. A merge that cannot keep a quotation whole is not proposed.
- drops: an entry that no longer holds - a later entry says it was undone or
  replaced - is dropped, with the reason. Nothing else is dropped.
- PINNED entries are listed for context. They are never a source and never
  dropped.
- A single entry is never rewritten on its own, and an entry that needs no
  change is left out of the plan. When in doubt, leave it out: an empty plan
  is a good answer.
"""

QUOTE_RE = re.compile(r"“([^”]{12,})”|«([^»]{12,})»|\"([^\"\n]{12,})\"|„([^“”]{12,})[“”]")


def quotes_in(text: str) -> list[str]:
    return [next(group for group in match.groups() if group)
            for match in QUOTE_RE.finditer(text)]


def groom_prompt(movable: list[dict], pinned: list[dict]) -> str:
    parts = [GROOM_RULES, "", "Entries, newest first:", ""]
    for row in sorted(pinned + movable, key=lambda r: r["created_at"], reverse=True):
        mark = " · PINNED" if row["pinned"] else ""
        parts += [f"=== {row['id']} · {day(row['created_at'])}{mark}",
                  f"Title: {row['title']}", row["body"].strip(), ""]
    return "\n".join(parts)


def check_plan(plan: Any, movable: list[dict], pinned: list[dict]) -> tuple[dict, list[dict]]:
    """The plan's operations that may be applied, and every one refused with
    the reason: a merge needs two or more distinct sources this pass may touch,
    a one-line title, text, and every quotation of its sources kept whole; a
    drop names one entry this pass may touch; no entry is in two operations."""
    accepted: dict = {"merges": [], "drops": []}
    refused: list[dict] = []
    if not isinstance(plan, dict):
        return accepted, [{"what": "the plan", "why": "the answer is not a JSON object"}]
    by_id = {str(r["id"]): r for r in movable}
    pinned_ids = {str(r["id"]) for r in pinned}
    used: set[str] = set()

    def known(ref) -> str | None:
        ref = str(ref or "").strip().lower()
        if ref in by_id:
            return ref
        hits = [i for i in by_id if i.replace("-", "").startswith(ref.replace("-", ""))] \
            if len(ref) >= 8 else []
        return hits[0] if len(hits) == 1 else None

    for merge in plan.get("merges") or []:
        if not isinstance(merge, dict):
            refused.append({"what": "a merge", "why": "not an object"})
            continue
        title = str(merge.get("title") or "").strip()
        label = f"merge {title!r}"
        raw = merge.get("sources") or []
        sources = []
        why = None
        for ref in raw if isinstance(raw, list) else []:
            sid = known(ref)
            if sid is None:
                why = (f"source {ref} is pinned" if str(ref) in pinned_ids
                       else f"source {ref} is no entry this pass may touch")
                break
            if sid in used:
                why = f"source {short(sid)} is in another operation"
                break
            if sid not in sources:
                sources.append(sid)
        body = str(merge.get("body") or "").strip()
        if why is None and len(sources) < 2:
            why = "a merge takes at least two distinct sources"
        if why is None and (not title or "\n" in title or len(title) > TITLE_MAX):
            why = f"its title is not one line of at most {TITLE_MAX} characters"
        if why is None and not body:
            why = "it has no text"
        if why is None and len(body) > BODY_MAX:
            why = f"its text is longer than {BODY_MAX} characters"
        if why is None:
            lost = [q for sid in sources for q in quotes_in(by_id[sid]["body"] + "\n"
                                                             + by_id[sid]["title"])
                    if q.strip() not in body + "\n" + title]
            if lost:
                why = "it does not keep this quotation word for word: " + lost[0][:80]
        if why:
            refused.append({"what": label, "why": why})
            continue
        used.update(sources)
        accepted["merges"].append({"sources": sources, "title": title, "body": body,
                                   "reason": str(merge.get("reason") or "").strip() or None})
    for drop in plan.get("drops") or []:
        if not isinstance(drop, dict):
            refused.append({"what": "a drop", "why": "not an object"})
            continue
        ref = drop.get("id")
        sid = known(ref)
        reason = str(drop.get("reason") or "").strip()
        if sid is None:
            why = (f"{ref} is pinned" if str(ref) in pinned_ids
                   else f"{ref} is no entry this pass may touch")
        elif sid in used:
            why = f"{short(sid)} is in another operation"
        elif not reason:
            why = "a drop says why the entry no longer holds"
        else:
            why = None
        if why:
            refused.append({"what": f"drop {ref}", "why": why})
            continue
        used.add(sid)
        accepted["drops"].append({"id": sid, "reason": reason})
    return accepted, refused


def describe_plan(accepted: dict, movable: list[dict]) -> dict:
    by_id = {str(r["id"]): r for r in movable}
    return {
        "merges": [{"title": m["title"], "reason": m["reason"],
                    "sources": [{"id": short(s), "title": by_id[s]["title"]} for s in m["sources"]],
                    "body": m["body"]} for m in accepted["merges"]],
        "drops": [{"id": short(d["id"]), "title": by_id[d["id"]]["title"], "reason": d["reason"]}
                  for d in accepted["drops"]],
    }


# ── adopting a memory folder ─────────────────────────────────────────────────

_DATED = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# ContextKit writes one of these on the line before an entry's heading: the
# entry's durable id, which belongs to the section that follows it.
_MEMORY_ID = re.compile(r"\s*<!--\s*contextkit-memory-id:\s*([0-9A-Fa-f]{64})\s*-->\s*")
_TIMED = re.compile(r"^(\d{1,2}):(\d{2})\s*(?:[—–-]+\s*)?(.*)$")


def parse_memory_folder(folder: Path, zone=None) -> dict:
    """Every entry a memory folder holds, with the decision taken for each.

    A file is one day's Markdown: an optional `# <date>` heading, then one
    `## [HH:MM — ]<title>` section per entry whose paragraphs are its text. The
    day comes from the file name, else its first heading, else its modification
    time; a section with no time keeps the file's order. The date and the time
    are read in `zone`, a tzinfo, or in the machine's local zone when it is
    None, each date taking that zone's offset on it. Text before the first
    section is an entry of its own, titled after the file. What is not read is
    said, never passed over in silence.

    A `<!-- contextkit-memory-id: <hex> -->` line is the id of the section
    after it: it is never an entry and never part of one's text, and the entry
    carries it as `memory_id`. Text before the first section that is only the
    file's title, blank lines and such markers is no entry."""
    found: dict = {"folder": str(folder), "entries": [], "not_read": []}
    if not folder.is_dir():
        found["not_read"].append({"path": str(folder), "why": "no such folder"})
        return found
    for path in sorted(folder.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            found["not_read"].append({"path": str(path), "why": "a folder; only the "
                                      "memory folder's own Markdown files are read"})
            continue
        if path.suffix.lower() != ".md":
            found["not_read"].append({"path": str(path), "why": "not a Markdown file"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            found["not_read"].append({"path": str(path), "why": f"unreadable: {exc}"})
            continue
        found["entries"] += _parse_file(path, text, zone)
    return found


def _in_zone(day: _dt.date, hour: int, minute: int, zone) -> _dt.datetime:
    """A wall-clock moment on `day` in `zone`, or in the machine's local zone
    when it is None, carrying the offset that zone has on that date."""
    naive = _dt.datetime(day.year, day.month, day.day, hour, minute)
    return naive.replace(tzinfo=zone) if zone is not None else naive.astimezone()


def _zone_said(moment: _dt.datetime, zone) -> str:
    offset = moment.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}"
    if zone is None:
        return f"the machine's local zone ({moment.tzname()}, {offset})"
    return f"{getattr(zone, 'key', None) or moment.tzname()} ({offset})"


def _parse_file(path: Path, text: str, zone=None) -> list[dict]:
    lines = text.splitlines()
    heading = next((line[2:].strip() for line in lines if line.startswith("# ")), None)
    dated = _DATED.search(path.stem) or (_DATED.search(heading) if heading else None)
    if dated:
        day = _dt.date(int(dated.group(1)), int(dated.group(2)), int(dated.group(3)))
        when = "the file name" if _DATED.search(path.stem) else "the file's heading"
    else:
        day = _dt.datetime.fromtimestamp(path.stat().st_mtime, zone).date()
        when = "the file's modification time"
    base = _in_zone(day, 0, 0, zone)
    sections: list[tuple[str | None, list[str], str | None]] = [(None, [], None)]
    pending: str | None = None
    unattached: list[str] = []
    for line in lines:
        marker = _MEMORY_ID.fullmatch(line)
        if marker:
            if pending:
                unattached.append(pending)
            pending = marker.group(1).lower()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), [], pending))
            pending = None
        elif line.startswith("# ") and sections[-1][0] is None:
            continue
        else:
            sections[-1][1].append(line)
    if pending:
        unattached.append(pending)
    out = []
    for memory_id in unattached:
        out.append({"source": f"{path.name}#contextkit-memory-id:{memory_id[:12]}",
                    "file": str(path), "title": None, "decision": "skip",
                    "why": "a memory id marker with no section after it", "chars": 0,
                    "created_at": None, "dated_from": None, "memory_id": memory_id})
    for index, (head, body_lines, memory_id) in enumerate(sections):
        body = "\n".join(body_lines).strip()
        source = f"{path.name}" + (f"#{head}" if head else "")
        entry: dict = {"source": source, "file": str(path)}
        if memory_id:
            entry["memory_id"] = memory_id
        if head is None:
            if not body:
                continue
            title, time_of = (heading or path.stem), None
            entry["decision_note"] = "text before the first section, carried as its own entry"
        else:
            timed = _TIMED.match(head)
            if timed and int(timed.group(1)) < 24:
                time_of = (int(timed.group(1)), int(timed.group(2)))
                title = timed.group(3).strip() or head
            else:
                time_of, title = None, head
        if time_of:
            created = _in_zone(day, time_of[0], time_of[1], zone)
            when_said = f"{when} and the section's time, read in {_zone_said(created, zone)}"
        else:
            created = base + _dt.timedelta(seconds=index)
            when_said = f"{when}, in the file's order, read in {_zone_said(base, zone)}"
        title = " ".join(title.split())
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rstrip() + "…"
        entry.update(title=title, created_at=created, dated_from=when_said, chars=len(body))
        if not body:
            entry["decision"] = "skip"
            entry["why"] = "the section has no text"
        elif len(body) > BODY_MAX:
            entry["decision"] = "skip"
            entry["why"] = f"its text is longer than {BODY_MAX} characters; split it by hand"
        else:
            entry["decision"] = "carry"
            entry["body"] = body
        out.append(entry)
    return out


def adopt_key(project: str, title: str, body: str, memory_id: str | None = None) -> str:
    """What makes a carried entry the same entry on every machine: the id its
    folder gave it where it has one, so a heading edited since still matches,
    else its title and text."""
    if memory_id:
        return hashlib.sha256(f"{project}\ncontextkit-memory-id\n{memory_id}".encode()).hexdigest()
    return hashlib.sha256(f"{project}\n{title}\n{body}".encode()).hexdigest()


# ── the service ──────────────────────────────────────────────────────────────

PID_FILE = "daemon.pid"
LOCK_FILE = "daemon.lock"
STATUS_FILE = "daemon.json"
LOG_FILE = "daemon.log"
MACHINE_TAG = "machine"
ADMIT_SECONDS = 5.0
CHECK_SECONDS = 10.0
POLL_SECONDS = 60.0
RERENDER_SECONDS = 3600.0
GROOM_CHECK_SECONDS = 300.0
PUBLISH_SECONDS = 2.0
DEBOUNCE_SECONDS = 0.5
FOLLOW_SECONDS = 1.0
RECONNECT_FIRST = 1.0
RECONNECT_LONGEST = 30.0


def default_node() -> str:
    return socket.gethostname()


class StoreLink:
    """The service's connection to one store, shared by every project on it:
    the transaction connection its questions go on, opened when a step first
    asks one and closed when the step ends (`release`), so between steps it
    holds no session. It learns what changed by following the projects'
    versions of `memory_changed` on that connection, once a step. The tables
    are migrated once, when the link first connects."""

    def __init__(self, setting):
        self.setting = setting
        self._store: Store | None = None
        self.migrated = False
        self.live = False
        # Kept across a lost connection, so the first poll after it reports
        # everything that moved meanwhile.
        self.follower = None
        self.follow_error: str | None = None
        self.error: str | None = None
        self.retry_at = 0.0
        self.delay = RECONNECT_FIRST

    @property
    def store(self) -> Store:
        """The questions' connection for this step, opened on first use."""
        if self._store is None:
            self._store = Store(self.setting, application_name="memory-service").open(
                migrate=not self.migrated)
            self.migrated = True
        return self._store

    def release(self) -> None:
        """Close the questions' connection at the end of a step."""
        if self._store is not None:
            self._store.close()
        self._store = None

    @property
    def connected(self) -> bool:
        return self.migrated and self.live

    def ensure(self, log: Callable[[str], None]) -> bool:
        if self.connected:
            return True
        if time.monotonic() < self.retry_at:
            return False
        try:
            self.store  # noqa: B018 - opens it, migrating the tables once
            self.live = True
        except Exception as exc:
            why = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            if why != self.error:
                log(f"store {self.setting.schema}: not reachable, asking again: {why}")
            self.error = why
            self.drop()
            self.retry_at = time.monotonic() + self.delay
            self.delay = min(self.delay * 2, RECONNECT_LONGEST)
            return False
        if self.error is not None:
            log(f"store {self.setting.schema}: reachable again")
        self.error = None
        self.delay = RECONNECT_FIRST
        return True

    def lost(self, exc: BaseException, log: Callable[[str], None]) -> None:
        why = f"{type(exc).__name__}: {exc}"
        log(f"store {self.setting.schema}: connection lost, reconnecting: {why}")
        self.error = why
        self.drop()
        self.retry_at = time.monotonic() + self.delay

    def drop(self) -> None:
        self.release()
        self.live = False

    def changes(self) -> set[str]:
        """The projects whose version of `memory_changed` moved since the last
        ask, read in one query on this step's connection; on the first ask,
        every project the store holds a version for."""
        if self.follower is None:
            self.follower = database().Follower([CHANNEL], application_name="memory-service",
                                                setting=self.setting)
        moved = self.follower.poll(self.store.conn)
        return {scope for (_, scope) in moved}

    def row(self) -> dict:
        # `listening` stays in the row for a reader of the earlier shape; the
        # link holds no listening session.
        return {"store": store_label(self.setting), "schema": self.setting.schema,
                "connected": self.connected, "listening": False,
                "following": self.follower is not None, "error": self.error}


class Slot:
    """One project as the process sees it."""

    def __init__(self, engine: "Engine", project_id: str, entry: dict):
        self.engine = engine
        self.project_id = project_id
        self.entry = entry
        self.slug = entry.get("slug") or project_id
        self.plan: dict | None = None
        self.link: StoreLink | None = None
        self.lock_handle = None
        self.state = "pending"
        self.reason: str | None = None
        self.reload_error: str | None = None
        self.next_admit = 0.0
        self.next_check = 0.0
        self.next_poll = 0.0
        self.next_groom_check = 0.0
        self.dirty_at: float | None = None
        self.rendered: dict | None = None
        self.rendered_mono = 0.0
        self.render_error: str | None = None
        self.groom: dict = {"on": None, "reason": None, "running": None, "last_attempt": None,
                            "last_result": None}
        self.groom_proc: subprocess.Popen | None = None
        self.groom_started = 0.0
        self.groom_deadline = 0.0
        self.groom_backoff_until = 0.0

    def log(self, message: str) -> None:
        self.engine.log(self.slug, message)

    def row(self) -> dict:
        rendered = self.rendered or {}
        return {"project": self.slug, "project_id": self.project_id,
                "root": self.entry.get("root"), "state": self.state, "reason": self.reason,
                "fingerprint": (self.plan or {}).get("fingerprint"),
                "reload_error": self.reload_error,
                "rendered_at": rendered.get("rendered_at"),
                "last_event": rendered.get("last_event"),
                "window": rendered.get("window"), "active": rendered.get("active"),
                "fits": rendered.get("fits"), "render_error": self.render_error,
                "context": (self.plan or {}).get("context") and str(self.plan["context"]),
                "consolidation": dict(self.groom)}

    def admit(self) -> None:
        plan, refusal = self.engine.host.admit(self.project_id, self.entry)
        if refusal is None and not self._take_lock(plan):
            refusal = ("refused", "another memory service process serves this project; "
                       "stop it there")
        if refusal:
            if (self.state, self.reason) != refusal:
                self.log(f"not served: {refusal[0]}: {refusal[1]}")
            self.state, self.reason = refusal
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        self.plan = plan
        self.link = self.engine.link_for(plan["setting"])
        self.state, self.reason = "served", None
        self.dirty_at = time.monotonic()
        self.next_check = time.monotonic() + CHECK_SECONDS
        self.next_groom_check = time.monotonic() + 5.0
        self.log(f"served: context at {plan['context']}")

    def _take_lock(self, plan: dict) -> bool:
        """The lock that keeps a second process off this project, the same file
        in both modes. A project-mode process holds it as its own lock."""
        path = plan.get("lock")
        if path is None or self.lock_handle is not None:
            return True
        import fcntl
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.lock_handle = handle
        return True

    def reload(self) -> None:
        if self.plan is None:
            self.next_admit = 0.0
            return
        plan, refusal = self.engine.host.admit(self.project_id, self.entry)
        if refusal:
            self.reload_error = f"{refusal[0]}: {refusal[1]}"
            self.log(f"reload rejected, the previous declaration stays: {self.reload_error}")
            return
        if self.engine.store_key(plan["setting"]) != self.engine.store_key(self.plan["setting"]):
            self.reload_error = "the project now resolves another store; that takes a restart"
            self.log(f"reload rejected: {self.reload_error}")
            return
        self.reload_error = None
        changed = plan["fingerprint"] != self.plan["fingerprint"]
        self.plan = plan
        self.dirty_at = time.monotonic()
        if changed:
            self.log(f"reloaded: fingerprint {plan['fingerprint']}")

    def release(self, said: str) -> None:
        self.stop_groom()
        self.plan = None
        self.link = None
        if self.lock_handle is not None:
            with contextlib.suppress(Exception):
                self.lock_handle.close()
            self.lock_handle = None
        self.log(said)

    # -- the work -------------------------------------------------------------

    def due(self, now: float) -> bool:
        if self.plan is None or self.link is None:
            return False
        if self.dirty_at is not None and now - self.dirty_at >= DEBOUNCE_SECONDS:
            return True
        return now - self.rendered_mono >= RERENDER_SECONDS

    def render(self) -> None:
        link = self.link
        if link is None or not link.ensure(self.engine.log_machine):
            return
        try:
            self.rendered = render_project(link.store, self.project_id,
                                           self.plan["settings"], Path(self.plan["context"]),
                                           rendered_by="the memory service")
        except StoreUnavailable as exc:
            self.render_error = f"{exc.slug}: {exc.message}"
            return
        except OSError as exc:
            if self.render_error != str(exc):
                self.log(f"cannot write the context file: {exc}")
            self.render_error = f"{type(exc).__name__}: {exc}"
            self.dirty_at = None
            self.rendered_mono = time.monotonic()
            return
        except Exception as exc:
            if _connection_lost(exc):
                link.lost(exc, self.engine.log_machine)
                return
            self.render_error = f"{type(exc).__name__}: {exc}"
            self.log(f"render failed: {self.render_error}")
            self.dirty_at = None
            self.rendered_mono = time.monotonic()
            return
        self.render_error = None
        self.dirty_at = None
        self.rendered_mono = time.monotonic()
        self.next_poll = time.monotonic() + POLL_SECONDS
        self.engine.changed = True

    def poll(self) -> None:
        """A change the versions did not show is found here: the store's
        newest event against the one the file is current to."""
        self.next_poll = time.monotonic() + POLL_SECONDS
        link = self.link
        if link is None or not link.ensure(self.engine.log_machine):
            return
        try:
            last = link.store.last_event(self.project_id)
        except Exception as exc:
            if _connection_lost(exc):
                link.lost(exc, self.engine.log_machine)
            return
        if last != (self.rendered or {}).get("last_event"):
            self.dirty_at = self.dirty_at or time.monotonic()

    def consider_groom(self, running: int, cap: int) -> bool:
        """Whether to start a pass now: consolidation on and the model ready,
        the window no longer fitting the block, no pass within the interval on
        any machine, and no write for the quiet period."""
        self.next_groom_check = time.monotonic() + GROOM_CHECK_SECONDS
        settings = self.plan["settings"]
        if not settings["consolidate"]:
            self.groom.update(on=False, reason="consolidate is false in the project's settings")
            return False
        ready, why = self.engine.host.model_ready(settings)
        if not ready:
            self.groom.update(on=False, reason=why)
            return False
        self.groom.update(on=True, reason=None)
        if self.groom_proc is not None or time.monotonic() < self.groom_backoff_until:
            return False
        if (self.rendered or {}).get("fits", True):
            return False
        link = self.link
        if link is None or not link.ensure(self.engine.log_machine):
            return False
        try:
            last_pass = link.store.last_pass_at(self.project_id)
            last_write = link.store.last_write_at(self.project_id)
        except Exception as exc:
            if _connection_lost(exc):
                link.lost(exc, self.engine.log_machine)
            return False
        now = now_utc()
        if last_pass and now - last_pass < _dt.timedelta(hours=settings["groom_interval_hours"]):
            return False
        if last_write and now - last_write < _dt.timedelta(minutes=settings["quiet_minutes"]):
            return False
        if running >= cap:
            self.groom["reason"] = "waiting for the machine's cap on passes"
            return False
        return True

    def start_groom(self) -> None:
        argv, cwd, env = self.engine.host.groom_command(self.entry)
        settings = self.plan["settings"]
        log_path = self.engine.state_dir / f"groom-{self.slug}.log"
        try:
            with open(log_path, "a") as out:
                self.groom_proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out,
                                                   stderr=subprocess.STDOUT,
                                                   stdin=subprocess.DEVNULL,
                                                   start_new_session=True)
        except OSError as exc:
            self.log(f"cannot start a grooming pass: {exc}")
            self.groom_backoff_until = time.monotonic() + settings["groom_interval_hours"] * 3600
            return
        self.groom_started = time.monotonic()
        self.groom_deadline = self.groom_started + settings["groom_timeout_seconds"] + 60
        self.groom.update(running={"pid": self.groom_proc.pid, "started_at": iso(now_utc())},
                          last_attempt=iso(now_utc()))
        self.log(f"grooming pass started, pid {self.groom_proc.pid}: the window no longer "
                 "fits block_chars")

    def reap_groom(self) -> bool:
        proc = self.groom_proc
        if proc is None:
            return False
        code = proc.poll()
        if code is None:
            if time.monotonic() > self.groom_deadline:
                self.log("grooming pass ran past its deadline; ending it")
                self.stop_groom()
                return True
            return False
        self.groom_proc = None
        self.groom["running"] = None
        settings = (self.plan or {}).get("settings") or SETTINGS_DEFAULTS
        # Whatever came of it, the next attempt waits the interval: a pass that
        # failed is not retried every few minutes against a shared subscription.
        self.groom_backoff_until = time.monotonic() + settings["groom_interval_hours"] * 3600
        self.groom["last_result"] = {"exit": code, "at": iso(now_utc())}
        self.log(f"grooming pass ended with exit {code}"
                 + ("" if code == 0 else f"; see {self.engine.state_dir}/groom-{self.slug}.log"))
        self.dirty_at = self.dirty_at or time.monotonic()
        return True

    def stop_groom(self) -> None:
        proc = self.groom_proc
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        self.groom_proc = None
        self.groom["running"] = None


def _connection_lost(exc: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


class Engine:
    """The process: its projects, the stores they resolve, its cap on passes."""

    def __init__(self, host, *, mode: str, state_dir: Path, echo: bool = True):
        self.host = host
        self.mode = mode
        self.state_dir = Path(state_dir)
        self.echo = echo
        self.node = host.node()
        self.slots: dict[str, Slot] = {}
        self.links: dict[tuple, StoreLink] = {}
        self.stopping = threading.Event()
        self.reload_requested = threading.Event()
        self.settings: dict = {}
        self.list_witness = None
        self.started_at = iso(now_utc())
        self.last_publish = 0.0
        self.reload_error: str | None = None
        self.fingerprint: str | None = None
        self.log_lock = threading.Lock()
        # Set by whatever changes what the status says, so the next publish is
        # not held back by the interval.
        self.changed = False

    # -- small parts ----------------------------------------------------------

    def log(self, project: str, message: str) -> None:
        self.changed = True
        text = " ".join(part.strip() for part in str(message).splitlines() if part.strip())
        line = f"{iso(now_utc())} memory service [{project}]: {text}\n"
        with self.log_lock:
            with contextlib.suppress(OSError):
                with (self.state_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
                    handle.write(line)
            if self.mode == "machine" and project != MACHINE_TAG:
                self.host.log_project(project, line)
            if self.echo:
                with contextlib.suppress(Exception):
                    import sys
                    sys.stderr.write(line)
                    sys.stderr.flush()

    def log_machine(self, message: str) -> None:
        self.log(MACHINE_TAG if self.mode == "machine" else self.host.tag(), message)

    @staticmethod
    def store_key(setting) -> tuple:
        kwargs = setting.connect_kwargs()
        return (tuple(sorted((k, str(v)) for k, v in kwargs.items())), setting.schema)

    def link_for(self, setting) -> StoreLink:
        key = self.store_key(setting)
        link = self.links.get(key)
        if link is None:
            link = StoreLink(setting)
            self.links[key] = link
        return link

    def grooming(self) -> int:
        return sum(1 for slot in self.slots.values() if slot.groom_proc is not None)

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        return {"mode": self.mode, "pid": os.getpid(), "node": self.node,
                "started_at": self.started_at, "published_at": iso(now_utc()),
                "fingerprint": self.fingerprint, "reload_error": self.reload_error,
                "cap": {"max_grooms": self.settings.get("max_grooms"),
                        "running": self.grooming()},
                "stores": [link.row() for link in self.links.values()],
                "projects": [slot.row() for slot in sorted(self.slots.values(),
                                                            key=lambda s: s.slug)]}

    def publish(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_publish < PUBLISH_SECONDS:
            return
        self.last_publish = time.monotonic()
        self.changed = False
        with contextlib.suppress(OSError):
            write_atomic(self.state_dir / STATUS_FILE,
                         json.dumps(self.status(), indent=2, default=str))

    # -- the loop -------------------------------------------------------------

    def run(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        import fcntl
        handle = (self.state_dir / LOCK_FILE).open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("busy")
        try:
            (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
            self.settings = self.host.machine_settings()
            self.fingerprint = self.host.fingerprint()
            signal.signal(signal.SIGHUP, lambda *_: self.reload_requested.set())
            signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
            signal.signal(signal.SIGINT, lambda *_: self.stopping.set())
            self.log_machine(f"started ({self.mode} mode, pid {os.getpid()}, node {self.node}, "
                             f"max_grooms {self.settings.get('max_grooms')})")
            while not self.stopping.is_set():
                self.step()
                self.wait(self.until_next())
        finally:
            self.shutdown()
            with contextlib.suppress(OSError):
                (self.state_dir / PID_FILE).unlink()
            handle.close()

    def until_next(self) -> float:
        """How long until the next step: the follow interval, or sooner when a
        changed project's debounce ends before it."""
        seconds = FOLLOW_SECONDS
        now = time.monotonic()
        for slot in self.slots.values():
            if slot.plan is not None and slot.dirty_at is not None:
                seconds = min(seconds, slot.dirty_at + DEBOUNCE_SECONDS - now)
        return max(0.05, seconds)

    def wait(self, seconds: float) -> None:
        """Until `seconds` pass or the process is asked to stop."""
        self.stopping.wait(seconds)

    def step(self) -> None:
        if self.reload_requested.is_set():
            self.reload_requested.clear()
            self.reload()
        self.take_list()
        now = time.monotonic()
        for slot in list(self.slots.values()):
            if slot.plan is None and now >= slot.next_admit:
                try:
                    slot.admit()
                except Exception as exc:
                    slot.state, slot.reason = "error", f"{type(exc).__name__}: {exc}"
                    slot.log(f"not served: {slot.reason}")
                    slot.next_admit = time.monotonic() + ADMIT_SECONDS
            elif slot.plan is not None and now >= slot.next_check:
                slot.next_check = now + CHECK_SECONDS
                refusal = self.host.check(slot.entry)
                if refusal:
                    slot.state, slot.reason = refusal
                    slot.release(f"no longer served: {refusal[0]}: {refusal[1]}")
                    slot.next_admit = time.monotonic() + ADMIT_SECONDS
        self.follow()
        cap = int(self.settings.get("max_grooms") or 1)
        for slot in self.slots.values():
            if slot.reap_groom():
                pass
            if slot.plan is None:
                continue
            now = time.monotonic()
            if now >= slot.next_poll and slot.dirty_at is None:
                slot.poll()
            if slot.due(time.monotonic()):
                slot.render()
            if time.monotonic() >= slot.next_groom_check:
                if slot.consider_groom(self.grooming(), cap):
                    slot.start_groom()
        for link in self.links.values():
            link.release()
        self.publish(force=self.changed)

    def follow(self) -> None:
        """Read which projects every store's versions say changed, and mark
        them."""
        for link in list(self.links.values()):
            if not link.ensure(self.log_machine):
                continue
            try:
                moved = link.changes()
            except Exception as exc:
                if _connection_lost(exc):
                    link.lost(exc, self.log_machine)
                    continue
                # A store whose versions do not read leaves its projects to
                # the poll behind them.
                why = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
                if why != link.follow_error:
                    self.log_machine(f"store {link.setting.schema}: versions do not read, "
                                     f"polling: {why}")
                link.follow_error = why
                continue
            link.follow_error = None
            for slot in self.slots.values():
                if slot.project_id in moved and slot.link is link:
                    slot.dirty_at = slot.dirty_at or time.monotonic()

    def take_list(self) -> None:
        witness = self.host.list_witness()
        if witness == self.list_witness and self.slots:
            return
        self.list_witness = witness
        try:
            entries = self.host.entries()
        except Exception as exc:
            self.log_machine(f"the opt-in list does not load: {exc}")
            return
        for project_id in list(self.slots):
            if project_id not in entries:
                slot = self.slots.pop(project_id)
                slot.release("left the service")
        for project_id, entry in entries.items():
            if project_id not in self.slots:
                self.slots[project_id] = Slot(self, project_id, entry)
        self.publish(force=True)

    def reload(self) -> None:
        try:
            settings = self.host.machine_settings()
        except Exception as exc:
            self.reload_error = getattr(exc, "message", None) or str(exc)
            self.log_machine(f"reload rejected, the previous settings stay: {self.reload_error}")
        else:
            self.settings = settings
            self.reload_error = None
        for slot in self.slots.values():
            try:
                slot.reload()
            except Exception as exc:
                slot.reload_error = f"{type(exc).__name__}: {exc}"
                slot.log(f"reload failed: {slot.reload_error}")
        self.list_witness = None
        if self.reload_error is None:
            self.fingerprint = self.host.fingerprint()
        self.publish(force=True)

    def shutdown(self) -> None:
        for slot in list(self.slots.values()):
            slot.release("stopped")
        for link in self.links.values():
            link.drop()
        self.log_machine("stopped")
        with contextlib.suppress(OSError):
            (self.state_dir / STATUS_FILE).unlink()


def iter_listed_roots(config_home: Path) -> Iterable[tuple[str, str]]:
    """Every project root a machine service's opt-in list on this machine names,
    as (the list's file, the root): the projects `adopt` looks in by default.
    A list that does not read is passed over and said by the caller."""
    import tomllib
    for path in sorted(config_home.glob("*/service/projects.*")):
        if path.suffix not in (".json", ".toml"):
            continue
        try:
            raw = (json.loads(path.read_text()) if path.suffix == ".json"
                   else tomllib.loads(path.read_text()))
        except (OSError, ValueError):
            yield str(path), ""
            continue
        projects = raw.get("projects") if isinstance(raw, dict) else None
        if isinstance(projects, dict):
            for entry in projects.values():
                if isinstance(entry, dict) and isinstance(entry.get("root"), str):
                    yield str(path), entry["root"]
