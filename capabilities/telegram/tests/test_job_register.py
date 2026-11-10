#!/usr/bin/env python3
"""The job register: isolation, arrival order, amendment, and cancellation.

The register lives in Postgres. These cases read TELEGRAM_TEST_DSN, a throwaway
database's URL, and skip without one; each case binds a schema of its own, so
cases never see each other's rows - the live-session index spans every project.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = TELEGRAM_DIR / "service"
STORE_DSN = os.environ.get("TELEGRAM_TEST_DSN")
# Every key the database cascade reads, so a case can hide each one it inherits.
from capabilities_contract.db import KEYS as DB_KEYS  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _agents import register_agents  # noqa: E402
sys.path.pop(0)


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jobs = _module("telegram_service_jobs_test", SERVICE_DIR / "jobs.py")
channels = _module("telegram_service_channels_test", SERVICE_DIR / "channels.py")


def _step_id(step) -> str:
    """A register step's id, whether it is a (step_id, sql) pair or a Step."""
    return step.id if hasattr(step, "id") else step[0]


def project_id() -> str:
    return f"prj_test_{uuid.uuid4().hex}"


def no_db_env(config_home: str) -> dict:
    """The process environment without any database key, under `config_home`,
    so no database of the machine running the suite answers."""
    env = {k: v for k, v in os.environ.items() if k not in DB_KEYS}
    env["XDG_CONFIG_HOME"] = config_home
    return env


def store_setting():
    """The throwaway database as the store in force, as a service resolves it
    for a project that names none, bound to a schema of this case's own."""
    env = {**no_db_env(tempfile.mkdtemp()), "AGENTKIT_DB_URL": STORE_DSN}
    with mock.patch.dict(os.environ, env, clear=True):
        setting = jobs.store_setting(Path(tempfile.mkdtemp()))
    return dataclasses.replace(setting, schema=f"tgtest_{uuid.uuid4().hex}")


# Every name and comment in an owner's tables that still says project, where the
# store now says agent: table columns, relations, constraints, triggers, comments, and
# the bodies of the owner's functions.
_SAYS_PROJECT = """
SELECT 'column ' || c.relname || '.' || a.attname FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
   AND a.attname ~* 'project'
UNION ALL SELECT 'relation ' || c.relname FROM pg_class c
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND c.relname ~* 'project'
UNION ALL SELECT 'constraint ' || o.conname FROM pg_constraint o
  JOIN pg_class c ON c.oid = o.conrelid
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND o.conname ~* 'project'
UNION ALL SELECT 'trigger ' || g.tgname FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND g.tgname ~* 'project'
UNION ALL SELECT 'comment on ' || c.relname || coalesce('.' || a.attname, '')
  FROM pg_description d JOIN pg_class c ON c.oid = d.objoid AND d.classoid = 'pg_class'::regclass
  LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid AND d.objsubid > 0
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND d.description ~* 'project'
UNION ALL SELECT 'function ' || p.proname FROM pg_proc p
 WHERE p.pronamespace = %(schema)s::regnamespace AND p.proname LIKE %(like)s
   AND p.prosrc ~* 'project'
"""



