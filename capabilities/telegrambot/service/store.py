"""The telegrambot tables in the project's store, and the one connection to them.

Every table is the `telegrambot` owner's, migrated under its ledger in the schema
the store setting names, and every row names the agent it belongs to
(`agent_id`, the project's declared id) and the connection - the bot - it is
about. No table keeps a conversation: Telegram is the only home of the messages.

  telegrambot_channels   per bot, chat and topic: the `/set` overrides, the last
                         message answered, the messages a turn has reserved and
                         has not answered yet, and how many turns in a row other
                         bots have started there.
  telegrambot_seen       per bot: the chats, topics and people it has seen, with
                         names and when - no text. The one way to learn where a
                         bot was added and who wrote to it.
  telegrambot_prompts    every saved version of an agent's prompts, one active
                         per key; history is immutable.
  telegrambot_jobs, telegrambot_job_amendments, telegrambot_job_slots
                         the work a turn hands to a background job: its brief,
                         its run and its result until delivered (service/jobs.py).
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

OWNER = "telegrambot"
MAJOR = 2
MINOR = 1

STEPS: list[tuple[str, str]] = [
    ("0001-channels", """
CREATE TABLE telegrambot_channels (
  agent_id text NOT NULL CHECK (agent_id <> ''),
  connection text NOT NULL CHECK (connection <> ''),
  chat_id bigint NOT NULL,
  topic_id bigint NOT NULL DEFAULT 0,
  overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_message_id bigint NOT NULL DEFAULT 0 CHECK (last_message_id >= 0),
  reserved bigint[] NOT NULL DEFAULT '{}',
  reserved_by text,
  reserved_at timestamptz,
  bot_turns integer NOT NULL DEFAULT 0 CHECK (bot_turns >= 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, connection, chat_id, topic_id));
COMMENT ON TABLE telegrambot_channels IS
  'The telegrambot service''s register: per bot, chat and topic, what decides whether a message is answered. No message text.';
COMMENT ON COLUMN telegrambot_channels.topic_id IS
  'The forum topic, 1 for General; 0 for a chat without topics.';
COMMENT ON COLUMN telegrambot_channels.overrides IS
  'The `/set` overrides of this channel, validated by the service on write.';
COMMENT ON COLUMN telegrambot_channels.last_message_id IS
  'The highest message number a turn here has finished with: anything at or below it is never answered again.';
COMMENT ON COLUMN telegrambot_channels.reserved IS
  'Message numbers a turn has taken and not finished; a restart finds them here and does not act on them twice.';
COMMENT ON COLUMN telegrambot_channels.bot_turns IS
  'Turns in a row started by other bots'' messages; a person''s message sets it back to 0.';
"""),
    ("0002-seen", """
CREATE TABLE telegrambot_seen (
  agent_id text NOT NULL CHECK (agent_id <> ''),
  connection text NOT NULL CHECK (connection <> ''),
  kind text NOT NULL CHECK (kind IN ('chat', 'topic', 'person')),
  peer_id bigint NOT NULL,
  topic_id bigint NOT NULL DEFAULT 0,
  title text,
  username text,
  chat_type text CHECK (chat_type IN ('private', 'group', 'supergroup', 'channel')),
  is_bot boolean NOT NULL DEFAULT false,
  member_status text,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, connection, kind, peer_id, topic_id));
COMMENT ON TABLE telegrambot_seen IS
  'Who and what a bot has seen: chats, forum topics and people, by Telegram id, with names - never message text.';
COMMENT ON COLUMN telegrambot_seen.peer_id IS
  'The chat id for a chat or a topic, the user id for a person.';
COMMENT ON COLUMN telegrambot_seen.member_status IS
  'For a chat: the bot''s own membership there as Telegram last reported it (member, administrator, left, kicked).';
"""),
    ("0003-prompts", """
CREATE TABLE telegrambot_prompts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id text NOT NULL CHECK (agent_id <> ''),
  key text NOT NULL CHECK (key ~ '^(bot|delegation|context[.][a-z0-9][a-z0-9_-]{0,63})$'),
  body text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL CHECK (created_by <> ''),
  note text);
CREATE UNIQUE INDEX telegrambot_prompts_active ON telegrambot_prompts (agent_id, key)
  WHERE active;
CREATE INDEX telegrambot_prompts_by_key ON telegrambot_prompts (agent_id, key, created_at DESC);
CREATE FUNCTION telegrambot_prompts_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'telegrambot_prompts keeps every version';
  END IF;
  IF new.id <> old.id OR new.agent_id <> old.agent_id OR new.key <> old.key
     OR new.body <> old.body OR new.created_at <> old.created_at
     OR new.created_by <> old.created_by OR new.note IS DISTINCT FROM old.note
     OR (new.active AND NOT old.active) THEN
    RAISE EXCEPTION 'a telegrambot_prompts version is immutable but for clearing active';
  END IF;
  RETURN new;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER telegrambot_prompts_guard BEFORE UPDATE OR DELETE ON telegrambot_prompts
  FOR EACH ROW EXECUTE FUNCTION telegrambot_prompts_guard();
COMMENT ON TABLE telegrambot_prompts IS
  'Every saved version of an agent''s telegrambot prompts (bot, delegation, context.<name>); one active per key, a save clears it on the version it replaces.';
"""),
]


# A connection is named by the id the store gave it, no longer by its name,
# which the core's step to 3.0 dropped after keeping each one in its map
# (`core_premig_connection_names`). Every row names its bot in one of two
# columns: `connection_id`, with a foreign key that removes the row with its
# connection, for a connection the store holds, and `file_connection` for one
# that only files hold (a files-mode project, or a machine's own connection
# lent to a store-mode project), whose rows keep working. Every case over the
# convenient case: the same shape as a node's `coolify_connection_id` /
# `coolify_address`. Each key and index takes whichever is set where the name
# was. The column a release of major 1 reads is gone, so this is major 2. It
# runs after the job tables (service/jobs.py) it converts too.
CUTOVER_STEPS: list[tuple[str, str]] = [
    ("0007-connection-ids", r"""
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['channels', 'seen', 'jobs', 'job_amendments', 'job_slots'] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN connection_id text, ADD COLUMN file_connection text',
                   'telegrambot_' || t);
    EXECUTE format(
      'UPDATE %I x SET connection_id = coalesce('
      '(SELECT m.id FROM core_premig_connection_names m'
      ' WHERE m.capability = ''telegrambot'' AND m.name = x.connection),'
      '(SELECT k.id FROM connections k'
      ' WHERE k.capability = ''telegrambot'' AND k.id = x.connection))', 'telegrambot_' || t);
    EXECUTE format('UPDATE %I SET file_connection = connection WHERE connection_id IS NULL',
                   'telegrambot_' || t);
    EXECUTE format('ALTER TABLE %I DROP COLUMN connection CASCADE', 'telegrambot_' || t);
    EXECUTE format(
      'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (connection_id) REFERENCES connections (id)'
      ' ON DELETE CASCADE, ADD CONSTRAINT %I CHECK (file_connection <> %L),'
      ' ADD CONSTRAINT %I CHECK (num_nonnulls(connection_id, file_connection) = 1)',
      'telegrambot_' || t, 'telegrambot_' || t || '_connection_id_fkey',
      'telegrambot_' || t || '_file_connection_check', '',
      'telegrambot_' || t || '_connection_check');
    EXECUTE format('COMMENT ON COLUMN %I.connection_id IS %L', 'telegrambot_' || t,
                   'The id of the connection - the bot - the row is about, where the store '
                   'holds it.');
    EXECUTE format('COMMENT ON COLUMN %I.file_connection IS %L', 'telegrambot_' || t,
                   'The id of the connection - the bot - the row is about, where only files '
                   'hold it; exactly one of connection_id and file_connection is set.');
  END LOOP;
END $$;
CREATE UNIQUE INDEX telegrambot_channels_key ON telegrambot_channels
  (agent_id, (coalesce(connection_id, file_connection)), chat_id, topic_id);
CREATE UNIQUE INDEX telegrambot_seen_key ON telegrambot_seen
  (agent_id, (coalesce(connection_id, file_connection)), kind, peer_id, topic_id);
CREATE INDEX telegrambot_jobs_queue ON telegrambot_jobs
  (agent_id, (coalesce(connection_id, file_connection)), state, created_at);
CREATE INDEX telegrambot_jobs_by_chat ON telegrambot_jobs
  (agent_id, (coalesce(connection_id, file_connection)), chat_id, topic_id, state);
CREATE UNIQUE INDEX telegrambot_jobs_by_origin ON telegrambot_jobs
  (agent_id, (coalesce(connection_id, file_connection)), chat_id, topic_id, origin_message_id)
  WHERE origin_message_id IS NOT NULL;