@unittest.skipUnless(STORE_DSN, "TELEGRAM_TEST_DSN is unset")
class RegisterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.envelope = root / "capabilities"
        self.envelope.mkdir(parents=True)
        self.project_id = project_id()
        (self.envelope / "project.json").write_text(
            json.dumps({"slug": "testproject", "store": "db"}))
        (root / ".env.local").write_text(f"CAPABILITIES_PROJECT_ID={self.project_id}\n")
        self.setting = store_setting()
        self.store, self.reg = self.open()
        self.addCleanup(self.store.close)

    def open(self, envelope=None, **kwargs):
        """Another connection to the same store, as another process opens it,
        for an agent the store has registered."""
        store, reg = jobs.open_register(envelope or self.envelope,
                                        setting=self.setting, **kwargs)
        register_agents(store.conn, reg.project_id)
        return store, reg

    def register(self, reg=None, submit=True, **overrides):
        """A job most tests want in the queue. Registering opens a draft, so
        unless a test is about drafting it is submitted here."""
        payload = {"channel_key": "-100:7", "requested_by": "42",
                   "description": "reconcile the ledger", "engine": "codex"}
        payload.update(overrides)
        target = reg or self.reg
        row = target.register(**payload)
        if submit and row["state"] == jobs.DRAFT:
            return target.submit(row["id"])
        return row

    # -- an archived agent ---------------------------------------------------

    def test_no_job_is_entered_for_an_archived_agent(self):
        """Step 0011 refuses entering a job of an archived agent, SQLSTATE 23R01,
        which `archived` reads as nothing to start rather than a failure; a job
        already entered still moves and ends, and a restore lets it in again."""
        import psycopg
        waiting = self.register()

        def mark(on: bool) -> None:
            with self.store.transaction():
                self.store._execute(
                    "UPDATE agents SET archived_at = CASE WHEN ? THEN now() END, "
                    "archived_by = CASE WHEN ? THEN 'ops@n1' END WHERE id = ?",
                    (on, on, self.reg.project_id))

        mark(True)
        with self.assertRaises(psycopg.IntegrityError) as refused:
            self.register()
        self.assertEqual(refused.exception.sqlstate, "23R01")
        self.assertTrue(jobs.archived(refused.exception))
        self.assertFalse(jobs.archived(RuntimeError("anything else")))
        self.assertTrue(self.reg.request_stop(waiting["id"])["stop_requested"])
        self.assertEqual(len(self.reg.list(limit=10)), 1)
        mark(False)
        self.assertEqual(self.register()["state"], jobs.WAITING)

    # -- the channel a job reports into --------------------------------------

    def test_the_register_is_keyed_by_the_id_the_launcher_resolved(self):
        """The id is the one the telegram CLI resolved and passed in, not the
        project's own binding in .env.local, which stands in only without one."""
        resolved = project_id()
        resolved_store, reg = self.open(project_id=resolved)
        self.addCleanup(resolved_store.close)
        self.assertEqual(reg.project_id, resolved)
        self.assertEqual(self.reg.project_id, self.project_id)

    def test_a_project_without_an_id_is_refused(self):
        (self.envelope / "project.json").write_text(
            json.dumps({"slug": "testproject", "store": "db"}))
        (self.envelope.parent / ".env.local").unlink()
        with self.assertRaises(jobs.JobError) as caught:
            self.open()
        self.assertEqual(caught.exception.slug, "no_project_identity")

    # -- the store ------------------------------------------------------------

    def test_a_machine_without_a_store_is_refused(self):
        """No local default stands in: a queue nobody else can see is work
        that silently goes nowhere."""
        with tempfile.TemporaryDirectory() as config:
            with mock.patch.dict(os.environ, no_db_env(config), clear=True):
                with self.assertRaises(jobs.JobError) as caught:
                    jobs.open_register(self.envelope,
                                       project_root=self.envelope.parent)
        self.assertEqual(caught.exception.slug, "store_not_configured")

    def test_the_tables_are_the_capabilitys_own_under_its_ledger(self):
        ledger = self.store._execute(
            "SELECT step FROM schema_ledger WHERE owner = ? ORDER BY step",
            (jobs.STORE_OWNER,)).fetchall()
        self.assertEqual([row[0] for row in ledger],
                         [_step_id(step) for step in jobs.STORE_STEPS])
        version = self.store._execute(
            "SELECT major, minor FROM schema_version WHERE owner = ?",
            (jobs.STORE_OWNER,)).fetchone()
        self.assertEqual(tuple(version),
                         (jobs.STORE_SCHEMA_MAJOR, jobs.STORE_SCHEMA_MINOR))
        tables = {row[0] for row in self.store._execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name LIKE ?",
            ("telegram\\_%",)).fetchall()}
        self.assertEqual(tables, {"telegram_jobs", "telegram_job_amendments",
                                  "telegram_job_slots", "telegram_prompt_versions",
                                  "telegram_channels", "telegram_premig_m4_settings",
                                  "telegram_premig_m9"})

    def test_the_profile_column_is_an_additive_fourth_step(self):
        """A job carries the profile it runs on in a nullable column of its own,
        added by a step of its own after the three the register had: a row
        registered without one keeps none, and a store at the earlier version
        takes the step without losing a row."""
        self.assertEqual([_step_id(step) for step in jobs.STORE_STEPS][3], "0004-job-profile")
        self.assertEqual((jobs.STORE_SCHEMA_MAJOR, jobs.STORE_SCHEMA_MINOR), (3, 2))
        with_profile = self.register(engine="claude", profile="telegram-fast")
        without = self.register(engine="codex", model="gpt-5.5")
        self.assertEqual(self.reg.get(with_profile["id"])["profile"], "telegram-fast")
        self.assertIsNone(self.reg.get(without["id"])["profile"])
        self.assertEqual(self.reg.get(without["id"])["model"], "gpt-5.5")

        from capabilities_contract.db import connect, migrate
        older = dataclasses.replace(self.setting, schema=f"{self.setting.schema}_older")
        conn = connect(application_name="telegram", setting=older)
        self.addCleanup(conn.close)
        migrate(conn, jobs.STORE_OWNER, jobs.STORE_STEPS[:3], major=1, minor=0)
        register_agents(conn, self.project_id)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO telegram_jobs (id, project_id, environment, surface, "
                        "channel_key, requested_by, description, engine, state, "
                        "created_at, updated_at) VALUES ('old-1', %s, 'development', "
                        "'telegram', '123', '42', 'from before', 'codex', 'waiting', "
                        "'2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
                        (self.project_id,))
        conn.commit()
        # The later steps cross to major 2, which only the store upgrade applies.
        with mock.patch.dict(os.environ, {"CAPABILITIES_STORE_UPGRADE": "1"}):
            old_store, old_reg = jobs.open_register(self.envelope, setting=older)
        self.addCleanup(old_store.close)
        row = old_reg.get("old-1")
        self.assertEqual((row["description"], row["engine"], row["profile"]),
                         ("from before", "codex", None))

    def test_one_register_keeps_every_labelled_row_and_merges_collisions(self):
        """A store whose rows were kept apart by the retired environment label,
        some of them colliding once it is gone, is brought to one register by
        the store upgrade: every job, amendment, slot and channel is visible to
        a register opened after it, and nothing is deleted but a channel row
        merged into the one with the highest watermark."""
        from capabilities_contract.db import connect, migrate
        older = dataclasses.replace(self.setting, schema=f"{self.setting.schema}_labels")
        conn = connect(application_name="telegram", setting=older)
        self.addCleanup(conn.close)
        migrate(conn, jobs.STORE_OWNER, jobs.STORE_STEPS[:6], major=1, minor=3)
        pid = self.project_id
        register_agents(conn, pid)
        lease = "2026-11-04T00:10:00+00:00"

        def job(job_id, env, created, state="waiting", origin=None):
            conn.execute(
                "INSERT INTO telegram_jobs (id, project_id, environment, surface, "
                "channel_key, requested_by, origin_message_id, description, engine, "
                "state, created_at, updated_at) VALUES (%s, %s, %s, 'telegram', "
                "'-100:7', '42', %s, %s, 'codex', %s, %s, %s)",
                (job_id, pid, env, origin, f"{job_id} under {env}", state, created,
                 created))

        def slot(env, number, job_id):
            conn.execute(
                "INSERT INTO telegram_job_slots (project_id, environment, surface, slot, "
                "job_id, owner_id, attempt_token, lease_expires_at) VALUES (%s, %s, "
                "'telegram', %s, %s, 'daemon', 'token', %s)", (pid, env, number, job_id, lease))

        def channel(env, key, watermark, settings, state, updated):
            conn.execute(
                "INSERT INTO telegram_channels (project_id, environment, connection, "
                "channel_key, last_processed_message_id, settings, voice_echo_senders, "
                "state, updated_at) VALUES (%s, %s, 'main', %s, %s, %s::jsonb, "
                "%s::jsonb, %s::jsonb, %s)",
                (pid, env, key, watermark, json.dumps(settings),
                 json.dumps({f"{env}-echo": env}), json.dumps(state), updated))

        # One Telegram update registered under both labels; the later job loses
        # only its claim on the update.
        job("dev-origin", "development", "2026-11-01T00:00:00+00:00", origin="91")
        job("prod-origin", "production", "2026-11-02T00:00:00+00:00", origin="91")
        job("prod-waiting", "production", "2026-11-03T00:00:00+00:00")
        # Slot 0, and the first slot below zero, each held under both labels.
        for env in ("development", "production"):
            job(f"{env}-run", env, "2026-11-01T01:00:00+00:00", state="running")
            job(f"{env}-direct", env, "2026-11-01T02:00:00+00:00", state="running")
            slot(env, 0, f"{env}-run")
            slot(env, -1, f"{env}-direct")
        for env, job_id in (("development", "dev-origin"), ("production", "prod-origin")):
            conn.execute(
                "INSERT INTO telegram_job_amendments (id, project_id, environment, "
                "surface, job_id, text, state, created_at) VALUES (%s, %s, %s, "
                "'telegram', %s, %s, 'pending', '2026-11-03T00:00:00+00:00')",
                (f"amend-{env}", pid, env, job_id, f"also {env}"))
        # One channel held under both labels; production's watermark is higher.
        channel("development", "-100:7", 50,
                {"tail_size": 10, "dev_only": True},
                {"jobs": {"40": {"status": "queued"}}, "last_seen_message_id": 50},
                "2026-11-01T00:00:00+00:00")
        channel("production", "-100:7", 80,
                {"tail_size": 20, "prod_only": True},
                {"jobs": {"70": {"status": "queued"}}, "last_seen_message_id": 80},
                "2026-11-02T00:00:00+00:00")
        channel("production", "-100:8", 5, {}, {}, "2026-11-02T00:00:00+00:00")
        conn.commit()

        # The step drops what older releases read, so only the upgrade takes it.
        with self.assertRaises(jobs.JobError) as refused:
            jobs.open_register(self.envelope, setting=older)
        self.assertEqual(refused.exception.slug, "store_upgrade_needed")
        with mock.patch.dict(os.environ, {"CAPABILITIES_STORE_UPGRADE": "1"}):
            store, reg = jobs.open_register(self.envelope, setting=older)
        self.addCleanup(store.close)

        rows = {row["id"]: row for row in reg.list(limit=100)}
        self.assertEqual(set(rows), {
            "dev-origin", "prod-origin", "prod-waiting", "development-run",
            "production-run", "development-direct", "production-direct"})
        self.assertEqual(rows["dev-origin"]["origin_message_id"], "91")
        self.assertIsNone(rows["prod-origin"]["origin_message_id"])
        self.assertEqual(reg.next_waiting()["id"], "dev-origin")
        self.assertEqual(reg.register(channel_key="-100:7", requested_by="42",
                                      description="again", engine="codex",
                                      origin_message_id="91")["id"], "dev-origin")
        self.assertEqual(reg.pending_amendment("dev-origin"), ["also development"])
        self.assertEqual(reg.pending_amendment("prod-origin"), ["also production"])

        slots = dict(store._execute(
            "SELECT job_id, slot FROM telegram_job_slots WHERE agent_id = ?",
            (pid,)).fetchall())
        self.assertEqual(set(slots), {"development-run", "production-run",
                                      "development-direct", "production-direct"})
        self.assertEqual(sorted([slots["development-run"], slots["production-run"]]), [0, 1])
        self.assertEqual(sorted([slots["development-direct"], slots["production-direct"]]),
                         [-2, -1])

        held = channels.rows(store.conn, (pid, "main"))
        self.assertEqual(set(held), {"-100:7", "-100:8"})
        merged = held["-100:7"]
        self.assertEqual(merged["last_processed_message_id"], 80)
        self.assertEqual(merged["settings"],
                         {"tail_size": 20, "dev_only": True, "prod_only": True})
        self.assertEqual(merged["voice_echo_senders"],
                         {"development-echo": "development",
                          "production-echo": "production"})
        self.assertEqual(set(merged["jobs"]), {"40", "70"})
        self.assertEqual(merged["last_seen_message_id"], 80)

        labelled = store._execute(
            "SELECT table_name FROM information_schema.columns WHERE "
            "table_schema = current_schema() AND column_name = 'environment'").fetchall()
        self.assertEqual(labelled, [])
    def test_a_store_at_major_1_is_renamed_in_place_and_keeps_its_rows(self):
        """A store at 1.3 with a row in every table takes the agent names in
        place: the rows read under them, the prompts' table and everything
        named for it carry the versions name, the guard still holds, and a
        release that knows only major 1 is refused. Only the store upgrade
        crosses the major."""
        import psycopg
        from capabilities_contract.db import DbError, connect, migrate
        old = [step for step in jobs.STORE_STEPS
               if _step_id(step) not in ("0007-one-register", "0008-agent-names",
                                         "0009-connection-ids", "0010-keys",
                                         "0011-unarchived")]
        older = dataclasses.replace(self.setting, schema=f"{self.setting.schema}_m1")
        conn = connect(application_name="telegram", setting=older)
        self.addCleanup(conn.close)
        migrate(conn, jobs.STORE_OWNER, old, major=1, minor=3)
        pid = self.project_id
        register_agents(conn, pid)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO telegram_jobs (id, project_id, environment, surface, "
                        "channel_key, requested_by, description, engine, state, "
                        "created_at, updated_at) VALUES ('old-1', %s, 'development', "
                        "'telegram', '123', '42', 'from before', 'codex', 'running', "
                        "'2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')", (pid,))
            cur.execute("INSERT INTO telegram_job_amendments (id, project_id, environment, "
                        "surface, job_id, text, state, created_at) VALUES ('a-1', %s, "
                        "'development', 'telegram', 'old-1', 'more', 'pending', "
                        "'2026-10-01T00:00:00Z')", (pid,))
            cur.execute("INSERT INTO telegram_job_slots (project_id, environment, surface, "
                        "slot, job_id, owner_id, attempt_token, lease_expires_at) VALUES "
                        "(%s, 'development', 'telegram', 0, 'old-1', 'o', 't', "
                        "'2026-10-01T00:00:00Z')", (pid,))
            cur.execute("INSERT INTO telegram_prompts (project_id, key, body, created_by) "
                        "VALUES (%s, 'context', 'kept prompt', 'a@n') RETURNING id::text",
                        (pid,))
            prompt = cur.fetchone()[0]
            cur.execute("INSERT INTO telegram_channels (project_id, environment, connection, "
                        "channel_key, last_processed_message_id) VALUES "
                        "(%s, 'development', 'default', '123', 7)", (pid,))
        conn.commit()

        # Outside the store upgrade the major is not crossed; inside it, it is.
        with mock.patch.dict(os.environ):
            os.environ.pop("CAPABILITIES_STORE_UPGRADE", None)
            with self.assertRaises(jobs.JobError) as held:
                jobs.open_register(self.envelope, setting=older)
        self.assertEqual(held.exception.slug, "store_upgrade_needed")
        with mock.patch.dict(os.environ, {"CAPABILITIES_STORE_UPGRADE": "1"}):
            store, reg = jobs.open_register(self.envelope, setting=older)
        self.addCleanup(store.close)
        self.assertEqual(reg.get("old-1")["description"], "from before")
        self.assertEqual(reg.pending_amendment("old-1"), ["more"])
        version = store._execute("SELECT major, minor FROM schema_version WHERE owner = ?",
                                 (jobs.STORE_OWNER,)).fetchone()
        self.assertEqual(tuple(version), (3, 2))
        for table in ("telegram_jobs", "telegram_job_amendments", "telegram_job_slots",
                      "telegram_prompt_versions", "telegram_channels"):
            rows = store._execute(f"SELECT agent_id FROM {table}").fetchall()
            self.assertEqual([tuple(r) for r in rows], [(pid,)], table)
        raw = psycopg.connect(STORE_DSN, autocommit=True)
        self.addCleanup(raw.close)
        raw.execute(f'SET search_path TO "{older.schema}"')
        self.assertEqual(raw.execute(_SAYS_PROJECT, {"schema": older.schema,
                                                     "like": "telegram\\_%"}).fetchall(), [])
        self.assertEqual(raw.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = %s::regnamespace "
            "AND relname LIKE 'telegram\\_prompts%%' UNION ALL SELECT proname FROM pg_proc "
            "WHERE pronamespace = %s::regnamespace AND proname LIKE 'telegram\\_prompts%%' "
            "UNION ALL SELECT conname FROM pg_constraint WHERE connamespace = "
            "%s::regnamespace AND conname LIKE 'telegram\\_prompts%%' UNION ALL SELECT "
            "tgname FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
            "WHERE c.relnamespace = %s::regnamespace AND tgname LIKE 'telegram\\_prompts%%'",
            (older.schema,) * 4).fetchall(), [])
        with self.assertRaises(psycopg.errors.CheckViolation):
            raw.execute("UPDATE telegram_prompt_versions SET body = 'changed' "
                        "WHERE id = %s::uuid", (prompt,))
        with self.assertRaises(DbError) as refused:
            migrate(raw, jobs.STORE_OWNER, old, major=1, minor=3, schema=older.schema)
        self.assertEqual(refused.exception.slug, "schema_too_new")

    def test_a_store_at_major_2_takes_connection_ids_in_place_and_keeps_its_rows(self):
        """A store whose core and register are at major 2, its connections
        keyed by name, takes the ids in place under the store upgrade: a
        register row of a connection the store holds reads under its id and
        goes with it, one of a connection only files hold keeps its name and
        keeps working, a role's grant of an account names it by id with the
        connection beside it, and the grant that named no connection is kept in
        the pre-image and gone from the settings. A release that knows only
        major 2 is refused."""
        import psycopg
        import capabilities_contract.db._migrate as core_migrate
        from capabilities_contract.db import DbError, connect, deleting, migrate
        old = [step for step in jobs.STORE_STEPS
               if _step_id(step) not in ("0009-connection-ids", "0010-keys",
                                         "0011-unarchived")]
        older = dataclasses.replace(self.setting, schema=f"{self.setting.schema}_m2")
        conn = connect(application_name="telegram", setting=older)
        self.addCleanup(conn.close)
        # The store as the release before the cutover left it: the core at 2.0.
        with mock.patch.object(core_migrate, "CORE_STEPS", core_migrate.CORE_STEPS[:1]), \
                mock.patch.object(core_migrate, "CORE_VERSION", (2, 0)):
            migrate(conn, jobs.STORE_OWNER, old, major=2, minor=0)
        pid = self.project_id
        register_agents(conn, pid)
        role = ["authority", "roles", "ops", "allowed_capabilities"]
        with conn.cursor() as cur:
            for capability, name in (("telegram", "work"), ("telegram", "personal"),
                                     ("clickup", "tasks")):
                cur.execute("INSERT INTO connections (capability, name, spec) "
                            "VALUES (%s, %s, '{}'::jsonb)", (capability, name))
            cur.execute("INSERT INTO telegram_channels (agent_id, connection, channel_key, "
                        "last_processed_message_id) VALUES (%s, 'work', '-100:7', 7), "
                        "(%s, 'filed', '-100:8', 3)", (pid, pid))
            for path, value in (
                    (role + ["telegram", "connections", "personal", "allow_write"], True),
                    (role + ["clickup", "connections", "tasks", "allow_write"], False),
                    (role + ["telegram", "connections", "missing", "allow_write"], True),
                    (["connection"], "work")):
                cur.execute("INSERT INTO settings (agent_id, capability, path, value, "
                            "updated_by) VALUES (%s, 'telegram', %s, %s::jsonb, 'a@n')",
                            (pid, path, json.dumps(value)))
        conn.commit()

        with mock.patch.dict(os.environ):
            os.environ.pop("CAPABILITIES_STORE_UPGRADE", None)
            with self.assertRaises(jobs.JobError) as held:
                jobs.open_register(self.envelope, setting=older)
        self.assertEqual(held.exception.slug, "store_upgrade_needed")
        with mock.patch.dict(os.environ, {"CAPABILITIES_STORE_UPGRADE": "1"}):
            store, _reg = jobs.open_register(self.envelope, setting=older)
        self.addCleanup(store.close)
        version = store._execute("SELECT major, minor FROM schema_version WHERE owner = ?",
                                 (jobs.STORE_OWNER,)).fetchone()
        self.assertEqual(tuple(version), (3, 2))
        ids = dict(store._execute(
            "SELECT name, id FROM core_premig_connection_names").fetchall())
        self.assertEqual(set(ids), {"work", "personal", "tasks"})

        self.assertEqual(channels.rows(store.conn, (pid, ids["work"]))["-100:7"][
            "last_processed_message_id"], 7)
        self.assertEqual(channels.rows(store.conn, (pid, "filed"))["-100:8"][
            "last_processed_message_id"], 3)
        self.assertEqual(sorted(store._execute(
            "SELECT connection_id, file_connection, channel_key FROM telegram_channels"
        ).fetchall(), key=lambda r: r[2]), [(ids["work"], None, "-100:7"),
                                            (None, "filed", "-100:8")])
        # A row written now lands in the column its connection's holder names.
        channels.write(store.conn, (pid, ids["work"]), {"-100:9": {"last_processed_message_id": 1}})
        channels.write(store.conn, (pid, "filed"), {"-100:9": {"last_processed_message_id": 2}})
        self.assertEqual(store._execute(
            "SELECT connection_id, file_connection FROM telegram_channels "
            "WHERE channel_key = '-100:9' ORDER BY 1").fetchall(),
            [(ids["work"], None), (None, "filed")])
        self.assertEqual(store._execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = "
            "current_schema() AND table_name = 'telegram_channels' "
            "AND column_name LIKE 'connection%%'").fetchall(), [("connection_id",)])

        settings = {tuple(path): (value, cid) for path, value, cid in store._execute(
            "SELECT path, value, connection_id FROM settings WHERE agent_id = ? "
            "AND capability = 'telegram'", (pid,)).fetchall()}
        self.assertEqual(settings, {
            tuple(role + ["telegram", "connections", ids["personal"], "allow_write"]):
                (True, ids["personal"]),
            tuple(role + ["clickup", "connections", ids["tasks"], "allow_write"]):
                (False, ids["tasks"]),
            ("connection",): (ids["work"], ids["work"])})
        self.assertEqual([tuple(r[0]) for r in store._execute(
            "SELECT path FROM telegram_premig_m4_settings").fetchall()],
            [tuple(role + ["telegram", "connections", "missing", "allow_write"])])

        raw = psycopg.connect(STORE_DSN, autocommit=True)
        self.addCleanup(raw.close)
        raw.execute(f'SET search_path TO "{older.schema}"')
        with raw.transaction():
            deleting(raw, "connections", ids["work"])
            raw.execute("DELETE FROM connections WHERE id = %s", (ids["work"],))
        self.assertEqual(raw.execute(
            "SELECT file_connection, channel_key FROM telegram_channels ORDER BY 2").fetchall(),
            [("filed", "-100:8"), ("filed", "-100:9")])
        with self.assertRaises(DbError) as refused:
            migrate(raw, jobs.STORE_OWNER, old, major=2, minor=0, schema=older.schema)
        self.assertEqual(refused.exception.slug, "schema_too_new")

    def test_reopening_applies_nothing_twice(self):
        again_store, again = self.open()
        self.addCleanup(again_store.close)
        self.register()
        self.assertIsNotNone(again.next_waiting())

    def test_a_topic_key_round_trips(self):
        """The key is written by whoever registers the job and read back by the
        daemon delivering the answer. A shape that does not survive that trip
        strands a finished job: the row says nothing is wrong with it, and the
        channel it names cannot be resolved."""
        key = jobs.channel_key(-1001, 77)
        self.assertEqual(jobs.channel_identity(key), ("-1001", 77))

    def test_a_key_without_a_topic_is_the_chat_itself(self):
        self.assertEqual(jobs.channel_key(-1001), "-1001")
        self.assertEqual(jobs.channel_identity("-1001"), ("-1001", None))

    # -- drafting ------------------------------------------------------------

    def test_registering_opens_a_draft_no_runner_takes(self):
        """The gap between writing a job and handing it over is the point: a
        description arrives incomplete, and the turn that wrote it needs room
        to ask before a runner starts on it."""
        row = self.register(submit=False)
        self.assertEqual(row["state"], jobs.DRAFT)
        self.assertIsNone(self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=4))

    def test_submitting_a_draft_queues_it(self):
        row = self.register(submit=False)
        submitted = self.reg.submit(row["id"])
        self.assertEqual(submitted["state"], jobs.WAITING)
        claimed = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=4)
        self.assertEqual(claimed["id"], row["id"])

    def test_a_draft_takes_amendments_before_it_is_handed_over(self):
        row = self.register(submit=False)
        self.reg.stage_amendment(row["id"], "only the closed invoices")
        self.reg.amend(row["id"])
        self.assertEqual(self.reg.get(row["id"])["state"], jobs.DRAFT)
        self.assertEqual(self.reg.submit(row["id"])["state"], jobs.WAITING)

    def test_only_a_draft_is_submitted(self):
        row = self.register()
        with self.assertRaises(jobs.JobError) as refused:
            self.reg.submit(row["id"])
        self.assertEqual(refused.exception.slug, "job_not_draft")

    # -- identity and isolation ----------------------------------------------

    def test_project_isolates_the_queue(self):
        other = jobs.JobRegister(self.store, project_id())
        self.register()
        self.assertIsNone(other.next_waiting())

    def test_surface_isolates_the_queue(self):
        self.register()
        elsewhere = jobs.JobRegister(self.store, self.reg.project_id, surface="slack")
        self.assertIsNone(elsewhere.next_waiting())

    def test_a_register_without_a_project_is_refused(self):
        with self.assertRaises(jobs.JobError):
            jobs.JobRegister(self.store, None)

    # -- registration ---------------------------------------------------------

    def test_registration_lands_queued_with_the_operators_line(self):
        row = self.register(description="  move the invoice run to Friday  ")
        self.assertEqual(row["state"], jobs.WAITING)
        self.assertEqual(row["description"], "move the invoice run to Friday")
        self.assertEqual(row["requested_by"], "42")
        self.assertEqual(row["amendments"], 0)
        self.assertEqual(row["attempt"], 0)
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["started_at"])

    def test_a_job_without_a_description_is_refused(self):
        with self.assertRaises(jobs.JobError):
            self.register(description="   ")

    def test_an_unknown_engine_is_refused(self):
        with self.assertRaises(jobs.JobError):
            self.register(engine="gemini")

    def test_registration_is_idempotent_for_one_telegram_update(self):
        first = self.register(origin_message_id="91")
        again = self.register(origin_message_id="91", description="duplicate delivery")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.reg.list()), 1)
        self.assertEqual(again["description"], "reconcile the ledger")

    def test_registered_engine_and_model_are_pinned_together(self):
        row = self.register(engine="codex", model="gpt-channel")
        self.assertEqual((row["engine"], row["model"]),
                         ("codex", "gpt-channel"))

    def test_actor_scope_hides_and_refuses_another_actors_job(self):
        mine = self.register(requested_by="42")
        other = self.register(requested_by="99", description="other")
        self.assertEqual([r["id"] for r in self.reg.list(actor_id="42")],
                         [mine["id"]])
        self.assertIsNone(self.reg.get(other["id"], actor_id="42"))
        self.assertIsNone(self.reg.request_stop(other["id"], actor_id="42"))
        self.assertIsNone(self.reg.stage_amendment(
            other["id"], "take it over", actor_id="42"))
        self.assertEqual(self.reg.get(other["id"])["requested_by"], "99")

    # -- arrival order --------------------------------------------------------

    def test_the_runner_takes_the_oldest_queued_job(self):
        first = self.register(description="first")
        second = self.register(description="second")
        self.assertEqual(self.reg.next_waiting()["id"], first["id"])
        self.reg.start(first["id"])
        self.assertEqual(self.reg.next_waiting()["id"], second["id"])

    def test_a_claim_is_taken_once(self):
        row = self.register()
        self.assertIsNotNone(self.reg.start(row["id"]))
        self.assertIsNone(self.reg.start(row["id"]))

    def test_starting_counts_the_attempt_and_stamps_the_wait(self):
        row = self.reg.start(self.register()["id"], pid=4242, model="gpt-5.5")
        self.assertEqual(row["state"], jobs.RUNNING)
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["model"], "gpt-5.5")
        self.assertIsNotNone(row["started_at"])

    def test_claim_records_owner_pid_and_shared_slot(self):
        first = self.register(description="first")
        self.register(description="second")
        claimed = self.reg.claim_next(owner_id="daemon-a", owner_host="host-a",
                                      max_parallel=1, lease_seconds=30)
        self.assertEqual(claimed["id"], first["id"])
        self.assertTrue(claimed["attempt_token"])
        peer_store, peer = self.open()
        self.addCleanup(peer_store.close)
        self.assertIsNone(peer.claim_next(
            owner_id="daemon-b", owner_host="host-a", max_parallel=1),
            "the slot budget is shared by independent daemon connections")
        self.assertTrue(self.reg.attach_process(
            claimed["id"], claimed["attempt_token"], "daemon-a",
            pid=4242, pgid=4242))
        after = self.reg.get(claimed["id"])
        self.assertEqual((after["pid"], after["pgid"]), (4242, 4242))

    def test_stale_attempt_cannot_overwrite_a_newer_attempt(self):
        row = self.register()
        old = self.reg.claim_next(owner_id="old", owner_host="host-a", max_parallel=1)
        self.reg.stop(row["id"], jobs.INTERRUPTED,
                      attempt_token=old["attempt_token"], owner_id="old")
        self.reg.resume(row["id"])
        new = self.reg.claim_next(owner_id="new", owner_host="host-a", max_parallel=1)
        self.assertIsNone(self.reg.stop(
            row["id"], jobs.SUCCEEDED, attempt_token=old["attempt_token"],
            owner_id="old"))
        current = self.reg.get(row["id"])
        self.assertEqual(current["state"], jobs.RUNNING)
        self.assertEqual(current["attempt_token"], new["attempt_token"])

    def test_expired_attempt_fencing_uses_the_exact_observed_lease(self):
        row = self.register(description="first")
        self.register(description="second")
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1,
            lease_seconds=30)
        peer_store, peer = self.open()
        self.addCleanup(peer_store.close)
        expired = "2000-01-01T00:00:00.000000+00:00"
        with self.store.transaction():
            self.store._execute(
                "UPDATE telegram_jobs SET lease_expires_at = ? WHERE id = ?",
                (expired, row["id"]))
            self.store._execute(
                "UPDATE telegram_job_slots SET lease_expires_at = ? WHERE job_id = ?",
                (expired, row["id"]))
        stale = peer.get(row["id"])
        self.assertTrue(self.reg.renew(
            row["id"], running["attempt_token"], "daemon-a", lease_seconds=30))
        callbacks = []
        self.assertIsNone(peer.fence_expired_attempt(
            stale, "stale observer", before_release=lambda: callbacks.append(True)))
        self.assertEqual(callbacks, [], "a stale observer must not signal the process")
        self.assertEqual(peer.get(row["id"])["state"], jobs.RUNNING)
        self.assertIsNone(peer.claim_next(
            owner_id="daemon-b", owner_host="host-a", max_parallel=1),
            "a stale reconciler must not free the live attempt's slot")

    def test_expired_attempt_and_slot_are_fenced_atomically(self):
        row = self.register(description="first")
        second = self.register(description="second")
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        peer_store, peer = self.open()
        self.addCleanup(peer_store.close)
        expired = "2000-01-01T00:00:00.000000+00:00"
        with self.store.transaction():
            self.store._execute(
                "UPDATE telegram_jobs SET lease_expires_at = ? WHERE id = ?",
                (expired, row["id"]))
            self.store._execute(
                "UPDATE telegram_job_slots SET lease_expires_at = ? WHERE job_id = ?",
                ("2000-01-01T00:00:01.000000+00:00", row["id"]))
        observed = peer.get(row["id"])
        with self.assertRaises(jobs.JobError):
            peer.fence_expired_attempt(observed, "mismatched slot")
        self.assertEqual(self.reg.get(row["id"])["state"], jobs.RUNNING,
                         "slot mismatch rolls back the row transition")
        self.assertIsNone(peer.claim_next(
            owner_id="daemon-b", owner_host="host-a", max_parallel=1))
        with self.store.transaction():
            self.store._execute(
                "UPDATE telegram_job_slots SET lease_expires_at = ? WHERE job_id = ?",
                (expired, row["id"]))
        slots_during_fence = []

        def observe_slot():
            slots_during_fence.append(peer.store._execute(
                "SELECT COUNT(*) FROM telegram_job_slots WHERE job_id = ?",
                (row["id"],)).fetchone()[0])

        stopped = peer.fence_expired_attempt(
            observed, "owner expired", before_release=observe_slot)
        self.assertEqual(slots_during_fence, [1],
                         "the process is stopped before its slot becomes reusable")
        self.assertEqual(stopped["outcome"], jobs.INTERRUPTED)
        claimed = peer.claim_next(
            owner_id="daemon-b", owner_host="host-a", max_parallel=1)
        self.assertEqual(claimed["id"], second["id"])

    def test_only_current_attempt_owner_may_finish_stop_or_amend(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.request_stop(row["id"])
        self.assertIsNone(self.reg.stop(
            row["id"], jobs.CANCELLED, attempt_token=running["attempt_token"],
            owner_id="daemon-b"))
        self.assertEqual(self.reg.get(row["id"])["state"], jobs.RUNNING)
        self.assertIsNone(self.reg.claim_next(
            owner_id="daemon-b", owner_host="host-a", max_parallel=1))
        stopped = self.reg.stop(
            row["id"], jobs.CANCELLED, attempt_token=running["attempt_token"],
            owner_id="daemon-a")
        self.assertEqual(stopped["outcome"], jobs.CANCELLED)

        amended_row = self.register(description="amend me")
        amended = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.amend(amended_row["id"], "new context")
        self.assertIsNone(self.reg.finish_amendment(
            amended_row["id"], amended["attempt_token"], "daemon-b"))
        self.assertEqual(self.reg.get(amended_row["id"])["state"], jobs.RUNNING)
        self.assertEqual(self.reg.finish_amendment(
            amended_row["id"], amended["attempt_token"], "daemon-a")["state"],
            jobs.STOPPED,
            "without a resumable checkpoint amendment stays stopped")

    # -- the open-jobs list the dialogue worker reads -------------------------

    def test_open_jobs_are_this_channels_only(self):
        mine = self.register(channel_key="-100:7")
        self.register(channel_key="-100:9")
        done = self.register(channel_key="-100:7")
        self.reg.start(done["id"])
        self.reg.stop(done["id"], jobs.SUCCEEDED)
        listed = self.reg.open_jobs("-100:7")
        self.assertEqual([row["id"] for row in listed], [mine["id"]])

    def test_a_stopped_job_is_no_longer_in_flight(self):
        row = self.register()
        self.reg.start(row["id"])
        self.reg.stop(row["id"], jobs.QUOTA, error="usage limit reached")
        self.assertEqual(self.reg.open_jobs("-100:7"), [],
                         "in flight means waiting or running, nothing else")

    # -- amendment ------------------------------------------------------------

    def test_amendment_intent_waits_for_the_attempt_owner(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.update_attempt(
            row["id"], running["attempt_token"], "daemon-a", session_id="thread-1")
        amended = self.reg.amend(row["id"], "use the other account")
        self.assertEqual(amended["id"], row["id"])
        self.assertEqual(amended["session_id"], "thread-1")
        self.assertEqual(amended["amendments"], 1)
        self.assertEqual(amended["state"], jobs.RUNNING,
                         "a writer records intent but cannot steal the attempt")
        self.assertIsNone(self.reg.finish_amendment(
            row["id"], running["attempt_token"], "daemon-b"))
        amended = self.reg.finish_amendment(
            row["id"], running["attempt_token"], "daemon-a")
        self.assertEqual(amended["state"], jobs.WAITING)
        self.assertIsNone(amended["pid"])
        resumed = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.assertEqual(resumed["state"], jobs.RUNNING)
        self.assertEqual(resumed["attempt"], 2)

    def test_the_amendment_text_is_handed_over_once(self):
        """The text belongs in the session. Between the worker that wrote it and
        the runner that delivers it, it is a hand-off, not a register column."""
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.amend(row["id"], "not that account, the other one")
        self.reg.amend(row["id"], "and hold the invoice")
        claimed = self.reg.claim_amendments(
            row["id"], running["attempt_token"], "daemon-a")
        self.assertEqual([item["text"] for item in claimed],
                         ["not that account, the other one", "and hold the invoice"])
        self.assertEqual(self.reg.ack_amendments(
            row["id"], running["attempt_token"], "daemon-a"), 2)
        self.assertEqual(self.reg.pending_amendment(row["id"]), [])
        self.assertEqual(self.reg.get(row["id"])["amendments"], 2)

    def test_amendment_survives_claim_until_engine_ack(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.stage_amendment(row["id"], "keep this")
        claimed = self.reg.claim_amendments(
            row["id"], running["attempt_token"], "daemon-a")
        self.assertEqual([r["text"] for r in claimed], ["keep this"])
        self.assertEqual(self.reg.pending_amendment(row["id"]), ["keep this"])
        self.assertEqual(self.reg.amend_pending(), [],
                         "a claimed addition is already in this attempt")
        self.reg.stop(row["id"], jobs.INTERRUPTED,
                      attempt_token=running["attempt_token"], owner_id="daemon-a")
        self.reg.resume(row["id"])
        next_attempt = self.reg.claim_next(
            owner_id="daemon-b", owner_host="host-a", max_parallel=1)
        reclaimed = self.reg.claim_amendments(
            row["id"], next_attempt["attempt_token"], "daemon-b")
        self.assertEqual([r["text"] for r in reclaimed], ["keep this"])
        self.assertEqual(self.reg.ack_amendments(
            row["id"], next_attempt["attempt_token"], "daemon-b"), 1)
        self.assertEqual(self.reg.pending_amendment(row["id"]), [])

    def test_another_attempt_cannot_claim_or_ack_amendments(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.stage_amendment(row["id"], "keep this")
        self.assertEqual(self.reg.claim_amendments(
            row["id"], running["attempt_token"], "daemon-b"), [])
        self.assertEqual(self.reg.claim_amendments(
            row["id"], "another-token", "daemon-a"), [])
        claimed = self.reg.claim_amendments(
            row["id"], running["attempt_token"], "daemon-a")
        self.assertEqual([item["text"] for item in claimed], ["keep this"])
        self.assertEqual(self.reg.ack_amendments(
            row["id"], running["attempt_token"], "daemon-b"), 0)
        self.assertEqual(self.reg.pending_amendment(row["id"]), ["keep this"])

    def test_claude_amendment_without_checkpoint_does_not_replay_automatically(self):
        row = self.register(engine="claude")
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.amend(row["id"], "also compare the second source")
        stopped = self.reg.finish_amendment(
            row["id"], running["attempt_token"], "daemon-a")
        self.assertEqual((stopped["state"], stopped["outcome"]),
                         (jobs.STOPPED, jobs.INTERRUPTED))
        self.assertEqual(self.reg.pending_amendment(row["id"]),
                         ["also compare the second source"])
        self.assertIsNone(self.reg.next_waiting(),
                          "no checkpoint means no implicit replay of external work")
        self.assertEqual(self.reg.resume(row["id"])["state"], jobs.WAITING,
                         "continuation now requires an explicit resume")

    def test_amendment_count_is_user_additions_not_transition_batches(self):
        row = self.register()
        self.reg.stage_amendment(row["id"], "first")
        self.reg.stage_amendment(row["id"], "second")
        self.reg.amend(row["id"])
        self.assertEqual(self.reg.get(row["id"])["amendments"], 2)

    def test_an_amendment_is_not_kept_on_the_row(self):
        row = self.register()
        self.reg.amend(row["id"], "one more thing")
        self.assertNotIn("one more thing", json.dumps(self.reg.get(row["id"])))

    def test_two_concurrent_resumes_of_one_session_are_refused(self):
        first = self.register(description="first")
        second = self.register(description="second")
        self.reg.start(first["id"], session_id="thread-1")
        with self.assertRaises(Exception):
            self.reg.start(second["id"], session_id="thread-1")

    def test_sequential_resumes_of_one_session_are_allowed(self):
        row = self.register()
        self.reg.start(row["id"], session_id="thread-1")
        self.reg.stop(row["id"], jobs.SUCCEEDED)
        again = self.register(description="follow-up")
        self.assertIsNotNone(self.reg.start(again["id"], session_id="thread-1"))

    # -- stop and continue ----------------------------------------------------

    def test_every_stop_is_continuable(self):
        """The session is the checkpoint, so no outcome is a dead end. What a
        caller chooses is whether anybody asks the work to come back."""
        for outcome in jobs.OUTCOMES:
            row = self.register(description=outcome)
            self.reg.start(row["id"], session_id=f"thread-{outcome}")
            self.reg.stop(row["id"], outcome, error="why it stopped")
            stopped = self.reg.get(row["id"])
            self.assertEqual(stopped["state"], jobs.STOPPED)
            self.assertEqual(stopped["outcome"], outcome)
            self.assertIsNone(stopped["pid"])
            resumed = self.reg.resume(row["id"])
            self.assertEqual(resumed["state"], jobs.WAITING)
            self.assertIsNone(resumed["outcome"])
            self.assertIsNone(resumed["error"])
            self.assertEqual(resumed["session_id"], f"thread-{outcome}")

    def test_stopping_keeps_the_session_and_the_place_in_line(self):
        first = self.register(description="first")
        second = self.register(description="second")
        self.reg.start(first["id"], session_id="thread-1", pid=4242)
        self.assertEqual(self.reg.request_stop(first["id"])["stop_requested"], 1)
        self.assertEqual([r["id"] for r in self.reg.stop_pending()], [first["id"]])
        self.reg.stop(first["id"], jobs.CANCELLED, error="stopped by request")
        self.assertEqual(self.reg.get(first["id"])["stop_requested"], 0)
        self.assertEqual(self.reg.next_waiting()["id"], second["id"])
        self.reg.resume(first["id"])
        self.assertEqual(self.reg.next_waiting()["id"], first["id"],
                         "it arrived first and keeps that place")

    def test_resume_is_the_exact_counterpart_of_stop(self):
        """Both are asks about a direction, so both are idempotent and neither
        refuses a state. Resuming work that is already moving withdraws a stop
        nobody wants any more."""
        row = self.register()
        self.assertEqual(self.reg.resume(row["id"])["state"], jobs.WAITING)
        self.reg.start(row["id"])
        self.reg.request_stop(row["id"])
        resumed = self.reg.resume(row["id"])
        self.assertEqual(resumed["state"], jobs.RUNNING)
        self.assertEqual(resumed["stop_requested"], 0)
        self.assertEqual(self.reg.stop_pending(), [])
        self.reg.stop(row["id"], jobs.SUCCEEDED)
        self.assertEqual(self.reg.resume(row["id"])["state"], jobs.WAITING)

    def test_a_draft_can_be_discarded_and_nothing_else_can(self):
        """A draft holds no session and no work, so dropping it keeps nothing
        back. Anything that has started stops instead and stays continuable -
        discard is not a second way to end work, it is a way to unsay a line
        that every turn is told to read before submitting."""
        draft = self.register(submit=False)
        self.assertEqual(draft["state"], jobs.DRAFT)
        discarded = self.reg.discard(draft["id"])
        self.assertEqual(discarded["id"], draft["id"])
        self.assertIsNone(self.reg.get(draft["id"]))
        self.assertIsNone(self.reg.discard(draft["id"]),
                          "discarding twice is not an error, it is a no-op")

        started = self.register()
        self.reg.start(started["id"])
        self.assertIsNone(self.reg.discard(started["id"]))
        self.assertIsNotNone(self.reg.get(started["id"]),
                             "work that has started is stopped, never dropped")

    def test_an_unknown_outcome_is_refused(self):
        row = self.register()
        with self.assertRaises(jobs.JobError):
            self.reg.stop(row["id"], "abandoned")

    def test_the_daemon_continues_only_what_it_may_continue_unasked(self):
        """Quota and a restart are the daemon's own doing, so it undoes them.
        Everything else stopped for a reason a person has to read."""
        made = {}
        for outcome in jobs.OUTCOMES:
            row = self.register(description=outcome)
            self.reg.start(row["id"])
            self.reg.stop(row["id"], outcome)
            made[outcome] = row["id"]
        self.assertEqual(
            {r["outcome"] for r in self.reg.self_resuming()},
            {jobs.QUOTA, jobs.INTERRUPTED})

    def test_staged_text_is_itself_the_request_to_amend(self):
        """No column says an amendment is waiting: the text being there says it,
        and the runner reads exactly the rows it has to act on."""
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.update_attempt(
            row["id"], running["attempt_token"], "daemon-a", session_id="thread-1")
        self.assertEqual(self.reg.amend_pending(), [])
        self.reg.stage_amendment(row["id"], "not that account")
        self.assertEqual([r["id"] for r in self.reg.amend_pending()], [row["id"]])
        self.reg.amend(row["id"])
        self.assertEqual([r["id"] for r in self.reg.amend_pending()], [row["id"]],
                         "intent remains until the current owner stops the attempt")
        self.reg.finish_amendment(
            row["id"], running["attempt_token"], "daemon-a")
        self.assertEqual(self.reg.amend_pending(), [],
                         "a waiting job has nothing running to stop")

    def test_amending_stopped_work_brings_it_back(self):
        row = self.register()
        self.reg.start(row["id"], session_id="thread-1")
        self.reg.stop(row["id"], jobs.FAILED, error="the engine gave up")
        amended = self.reg.amend(row["id"], "try it the other way")
        self.assertEqual(amended["state"], jobs.WAITING)
        self.assertIsNone(amended["outcome"])
        self.assertEqual(self.reg.take_amendment(row["id"]),
                         ["try it the other way"])

    # -- outcome --------------------------------------------------------------

    def test_the_recorded_reason_is_kept_whole_enough_to_read(self):
        row = self.register()
        self.reg.start(row["id"])
        done = self.reg.stop(row["id"], jobs.FAILED, exit_code=1,
                             error="You've hit your usage limit.")
        self.assertEqual(done["error"], "You've hit your usage limit.")
        self.assertEqual(done["exit_code"], 1)
        self.assertIsNotNone(done["finished_at"])

    def test_execution_and_delivery_are_separate_durable_states(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        done = self.reg.stop(row["id"], jobs.SUCCEEDED,
                             attempt_token=running["attempt_token"],
                             owner_id="daemon-a",
                             result_text="the durable answer")
        self.assertEqual(done["outcome"], jobs.SUCCEEDED)
        self.assertEqual(done["delivery_state"], "pending")
        self.assertEqual(self.reg.pending_deliveries()[0]["result_text"],
                         "the durable answer")
        reopened_store, reopened = self.open()
        self.addCleanup(reopened_store.close)
        self.assertEqual(reopened.pending_deliveries()[0]["result_text"],
                         "the durable answer")
        claimed = reopened.claim_deliveries("sender-a")
        self.assertEqual([item["id"] for item in claimed], [row["id"]])
        delivered = reopened.finish_delivery(
            row["id"], claimed[0]["delivery_token"], "sender-a", delivered=True)
        self.assertEqual(delivered["delivery_state"], "delivered")
        self.assertEqual(self.reg.pending_deliveries(), [])

    def test_delivery_has_one_concurrent_sender(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.stop(row["id"], jobs.SUCCEEDED,
                      attempt_token=running["attempt_token"], owner_id="daemon-a",
                      result_text="durable result")
        peer_store, peer = self.open()
        self.addCleanup(peer_store.close)
        first = self.reg.claim_deliveries("sender-a", lease_seconds=30)
        self.assertEqual([item["id"] for item in first], [row["id"]])
        self.assertEqual(peer.claim_deliveries("sender-b", lease_seconds=30), [])
        self.assertIsNone(peer.finish_delivery(
            row["id"], first[0]["delivery_token"], "sender-b", delivered=True))
        self.assertEqual(self.reg.finish_delivery(
            row["id"], first[0]["delivery_token"], "sender-a", delivered=True
        )["delivery_state"], "delivered")

    def test_resume_cannot_discard_a_pending_result(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.stop(row["id"], jobs.SUCCEEDED,
                      attempt_token=running["attempt_token"], owner_id="daemon-a",
                      result_text="deliver this first")
        with self.assertRaises(jobs.JobError) as caught:
            self.reg.resume(row["id"])
        self.assertEqual(caught.exception.slug, "result_delivery_pending")
        after = self.reg.get(row["id"])
        self.assertEqual((after["state"], after["delivery_state"], after["result_text"]),
                         (jobs.STOPPED, "pending", "deliver this first"))
        self.assertEqual([item["id"] for item in self.reg.claim_deliveries("sender-a")],
                         [row["id"]])

    def test_amend_cannot_discard_a_claimed_delivery(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        self.reg.stop(row["id"], jobs.SUCCEEDED,
                      attempt_token=running["attempt_token"], owner_id="daemon-a",
                      result_text="deliver this first")
        delivery = self.reg.claim_deliveries("sender-a")[0]
        with self.assertRaises(jobs.JobError) as caught:
            self.reg.amend(row["id"], "one more thing")
        self.assertEqual(caught.exception.slug, "result_delivery_pending")
        after = self.reg.get(row["id"])
        self.assertEqual((after["delivery_state"], after["delivery_token"],
                          after["result_text"], after["amendments"]),
                         ("delivering", delivery["delivery_token"],
                          "deliver this first", 0))
        delivered = self.reg.finish_delivery(
            row["id"], delivery["delivery_token"], "sender-a", delivered=True)
        self.assertEqual(delivered["delivery_state"], "delivered")

    def test_old_active_job_is_not_hidden_by_newer_history(self):
        active = self.register(description="long running")
        self.reg.start(active["id"])
        for number in range(20):
            row = self.register(description=f"finished {number}")
            self.reg.start(row["id"])
            self.reg.stop(row["id"], jobs.SUCCEEDED)
        self.assertNotIn(active["id"], [row["id"] for row in self.reg.list(limit=20)])
        self.assertEqual([row["id"] for row in self.reg.active()], [active["id"]])

    def test_quota_resume_time_survives_register_reopen(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="daemon-a", owner_host="host-a", max_parallel=1)
        future = "2999-01-01T00:00:00.000000+00:00"
        self.reg.stop(row["id"], jobs.QUOTA,
                      attempt_token=running["attempt_token"], owner_id="daemon-a",
                      resume_at=future)
        self.assertEqual(self.reg.quota_until(), future)
        self.assertEqual(self.reg.self_resuming(), [])

    def test_counts_separate_what_is_moving_from_why_it_stopped(self):
        moving = self.register(description="moving")
        self.reg.start(moving["id"])
        done = self.register(description="done")
        self.reg.start(done["id"])
        self.reg.stop(done["id"], jobs.SUCCEEDED)
        self.register(description="queued")
        counts = self.reg.counts()
        self.assertEqual(counts["state"],
                         {jobs.WAITING: 1, jobs.RUNNING: 1, jobs.STOPPED: 1})
        self.assertEqual(counts["outcome"], {jobs.SUCCEEDED: 1})

    # -- restart ---    # -- restart --------------------------------------------------------------

    def test_running_rows_become_interrupted_on_start(self):
        row = self.register()
        running = self.reg.claim_next(
            owner_id="dead-daemon", owner_host="host-a", max_parallel=1)
        self.reg.attach_process(
            row["id"], running["attempt_token"], "dead-daemon", pid=1234)
        expired = "2000-01-01T00:00:00.000000+00:00"
        with self.store.transaction():
            self.store._execute(
                "UPDATE telegram_jobs SET lease_expires_at = ? WHERE id = ?",
                (expired, row["id"]))
            self.store._execute(
                "UPDATE telegram_job_slots SET lease_expires_at = ? WHERE job_id = ?",
                (expired, row["id"]))
        interrupted = self.reg.interrupt_active("service restarted")
        self.assertEqual([r["id"] for r in interrupted], [row["id"]])
        after = self.reg.get(row["id"])
        self.assertEqual(after["state"], jobs.STOPPED)
        self.assertEqual(after["outcome"], jobs.INTERRUPTED)
        self.assertIsNone(after["pid"])
        self.assertEqual(after["error"], "service restarted")

    def test_interrupted_work_can_be_continued_as_a_fresh_attempt(self):
        row = self.register()
        self.reg.start(row["id"])
        self.reg.stop(row["id"], jobs.INTERRUPTED, error="service restarted")
        self.assertEqual(self.reg.resume(row["id"])["state"], jobs.WAITING)
        self.assertEqual(self.reg.next_waiting()["id"], row["id"])


    # -- work outside the runner's budget --------------------------------------
    #
    # A task started on a call is recorded here and starts at once: its attempt
    # has an owner, a token, a lease and a slot row numbered below zero, which no
    # runner counts. Everything below is what the register has to do for it.

    DIRECT = jobs.direct_owner("host-a:1:1")

    def direct(self, **overrides):
        row = self.register(submit=False, channel_key="777", requested_by="777",
                            description="Asked on a call: look it up", **overrides)
        return self.reg.begin_direct(row["id"], owner_id=self.DIRECT,
                                     owner_host="host-a")

    def expire(self, job_id):
        past = "2000-01-01T00:00:00.000000+00:00"
        with self.store.transaction():
            self.store._execute(
                "UPDATE telegram_jobs SET lease_expires_at = ? WHERE id = ?",
                (past, job_id))
            self.store._execute(
                "UPDATE telegram_job_slots SET lease_expires_at = ? WHERE job_id = ?",
                (past, job_id))

    def slot_of(self, job_id):
        return self.store._execute(
            "SELECT slot, owner_id, attempt_token, lease_expires_at "
            "FROM telegram_job_slots WHERE job_id = ?", (job_id,)).fetchone()

    def test_a_direct_attempt_runs_without_waiting_or_a_runner_slot(self):
        row = self.direct()
        self.assertEqual(row["state"], jobs.RUNNING)
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["lease_owner"], self.DIRECT)
        self.assertIsNotNone(row["attempt_token"])
        # The one slot is still free for queued work, and the direct row is
        # never what the runner takes.
        queued = self.register(description="queued")
        claimed = self.reg.claim_next(owner_id="daemon", owner_host="host-a",
                                      max_parallel=1)
        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(self.reg.running(), 1)

    def test_a_direct_attempt_holds_the_slot_row_every_register_version_fences(self):
        # A register that knows nothing of direct attempts fences, renews and
        # stops an attempt by deleting or touching the slot row with its exact
        # owner, token and lease, and refuses one without it. Each direct
        # attempt carries that row, below zero where `claim_next` never looks.
        first, second = self.direct(), self.direct()
        for row in (first, second):
            slot, owner, token, lease = self.slot_of(row["id"])
            self.assertLess(int(slot), 0)
            self.assertEqual((owner, token, lease),
                             (self.DIRECT, row["attempt_token"], row["lease_expires_at"]))
        self.assertNotEqual(self.slot_of(first["id"])[0], self.slot_of(second["id"])[0])
        self.assertTrue(self.reg.renew(first["id"], first["attempt_token"], self.DIRECT))
        renewed = self.reg.get(first["id"])
        self.assertEqual(self.slot_of(first["id"])[3], renewed["lease_expires_at"])
        self.reg.stop(first["id"], jobs.SUCCEEDED, exit_code=0,
                      attempt_token=first["attempt_token"], owner_id=self.DIRECT,
                      result_silent=True)
        self.assertIsNone(self.slot_of(first["id"]))
        self.expire(second["id"])
        self.assertIsNotNone(self.reg.fence_expired_attempt(
            self.reg.expired_active()[0], "service restarted"))
        self.assertIsNone(self.slot_of(second["id"]))

    def test_only_a_direct_owner_may_begin_outside_the_slots(self):
        row = self.register(submit=False)
        with self.assertRaises(jobs.JobError) as caught:
            self.reg.begin_direct(row["id"], owner_id="daemon", owner_host="host-a")
        self.assertEqual(caught.exception.slug, "not_a_direct_owner")
        submitted = self.register()
        self.assertIsNone(self.reg.begin_direct(
            submitted["id"], owner_id=self.DIRECT, owner_host="host-a"))

    def test_a_direct_attempt_renews_and_lands(self):
        row = self.direct()
        self.assertTrue(self.reg.renew(row["id"], row["attempt_token"], self.DIRECT))
        landed = self.reg.stop(row["id"], jobs.SUCCEEDED, exit_code=0,
                               session_id="thread-1",
                               attempt_token=row["attempt_token"],
                               owner_id=self.DIRECT, result_silent=True)
        self.assertEqual(landed["state"], jobs.STOPPED)
        self.assertEqual(landed["outcome"], jobs.SUCCEEDED)
        self.assertEqual(landed["delivery_state"], "delivered")
        self.assertIsNone(landed["pid"])

    def test_an_expired_direct_attempt_is_fenced_with_its_notice(self):
        row = self.direct()
        self.reg.attach_process(row["id"], row["attempt_token"], self.DIRECT, pid=4321)
        self.expire(row["id"])
        observed = self.reg.expired_active()
        self.assertEqual([r["id"] for r in observed], [row["id"]])
        fenced = self.reg.fence_expired_attempt(
            observed[0], "service restarted", result_text="It was cut off.")
        self.assertEqual(fenced["state"], jobs.STOPPED)
        self.assertEqual(fenced["outcome"], jobs.INTERRUPTED)
        self.assertIsNone(fenced["pid"])
        self.assertEqual(fenced["result_text"], "It was cut off.")
        self.assertEqual(fenced["delivery_state"], "pending")
        self.assertEqual([r["id"] for r in self.reg.claim_deliveries("daemon")],
                         [row["id"]])

    def test_a_direct_row_is_listed_in_its_callers_channel_only(self):
        row = self.direct()
        self.assertEqual([r["id"] for r in self.reg.open_jobs("777", actor_id="777")],
                         [row["id"]])
        self.assertEqual(self.reg.open_jobs("888", actor_id="888"), [])
        self.assertEqual(self.reg.list(channel_key="-100:7", actor_id="777"), [])
        self.assertIsNone(self.reg.get(row["id"], actor_id="888"))


if __name__ == "__main__":
    unittest.main()