CREATE INDEX telegrambot_job_amendments_pending ON telegrambot_job_amendments
  (agent_id, (coalesce(connection_id, file_connection)), job_id, state, created_at);
CREATE UNIQUE INDEX telegrambot_job_slots_key ON telegrambot_job_slots
  (agent_id, (coalesce(connection_id, file_connection)), slot);
CREATE UNIQUE INDEX telegrambot_job_slots_job_key ON telegrambot_job_slots
  (agent_id, (coalesce(connection_id, file_connection)), job_id);
"""),
]

# A row names its bot by id in `connection_id` where the store holds that
# connection, else in `file_connection`; every key and read takes whichever is
# set. A write gives the id three times: `_HELD` asks the store whether it
# holds it, and `file_connection` keeps it only where the store does not.
CONNECTION = "coalesce(connection_id, file_connection)"
_HELD = ("(SELECT k.id FROM connections k WHERE k.id = %s"
         " AND k.capability = 'telegrambot')")
CONNECTION_VALUES = f"{_HELD}, nullif(%s, {_HELD})"


def connection_values(cid: str) -> tuple:
    """The parameters `CONNECTION_VALUES` takes for connection `cid`."""
    return (cid, cid, cid)

class StoreUnavailable(Exception):
    """The store cannot be used; `slug` is the shared library's word for why."""

    def __init__(self, slug: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.slug, self.message, self.hint = slug, message, hint


# The store's foreign keys (core 4.0): every agent and node column keyed by what
# its parent takes with it, a job's slots and amendments by their job, and the
# prompts guard admitting the cascade of a deliberate delete and nothing else.
KEYS_STEPS: list[tuple[str, str]] = [
    ("0008-keys", r"""
-- telegrambot: every agent and node reference (and every key inside telegrambot) a foreign key in its
-- class; guards admit the cascade of a deliberate delete and nothing else.
CREATE OR REPLACE FUNCTION telegrambot_prompts_guard() RETURNS trigger
    LANGUAGE plpgsql AS $guard$

DECLARE
    admitted boolean := false;
    BEGIN
    IF tg_op IN ('DELETE', 'UPDATE')
       AND coalesce(current_setting('store.deleting_agent', true), '')
           || coalesce(current_setting('store.deleting_node', true), '')
           || coalesce(current_setting('store.deleting_connection', true), '') <> '' THEN
        EXECUTE format('SELECT %I.store_state_cascade($1, $2, $3, $4)', tg_table_schema)
            INTO admitted USING tg_table_name, tg_op, to_jsonb(old),
            CASE WHEN tg_op = 'UPDATE' THEN to_jsonb(new) END;
        IF admitted THEN
            RETURN CASE WHEN tg_op = 'DELETE' THEN old ELSE new END;
        END IF;
    END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'telegrambot_prompts keeps every version';
  END IF;
  IF new.id <> old.id OR new.agent_id <> old.agent_id OR new.key <> old.key
     OR new.body <> old.body OR new.created_at <> old.created_at
     OR new.created_by <> old.created_by OR new.note IS DISTINCT FROM old.note
     OR (new.active AND NOT old.active) THEN
    RAISE EXCEPTION 'a telegrambot_prompts version is immutable but for clearing active';
  END IF;
  RETURN new;
END;
$guard$;
SELECT store_state_keys('telegrambot', ARRAY[
    ARRAY['telegrambot_channels', 'agent_id', 'agents', 'cascade'],
    ARRAY['telegrambot_seen', 'agent_id', 'agents', 'cascade'],
    ARRAY['telegrambot_jobs', 'agent_id', 'agents', 'cascade'],
    ARRAY['telegrambot_jobs', 'node', 'nodes', 'set null'],
    ARRAY['telegrambot_job_slots', 'agent_id', 'agents', 'cascade'],
    ARRAY['telegrambot_job_slots', 'job_id', 'telegrambot_jobs', 'cascade'],
    ARRAY['telegrambot_job_amendments', 'agent_id', 'agents', 'cascade'],
    ARRAY['telegrambot_job_amendments', 'job_id', 'telegrambot_jobs', 'cascade'],
    ARRAY['telegrambot_prompts', 'agent_id', 'agents', 'cascade']]);
"""),
]


def database():
    try:
        from capabilities_contract import db
    except ImportError as exc:  # pragma: no cover - the script header pins it
        raise StoreUnavailable("driver_missing", "the shared database library is not "
                               "installed", "run telegrambot through its own script header") from exc
    return db


def resolve_store(root: Path | None):
    """The store setting a process standing in `root` uses. Read only."""
    db = database()
    try:
        return db.resolve_setting(root)
    except db.DbError as exc:
        raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc


def steps() -> list:
    """Every step this capability migrates, the job tables included."""
    from importlib import util
    path = Path(__file__).resolve().parent / "jobs.py"
    extra: list = []
    if path.is_file():
        spec = util.spec_from_file_location("telegrambot_jobs_steps", path)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        extra = list(getattr(module, "STEPS", []))
    step = database().Step
    return (STEPS + extra + [step(sid, sql, core=(3, 0)) for sid, sql in CUTOVER_STEPS]
            + [step(sid, sql, core=(4, 0)) for sid, sql in KEYS_STEPS])


class Store:
    """The project's store. Each operation borrows one transaction connection and
    gives it back; the process holds none between operations."""

    def __init__(self, setting):
        self.setting = setting
        self.conn = None
        self.lock = threading.RLock()
        self._depth = 0

    def _connect(self):
        db = database()
        try:
            conn = db.connect(application_name=OWNER, setting=self.setting, transaction=True)
        except db.DbError as exc:
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
        conn.autocommit = True
        return conn

    def open(self) -> "Store":
        """Reach the store once and bring the tables to this code's version."""
        db = database()
        with self._operation() as conn:
            try:
                conn.autocommit = False
                with contextlib.suppress(ImportError):
                    # The harness runner's profile table, which the service reads.
                    from callva import harness_runner as lib
                    db.migrate(conn, lib.PROFILE_STORE_OWNER, lib.PROFILE_STORE_STEPS,
                               major=lib.PROFILE_STORE_MAJOR, minor=lib.PROFILE_STORE_MINOR)
                db.migrate(conn, OWNER, steps(), major=MAJOR, minor=MINOR)
                conn.autocommit = True
            except db.DbError as exc:
                raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc
        return self

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                with contextlib.suppress(Exception):
                    self.conn.close()
                self.conn = None

    @contextlib.contextmanager
    def _operation(self):
        with self.lock:
            if self.conn is not None and (self.conn.closed or self.conn.broken):
                self.close()
            if self.conn is None:
                self.conn = self._connect()
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self.close()

    def execute(self, sql: str, params=(), *, fetch: str | None = None):
        db = database()
        try:
            with self._operation() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    row = cur.fetchone()
                    return _row(cur, row) if row is not None else None
                if fetch == "all":
                    return [_row(cur, r) for r in cur.fetchall()]
                return cur.rowcount
        except db.DbError as exc:
            raise StoreUnavailable(exc.slug, exc.message, exc.hint) from exc

    @contextlib.contextmanager
    def transaction(self):
        with self._operation() as conn, conn.transaction(), conn.cursor() as cur:
            yield cur

    # -- seen ------------------------------------------------------------------

    def see(self, agent: str, connection: str, kind: str, peer_id: int, topic_id: int = 0, *,
            title=None, username=None, chat_type=None, is_bot=False,
            member_status=None) -> bool:
        """Record a sighting; the row changes only where a value moved or the last
        sighting is over an hour old. True when a row was written."""
        return bool(self.execute(
            """
            INSERT INTO telegrambot_seen AS s (agent_id, connection_id, file_connection, kind,
                peer_id, topic_id, title, username, chat_type, is_bot, member_status)
            VALUES (%s, """ + CONNECTION_VALUES + """, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id, (coalesce(connection_id, file_connection)), kind, peer_id, topic_id) DO UPDATE SET
                title = coalesce(excluded.title, s.title),
                username = coalesce(excluded.username, s.username),
                chat_type = coalesce(excluded.chat_type, s.chat_type),
                is_bot = excluded.is_bot,
                member_status = coalesce(excluded.member_status, s.member_status),
                last_seen_at = now()
            WHERE s.title IS DISTINCT FROM coalesce(excluded.title, s.title)
               OR s.username IS DISTINCT FROM coalesce(excluded.username, s.username)
               OR s.chat_type IS DISTINCT FROM coalesce(excluded.chat_type, s.chat_type)
               OR s.is_bot IS DISTINCT FROM excluded.is_bot
               OR s.member_status IS DISTINCT FROM coalesce(excluded.member_status, s.member_status)
               OR s.last_seen_at < now() - interval '1 hour'
            """, (agent, *connection_values(connection), kind, peer_id, topic_id, title,
                  username, chat_type,
                  bool(is_bot), member_status)))

    def seen(self, agent: str, connection: str, kind: str | None = None) -> list[dict]:
        clause, params = "", [agent, connection]
        if kind:
            clause, params = " AND kind = %s", params + [kind]
        return self.execute(
            "SELECT kind, peer_id, topic_id, title, username, chat_type, is_bot, member_status,"
            " first_seen_at, last_seen_at FROM telegrambot_seen"
            f" WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s{clause}"
            " ORDER BY kind, last_seen_at DESC", tuple(params), fetch="all")

    def seen_chat(self, agent: str, connection: str, chat_id: int) -> dict | None:
        return self.execute(
            "SELECT kind, peer_id, topic_id, title, username, chat_type, is_bot, member_status"
            " FROM telegrambot_seen WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND kind = 'chat'"
            " AND peer_id = %s", (agent, connection, chat_id), fetch="one")

    # -- channels --------------------------------------------------------------

    def channel(self, agent: str, connection: str, chat_id: int, topic_id: int = 0) -> dict:
        row = self.execute(
            "SELECT overrides, last_message_id, reserved, reserved_by, bot_turns"
            " FROM telegrambot_channels WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
            " AND chat_id = %s AND topic_id = %s",
            (agent, connection, chat_id, topic_id), fetch="one")
        return row or {"overrides": {}, "last_message_id": 0, "reserved": [],
                       "reserved_by": None, "bot_turns": 0}

    def channels(self, agent: str, connection: str) -> list[dict]:
        return self.execute(
            "SELECT chat_id, topic_id, overrides, last_message_id, reserved, bot_turns,"
            " updated_at FROM telegrambot_channels WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
            " ORDER BY updated_at DESC", (agent, connection), fetch="all")

    def reserve(self, agent: str, connection: str, chat_id: int, topic_id: int,
                message_id: int, holder: str) -> bool:
        """Take one message for a turn, before anything is done with it. False when
        it is already answered or already taken: then nothing more is done."""
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO telegrambot_channels (agent_id, connection_id, file_connection,"
                f" chat_id, topic_id) VALUES (%s, {CONNECTION_VALUES}, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (agent, *connection_values(connection), chat_id, topic_id))
            cur.execute(
                "SELECT last_message_id, reserved FROM telegrambot_channels"
                " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND chat_id = %s AND topic_id = %s"
                " FOR UPDATE", (agent, connection, chat_id, topic_id))
            last, reserved = cur.fetchone()
            if message_id <= last or message_id in (reserved or []):
                return False
            cur.execute(
                "UPDATE telegrambot_channels SET reserved = array_append(reserved, %s),"
                " reserved_by = %s, reserved_at = now(), updated_at = now()"
                " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND chat_id = %s AND topic_id = %s",
                (message_id, holder, agent, connection, chat_id, topic_id))
            return True

    def finish(self, agent: str, connection: str, chat_id: int, topic_id: int,
               message_ids: list[int], *, bot_turn: bool | None = None) -> None:
        """A turn is done with these messages: they leave the reservation and the
        watermark moves past them, in one commit. `bot_turn` True counts a turn
        another bot started, False (a person's) sets the count back to 0."""
        top = max(message_ids) if message_ids else 0
        count = ("bot_turns + 1" if bot_turn else "0") if bot_turn is not None else "bot_turns"
        self.execute(
            "UPDATE telegrambot_channels SET"
            " reserved = ARRAY(SELECT x FROM unnest(reserved) x WHERE NOT x = ANY(%s::bigint[])),"
            f" last_message_id = greatest(last_message_id, %s), bot_turns = {count},"
            " updated_at = now()"
            " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND chat_id = %s AND topic_id = %s",
            (list(message_ids), top, agent, connection, chat_id, topic_id))

    def reset_bot_turns(self, agent: str, connection: str, chat_id: int,
                        topic_id: int) -> None:
        """A person wrote here: the count of bot-started turns starts again."""
        self.execute(
            "UPDATE telegrambot_channels SET bot_turns = 0, updated_at = now()"
            " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND chat_id = %s AND topic_id = %s"
            " AND bot_turns > 0", (agent, connection, chat_id, topic_id))

    def release(self, agent: str, connection: str, chat_id: int, topic_id: int,
                message_ids: list[int]) -> None:
        """Hand reserved messages back unanswered (the turn never started)."""
        self.execute(
            "UPDATE telegrambot_channels SET"
            " reserved = ARRAY(SELECT x FROM unnest(reserved) x WHERE NOT x = ANY(%s::bigint[])),"
            " updated_at = now()"
            " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND chat_id = %s AND topic_id = %s",
            (list(message_ids), agent, connection, chat_id, topic_id))

    def stale_reservations(self, agent: str, connection: str) -> list[dict]:
        """Channels holding reservations: what a restart found taken and not
        finished. They are cleared, not acted on again."""
        return self.execute(
            "SELECT chat_id, topic_id, reserved FROM telegrambot_channels"
            " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND cardinality(reserved) > 0",
            (agent, connection), fetch="all")

    def set_override(self, agent: str, connection: str, chat_id: int, topic_id: int,
                     key: str, value) -> dict:
        """Set (or, with None, clear) one `/set` override; the overrides after it."""
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO telegrambot_channels (agent_id, connection_id, file_connection,"
                f" chat_id, topic_id) VALUES (%s, {CONNECTION_VALUES}, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (agent, *connection_values(connection), chat_id, topic_id))
            if value is None:
                cur.execute(
                    "UPDATE telegrambot_channels SET overrides = overrides - %s,"
                    " updated_at = now() WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s"
                    " AND chat_id = %s AND topic_id = %s RETURNING overrides",
                    (key, agent, connection, chat_id, topic_id))
            else:
                cur.execute(
                    "UPDATE telegrambot_channels SET overrides = overrides ||"
                    " jsonb_build_object(%s::text, %s::jsonb), updated_at = now()"
                    " WHERE agent_id = %s AND coalesce(connection_id, file_connection) = %s AND chat_id = %s"
                    " AND topic_id = %s RETURNING overrides",
                    (key, json.dumps(value), agent, connection, chat_id, topic_id))
            return cur.fetchone()[0] or {}

    # -- prompts ---------------------------------------------------------------

    def prompt(self, agent: str, key: str) -> dict | None:
        return self.execute(
            "SELECT id::text AS id, key, body, created_at, created_by, note"
            " FROM telegrambot_prompts WHERE agent_id = %s AND key = %s AND active",
            (agent, key), fetch="one")

    def prompts(self, agent: str) -> list[dict]:
        return self.execute(
            "SELECT id::text AS id, key, length(body) AS chars, created_at, created_by"
            " FROM telegrambot_prompts WHERE agent_id = %s AND active ORDER BY key",
            (agent,), fetch="all")

    def prompt_history(self, agent: str, key: str) -> list[dict]:
        return self.execute(
            "SELECT id::text AS id, active, length(body) AS chars, created_at, created_by, note"
            " FROM telegrambot_prompts WHERE agent_id = %s AND key = %s"
            " ORDER BY created_at DESC", (agent, key), fetch="all")

    def save_prompt(self, agent: str, key: str, body: str, by: str, *,
                    base: str | None = None, note: str | None = None) -> dict:
        """Add a version and make it active, clearing the one it replaces, in one
        transaction. `base`, where given, must name the active version (its id or a
        prefix of six or more), else PromptConflict."""
        with self.transaction() as cur:
            cur.execute(
                "SELECT id::text FROM telegrambot_prompts WHERE agent_id = %s AND key = %s"
                " AND active FOR UPDATE", (agent, key))
            found = cur.fetchone()
            current = found[0] if found else None
            if base is not None and not (current and current.startswith(base.lower())):
                raise PromptConflict(current)
            if current:
                cur.execute("UPDATE telegrambot_prompts SET active = false WHERE id = %s",
                            (current,))
            cur.execute(
                "INSERT INTO telegrambot_prompts (agent_id, key, body, created_by, note)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id::text, created_at",
                (agent, key, body, by, note))
            new_id, created = cur.fetchone()
        return {"id": new_id, "key": key, "replaced": current, "created_at": created}

    def retire_prompt(self, agent: str, key: str) -> str | None:
        """Clear the active version; the versions stay. The id it retired."""
        row = self.execute(
            "UPDATE telegrambot_prompts SET active = false WHERE agent_id = %s AND key = %s"
            " AND active RETURNING id::text AS id", (agent, key), fetch="one")
        return row["id"] if row else None


class PromptConflict(Exception):
    def __init__(self, current: str | None):
        super().__init__(current)
        self.current = current


def _row(cur, row) -> dict:
    return {d.name: v for d, v in zip(cur.description, row)}
