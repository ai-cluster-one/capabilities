#!/usr/bin/env python3
"""Jobs: the work a dialogue turn hands over, the runner that does it, and the
answer that comes back quoting the message that asked for it.

The harness runner's `run` is stood in for, so no model is reached. The
store-backed cases need a throwaway Postgres named by WHATSAPP_TEST_DSN and
skip without one; the cases that resolve profiles need callva-harness-runner.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_dialogue as td  # noqa: E402

wa, dialogue, profiles = td.wa, td.dialogue, td.profiles
jobs = wa._service_module("jobs")
schema = wa._service_schema()
SHIM = wa._service_bundle_dir() / "service" / "worker-bin" / "whatsapp"
ALICE, ALICE_JID, OWN, OWN_PHONE = td.ALICE, td.ALICE_JID, td.OWN, td.OWN_PHONE
DOCS = {"context": "CONTEXT: be brief.", "delegation": "DELEGATION: hand work off.",
        "job-worker": "JOB WORKER: do the work."}


class JobRun:
    """The runner's `run` for jobs: a session per job, held open while
    `block` is set until cancelled or released, and answering as told."""

    def __init__(self, answer="=== REPLY ===\nThe result.", *, block=False, fail=None):
        self.answer, self.block, self.fail = answer, block, fail
        self.calls: list[dict] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, prompt, profile, cwd, *, session, environ, extra_env, cancel,
                 on_start):
        sid = session[1] if session[0] == "resume" else f"sess-{uuid.uuid4().hex[:8]}"
        self.calls.append({"prompt": prompt, "session": session, "cancel": cancel,
                           "extra_env": dict(extra_env), "sid": sid})
        on_start(types.SimpleNamespace(pid=4242, harness="claude", session_id=sid))
        self.started.set()
        if self.block:
            while not cancel.is_set() and not self.release.is_set():
                cancel.wait(0.02)
            if cancel.is_set():
                return types.SimpleNamespace(ok=False, answer="", session_id=sid,
                                             failure=types.SimpleNamespace(
                                                 kind="cancelled", message="cancelled"))
        if self.fail:
            return types.SimpleNamespace(ok=False, answer="", session_id=sid,
                                         failure=types.SimpleNamespace(
                                             kind=self.fail, message=f"{self.fail} said no"))
        return types.SimpleNamespace(ok=True, answer=self.answer, session_id=sid,
                                     failure=None)


class JobsCase(td.DialogueCase):
    def setUp(self):
        super().setUp()
        records = types.SimpleNamespace(
            document_read=lambda cap, key: {"body": DOCS[key]} if key in DOCS else None)
        patcher = mock.patch.object(wa, "_records", return_value=records)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.killed: list[int] = []

    def store(self):
        db = wa._open_store(self.cfg)
        self.addCleanup(db.close)
        return db

    def make_jobs(self, settings=None, run=None, turn=None, owner_id=None):
        d = self.make(settings, run=turn)
        self.jrun = run or JobRun()
        runner = jobs.JobRunner(
            wa._CliFacade(), dialogue=d, db=self.store(), run=self.jrun,
            sessions=(lambda: ("fresh",), lambda sid: ("resume", sid)),
            kill_group=self.killed.append, poll=0.05, owner_id=owner_id)
        d.jobs = runner
        self.addCleanup(runner.shutdown, 5.0)
        return d, runner

    def submit(self, runner, *, chat=ALICE_JID, text="count the files",
               requester=ALICE, origin=True):
        msg = self.capture(td._msg(chat_id=chat, text=text)) if origin else None
        row = runner.register.register(
            chat_id=chat, requested_by=requester, description=text,
            origin_message_id=msg["id"] if msg else None, profile="whatsapp-job-claude")
        return runner.register.submit(row["id"]), msg

    def until(self, runner, condition, limit=10.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            runner.tick()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def row(self, runner, job_id):
        return runner.register.get(job_id)

    def stopped(self, runner, job_id):
        return lambda: (self.row(runner, job_id) or {}).get("state") == jobs.STOPPED

    def settled(self, runner):
        return lambda: not runner.live


# ── The tables ──────────────────────────────────────────────────────────────


@_cli.needs_store
class Migration(td.DialogueCase):
    """The three job tables arrive as steps of their own, under the naming law,
    at a new minor, and leave what was stored before as it was."""

    def test_the_job_steps_are_in_the_ledger_at_the_new_minor(self):
        steps = [r["step"] for r in self.db.execute(
            "SELECT step FROM schema_ledger WHERE owner = 'whatsapp' ORDER BY step")]
        self.assertEqual(steps[13:], ["0014-jobs", "0015-jobs-queue", "0016-jobs-by-chat",
                                      "0017-jobs-live-session", "0018-jobs-by-origin",
                                      "0019-job-amendments", "0020-job-amendments-pending",
                                      "0021-job-slots"])
        self.assertEqual(steps, [s.id for s in wa.STORE_STEPS])
        version = self.db.execute(
            "SELECT major, minor FROM schema_version WHERE owner = 'whatsapp'").fetchone()
        self.assertEqual((version["major"], version["minor"]), (1, 3))
        for table, wanted in (
                ("whatsapp_jobs", {"project_id", "environment", "account", "chat_id",
                                   "host", "requested_by", "origin_message_id",
                                   "session_id", "pid", "attempt_token", "lease_owner",
                                   "lease_expires_at", "delivery_state"}),
                ("whatsapp_job_amendments", {"project_id", "environment", "account",
                                             "job_id", "text", "state", "claim_token"}),
                ("whatsapp_job_slots", {"project_id", "environment", "account", "slot",
                                        "job_id", "owner_id", "attempt_token"})):
            columns = {r["column_name"] for r in self.db.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = %s", (table,))}
            self.assertTrue(wanted <= columns, (table, wanted - columns))

    def test_a_store_at_the_previous_minor_keeps_its_rows(self):
        import psycopg
        name = f"wa_mig_{uuid.uuid4().hex[:8]}"
        conn = psycopg.connect(_cli.STORE_DSN, autocommit=True)
        self.addCleanup(conn.close)
        conn.execute(f"CREATE SCHEMA {name}")
        self.addCleanup(conn.execute, f"DROP SCHEMA {name} CASCADE")
        wa.migrate(conn, "whatsapp", wa.STORE_STEPS[:13], major=1, minor=2, schema=name)
        conn.execute(f"SET search_path TO {name}")
        conn.execute("INSERT INTO whatsapp_messages (account, chat_id, id, text, ts)"
                     " VALUES ('a', 'c', 'm1', 'kept', now())")
        conn.execute("INSERT INTO whatsapp_register (project_id, environment, account,"
                     " chat_id, counters) VALUES ('p', 'e', 'a', 'c', '{\"answered\": 2}')")
        before = conn.execute("SELECT * FROM whatsapp_messages").fetchall()
        result = wa.migrate(conn, "whatsapp", wa.STORE_STEPS, major=1, minor=3, schema=name)
        self.assertEqual(result.applied, [s.id for s in wa.STORE_STEPS[13:]])
        conn.execute(f"SET search_path TO {name}")
        self.assertEqual(conn.execute("SELECT * FROM whatsapp_messages").fetchall(), before)
        self.assertEqual(conn.execute("SELECT counters FROM whatsapp_register").fetchone()[0],
                         {"answered": 2})
        self.assertEqual(conn.execute("SELECT count(*) FROM whatsapp_jobs").fetchone()[0], 0)


# ── Settings ────────────────────────────────────────────────────────────────


class Settings(unittest.TestCase):
    def test_the_job_keys_are_validated(self):
        schema.validate_settings({"defaults": {"max_parallel_jobs": 4,
                                               "job_profile": "whatsapp-job-codex",
                                               "job_recovery": "inspect"}})
        for document, fragment in (
                ({"defaults": {"max_parallel_jobs": 0}}, "max_parallel_jobs"),
                ({"defaults": {"max_parallel_jobs": 2.5}}, "max_parallel_jobs"),
                ({"defaults": {"job_recovery": "retry"}}, "job_recovery"),
                ({"defaults": {"job_profile": "a b"}}, "job_profile"),
                ({"defaults": {"job_poll_interval": 1}}, "unsupported property")):
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(ValueError, fragment):
                    schema.validate_settings(document)

    def test_the_job_profile_is_reached_with_its_position(self):
        found = schema.profile_names({}, profiles.DEFAULT_PROFILE,
                                     profiles.DEFAULT_JOB_PROFILE)
        self.assertEqual(found["whatsapp-job-claude"], ["defaults.job_profile"])
        found = schema.profile_names({"defaults": {"job_profile": "mine"}}, "a", "b")
        self.assertEqual(found["mine"], ["defaults.job_profile"])
        self.assertIn("whatsapp-job-claude", profiles.BUNDLED)

    def test_init_seeds_the_job_prose_and_never_overwrites_it(self):
        held = {"context": "mine"}
        put = []
        adapter = types.SimpleNamespace(
            source="files", document_read=lambda cap, key: held.get(key),
            document_put=lambda cap, key, body, author=None: put.append(key))
        written, kept = wa._seed_service_context(adapter)
        self.assertEqual(put, ["delegation", "job-worker"])
        self.assertEqual(kept, ["files:whatsapp/context"])
        self.assertEqual(written, ["files:whatsapp/delegation", "files:whatsapp/job-worker"])
        for key in ("delegation", "job-worker"):
            self.assertTrue(wa._service_template(f"{key}.md").is_file())


# ── The shim and the CLI's scope ────────────────────────────────────────────


class Shim(unittest.TestCase):
    """The shim fixes the destination from the turn's environment: a call that
    names one itself is refused, and the scope is added where it parses."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.real = folder / "real-whatsapp"
        self.real.write_text(f"#!{sys.executable}\nimport json, sys\n"
                             "print(json.dumps(sys.argv[1:]))\n")
        self.real.chmod(0o755)
        self.env = {"PATH": os.environ.get("PATH", ""), "WHATSAPP_REAL_WHATSAPP": str(self.real),
                    "WHATSAPP_DAEMON_CHILD": "1",
                    "WHATSAPP_AUTHORIZED_CHAT_ID": ALICE_JID,
                    "WHATSAPP_AUTHORIZED_REQUESTER": ALICE,
                    "WHATSAPP_AUTHORIZED_ORIGIN_MESSAGE_ID": "M1",
                    "WHATSAPP_AUTHORIZED_JOB_PROFILE": "whatsapp-job-claude"}

    def shim(self, *args, env=None):
        return subprocess.run([sys.executable, str(SHIM), *args], capture_output=True,
                              text=True, env=env or self.env, timeout=30)

    def test_register_carries_the_turns_scope(self):
        done = self.shim("jobs", "register", "--", "-count the files")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [
            "jobs", "register", "--chat", ALICE_JID, "--actor", ALICE,
            "--requested-by", ALICE, "--origin-message-id", "M1",
            "--profile", "whatsapp-job-claude", "--", "-count the files"])

    def test_every_other_jobs_verb_carries_the_chat_and_actor(self):
        done = self.shim("jobs", "submit", "J1", "--confirm-active-jobs-checked")
        self.assertEqual(json.loads(done.stdout), [
            "jobs", "submit", "--chat", ALICE_JID, "--actor", ALICE, "J1",
            "--confirm-active-jobs-checked"])

    def test_a_destination_named_by_the_worker_is_refused(self):
        for flag in ("--chat", "--chat=x", "--origin-message-id", "--requested-by",
                     "--actor", "--profile", "--connection"):
            with self.subTest(flag=flag):
                args = ["jobs", "register", "do it", flag] + ([] if "=" in flag else ["x"])
                done = self.shim(*args)
                self.assertEqual(done.returncode, 4)
                self.assertEqual(json.loads(done.stderr)["error"]["code"],
                                 "worker_scope_denied")
                self.assertEqual(done.stdout, "")

    def test_help_and_other_verbs_pass_through(self):
        self.assertEqual(json.loads(self.shim("jobs", "help").stdout), ["jobs", "help"])
        self.assertEqual(json.loads(self.shim("messages", "x@s.whatsapp.net").stdout),
                         ["messages", "x@s.whatsapp.net"])

    def test_without_the_turns_scope_jobs_are_refused(self):
        env = dict(self.env)
        del env["WHATSAPP_AUTHORIZED_CHAT_ID"]
        done = self.shim("jobs", "active", env=env)
        self.assertEqual(done.returncode, 4)
        self.assertEqual(json.loads(done.stderr)["error"]["code"], "worker_authority_missing")


def _args(command, **over):
    base = {"jobs_command": command, "chat": None, "actor": None, "connection": None,
            "requested_by": None, "origin_message_id": None, "profile": None,
            "description": None, "id": None, "text": None, "limit": 50, "state": None,
            "outcome": None, "confirm_active_jobs_checked": False}
    base.update(over)
    return argparse.Namespace(**base)


class CliScope(unittest.TestCase):
    """The CLI holds a worker the service started to the same scope, whatever
    reached it."""

    ENV = {"WHATSAPP_DAEMON_CHILD": "1", "WHATSAPP_AUTHORIZED_CHAT_ID": ALICE_JID,
           "WHATSAPP_AUTHORIZED_REQUESTER": ALICE,
           "WHATSAPP_AUTHORIZED_ORIGIN_MESSAGE_ID": "M1",
           "WHATSAPP_AUTHORIZED_JOB_PROFILE": "whatsapp-job-claude"}

    def test_the_turns_scope_replaces_what_is_missing(self):
        args = _args("register", description="do it")
        with mock.patch.dict(os.environ, self.ENV):
            wa._job_scope(args)
        self.assertEqual((args.chat, args.actor, args.requested_by, args.origin_message_id,
                          args.profile),
                         (ALICE_JID, ALICE, ALICE, "M1", "whatsapp-job-claude"))

    def test_a_different_destination_is_refused(self):
        for over in ({"chat": "15559999999@s.whatsapp.net"}, {"actor": "15559999999"},
                     {"requested_by": "15559999999"}, {"origin_message_id": "OTHER"}):
            with self.subTest(over=over), mock.patch.dict(os.environ, self.ENV):
                with self.assertRaises(wa._Refusal) as caught:
                    wa._job_scope(_args("register", description="x", **over))
                self.assertEqual(caught.exception.code, "job_scope_denied")

    def test_a_child_without_its_scope_is_refused(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_DAEMON_CHILD": "1",
                                          "WHATSAPP_AUTHORIZED_CHAT_ID": "",
                                          "WHATSAPP_AUTHORIZED_REQUESTER": ""}):
            with self.assertRaises(wa._Refusal) as caught:
                wa._job_scope(_args("active"))
        self.assertEqual(caught.exception.code, "job_scope_missing")


# ── Handing a turn's request to a job ───────────────────────────────────────


class DelegatingTurn:
    """A dialogue turn that does what the shim lets a worker do: register and
    submit a job through the CLI, under the environment the turn was given,
    then either says one line or keeps going until it is ended."""

    def __init__(self, case, *, line=None, destination=None):
        self.case, self.line, self.destination = case, line, destination
        self.calls: list[dict] = []
        self.refusal = None

    def __call__(self, prompt, profile, cwd, *, session, environ, extra_env, cancel,
                 on_start):
        self.calls.append({"prompt": prompt, "extra_env": dict(extra_env),
                           "cancel": cancel})
        on_start(types.SimpleNamespace(pid=4243, harness="claude", session_id="turn"))
        opened = lambda _args: (jobs, jobs.JobRegister(  # noqa: E731
            wa._CliFacade(), wa._open_store(self.case.cfg), "prj_test", "test"), {})
        with mock.patch.dict(os.environ, extra_env), \
                mock.patch.object(wa, "_open_jobs", side_effect=opened):
            if self.destination:
                try:
                    wa.cmd_jobs(_args("register", description="elsewhere",
                                      chat=self.destination))
                except wa._Refusal as refusal:
                    self.refusal = refusal
            row = wa.cmd_jobs(_args("register", description="count the files"))
            wa.cmd_jobs(_args("submit", id=row["id"], confirm_active_jobs_checked=True))
        if self.line is not None:
            return types.SimpleNamespace(ok=True, answer=f"=== REPLY ===\n{self.line}",
                                         failure=None)
        cancel.wait(30)
        return types.SimpleNamespace(ok=False, answer="", failure=types.SimpleNamespace(
            kind="cancelled", message="cancelled"))


@_cli.needs_store
@td.needs_runner
class Handoff(JobsCase):
    def setUp(self):
        super().setUp()
        for name, value in (("HANDOFF_POLL", 0.05), ("HANDOFF_GRACE", 0.3)):
            patcher = mock.patch.object(dialogue, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def delegate(self, turn, **over):
        d, runner = self.make_jobs(td._settings(authority={"roles": {
            "direct_user": {"allowed_capabilities": {"whatsapp": True}}}}), turn=turn)
        msg, verdict = self.arrive(d, **over)
        self.assertTrue(verdict["admit"], verdict)
        self.settle(d)
        return d, runner, msg

    def test_a_submitted_job_ends_the_turn_with_an_acknowledgement(self):
        turn = DelegatingTurn(self, destination="15559999999@s.whatsapp.net")
        started = time.monotonic()
        d, runner, msg = self.delegate(turn)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(turn.refusal.code, "job_scope_denied")
        rows = runner.register.list()
        self.assertEqual(len(rows), 1)
        job = rows[0]
        self.assertEqual((job["state"], job["chat_id"], job["requested_by"],
                          job["origin_message_id"], job["profile"]),
                         (jobs.WAITING, ALICE_JID, ALICE, msg["id"], "whatsapp-job-claude"))
        self.assertTrue(turn.calls[0]["cancel"].is_set())
        self.assertEqual([r["text"] for r in self.outgoing(ALICE_JID)],
                         [dialogue.HANDOFF_MARK + "count the files"])
        self.assertEqual(d.stats["handed_off"], 1)

    def test_a_turn_that_says_its_line_is_acknowledged_by_it(self):
        turn = DelegatingTurn(self, line="one second")
        d, runner, _msg = self.delegate(turn)
        self.assertFalse(turn.calls[0]["cancel"].is_set())
        self.assertEqual([r["text"] for r in self.outgoing(ALICE_JID)], ["one second"])
        self.assertEqual(runner.register.list()[0]["state"], jobs.WAITING)

    def test_the_turn_is_told_how_to_delegate_only_where_its_role_may(self):
        d, _runner = self.make_jobs()
        self.arrive(d, chat_id=OWN, from_me=1, sender=OWN, sender_device=0, text="hi")
        self.settle(d)
        self.arrive(d, text="hello")
        self.settle(d)
        supervisor, direct = self.run.calls[0], self.run.calls[1]
        self.assertIn("DELEGATION: hand work off.", supervisor["prompt"])
        self.assertIn(f"Jobs command: {dialogue.WORKER_BIN / 'whatsapp'} jobs",
                      supervisor["prompt"])
        self.assertNotIn("DELEGATION", direct["prompt"])
        self.assertNotIn("Jobs command", direct["prompt"])
        env = supervisor["extra_env"]
        self.assertTrue(env["PATH"].startswith(str(dialogue.WORKER_BIN) + os.pathsep))
        self.assertEqual(Path(env["WHATSAPP_REAL_WHATSAPP"]), Path(_cli.CLI_PATH).resolve())
        self.assertEqual(env["WHATSAPP_AUTHORIZED_JOB_PROFILE"], "whatsapp-job-claude")
        self.assertEqual(env["WHATSAPP_ENVIRONMENT"], "test")


# ── The runner ──────────────────────────────────────────────────────────────


@_cli.needs_store
@td.needs_runner
class Runner(JobsCase):
    def test_max_parallel_jobs_is_honoured(self):
        settings = td._settings(defaults={"debounce": 0, "max_parallel_jobs": 2})
        d, runner = self.make_jobs(settings, run=JobRun(block=True))
        made = [self.submit(runner, text=f"job {n}")[0] for n in range(3)]
        self.assertTrue(self.until(runner, lambda: len(self.jrun.calls) == 2))
        for _ in range(5):
            runner.tick()
        self.assertEqual(len(self.jrun.calls), 2)
        self.assertEqual(runner.register.slots_in_use(), 2)
        self.assertEqual([self.row(runner, j["id"])["state"] for j in made],
                         [jobs.RUNNING, jobs.RUNNING, jobs.WAITING])
        self.jrun.release.set()
        self.assertTrue(self.until(runner, lambda: all(
            self.row(runner, j["id"])["outcome"] == jobs.SUCCEEDED for j in made)))
        self.assertEqual(len(self.jrun.calls), 3)
        self.assertEqual(runner.register.slots_in_use(), 0)

    def test_two_claimants_take_one_job_once(self):
        d, runner = self.make_jobs()
        job, _msg = self.submit(runner)
        others = [jobs.JobRegister(wa._CliFacade(), self.store(), "prj_test", "test")
                  for _ in range(2)]
        gate = threading.Barrier(2)
        won = []

        def claim(register, owner):
            gate.wait()
            won.append(register.claim_next(owner_id=owner, host=jobs.HOST, max_parallel=2))
        threads = [threading.Thread(target=claim, args=(r, f"{jobs.HOST}:1:{n}"))
                   for n, r in enumerate(others)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        claimed = [w for w in won if w is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], job["id"])
        self.assertEqual(claimed[0]["attempt"], 1)
        self.assertEqual(runner.register.slots_in_use(), 1)

    def test_one_slot_is_one_job_across_claimants(self):
        d, runner = self.make_jobs()
        for n in range(2):
            self.submit(runner, text=f"job {n}")
        others = [jobs.JobRegister(wa._CliFacade(), self.store(), "prj_test", "test")
                  for _ in range(2)]
        gate = threading.Barrier(2)
        won = []

        def claim(register, owner):
            gate.wait()
            won.append(register.claim_next(owner_id=owner, host=jobs.HOST, max_parallel=1))
        threads = [threading.Thread(target=claim, args=(r, f"{jobs.HOST}:1:{n}"))
                   for n, r in enumerate(others)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len([w for w in won if w is not None]), 1)
        self.assertEqual(runner.register.slots_in_use(), 1)

    def test_an_amendment_continues_the_session(self):
        d, runner = self.make_jobs(run=JobRun(block=True))
        job, _msg = self.submit(runner)
        self.assertTrue(self.until(runner, lambda: self.jrun.started.is_set()))
        first = self.jrun.calls[0]
        self.assertEqual(first["session"], ("fresh",))
        self.assertIn("JOB WORKER: do the work.", first["prompt"])
        self.assertIn("count the files", first["prompt"])
        self.assertEqual(self.row(runner, job["id"])["session_id"], first["sid"])
        runner.register.stage_amendment(job["id"], "only the .py ones")
        self.assertTrue(self.until(runner, lambda: len(self.jrun.calls) == 2))
        second = self.jrun.calls[1]
        self.assertTrue(first["cancel"].is_set())
        self.assertEqual(second["session"], ("resume", first["sid"]))
        self.assertIn("only the .py ones", second["prompt"])
        self.jrun.release.set()
        self.assertTrue(self.until(runner, self.stopped(runner, job["id"])))
        row = self.row(runner, job["id"])
        self.assertEqual((row["outcome"], row["attempt"], row["amendments"],
                          row["session_id"]), (jobs.SUCCEEDED, 2, 1, first["sid"]))
        self.assertEqual(runner.register.pending_amendment(job["id"]), [])

    def test_the_result_is_delivered_quoting_the_request_once(self):
        d, runner = self.make_jobs()
        job, msg = self.submit(runner)
        self.assertTrue(self.until(runner, lambda: self.row(runner, job["id"])
                                   .get("delivery_state") == "delivered"))
        for _ in range(3):
            runner.tick()
        rival = jobs.JobRegister(wa._CliFacade(), self.store(), "prj_test", "test")
        self.assertIsNone(rival.deliver(job["id"], lambda row: ["never"]))
        rows = self.outgoing(ALICE_JID)
        self.assertEqual([(r["text"], r["quoted_id"], r["delivery"]) for r in rows],
                         [("The result.", msg["id"], "pending")])
        self.assertEqual(self.row(runner, job["id"])["delivered_message_id"],
                         rows[0]["local_id"])

    def test_two_deliverers_queue_one_answer(self):
        d, runner = self.make_jobs()
        job, msg = self.submit(runner)
        runner.deliver = lambda: None
        self.assertTrue(self.until(runner, self.stopped(runner, job["id"])))
        racers = [jobs.JobRunner(wa._CliFacade(), dialogue=d, db=self.store(),
                                 run=self.jrun) for _ in range(2)]
        gate = threading.Barrier(2)

        def deliver(racer):
            gate.wait()
            racer.deliver()
        threads = [threading.Thread(target=deliver, args=(r,)) for r in racers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(self.outgoing(ALICE_JID)), 1)

    def test_a_silent_job_sends_nothing(self):
        d, runner = self.make_jobs(run=JobRun("working\n=== REPLY ===\n"))
        job, _msg = self.submit(runner)
        self.assertTrue(self.until(runner, self.stopped(runner, job["id"])))
        runner.tick()
        row = self.row(runner, job["id"])
        self.assertEqual((row["outcome"], row["result_silent"], row["delivery_state"]),
                         (jobs.SUCCEEDED, 1, None))
        self.assertEqual(self.outgoing(ALICE_JID), [])


# ── Failures ────────────────────────────────────────────────────────────────


@_cli.needs_store
@td.needs_runner
class Failures(JobsCase):
    def test_a_spent_quota_pauses_the_queue_and_resumes_it(self):
        d, runner = self.make_jobs(run=JobRun(fail="quota"))
        with mock.patch.object(jobs, "QUOTA_RETRY_SECONDS", 1.0):
            first, msg = self.submit(runner, text="first")
            self.assertTrue(self.until(runner, self.stopped(runner, first["id"])))
            row = self.row(runner, first["id"])
            self.assertEqual((row["state"], row["outcome"]), (jobs.STOPPED, jobs.QUOTA))
            self.assertIsNotNone(row["resume_at"])
            self.assertTrue(runner.paused())
            second, _ = self.submit(runner, text="second")
            for _ in range(5):
                runner.tick()
            self.assertEqual(len(self.jrun.calls), 1)
            self.assertEqual(self.row(runner, second["id"])["state"], jobs.WAITING)
            notice = self.outgoing(ALICE_JID)[0]
            self.assertIn("queue resumes", notice["text"])
            self.assertEqual(notice["quoted_id"], msg["id"])
            self.jrun.fail = None
            self.assertTrue(self.until(runner, lambda: all(
                self.row(runner, j["id"])["outcome"] == jobs.SUCCEEDED
                for j in (first, second))))
        self.assertFalse(runner.paused())
        self.assertEqual(self.jrun.calls[1]["session"][0], "resume")

    def test_a_refused_model_is_recorded_and_reported(self):
        d, runner = self.make_jobs(run=JobRun(fail="model_refused"))
        job, msg = self.submit(runner, chat=OWN, requester=OWN_PHONE)
        self.assertTrue(self.until(runner, lambda: self.outgoing(OWN)))
        row = self.row(runner, job["id"])
        self.assertEqual(row["outcome"], jobs.MODEL_REFUSED)
        self.assertIn("model_refused said no", row["error"])
        text = self.outgoing(OWN)[0]
        self.assertIn("Job failed: «count the files»", text["text"])
        self.assertEqual(text["quoted_id"], msg["id"])
        self.assertFalse(runner.paused())

    def test_a_failure_is_recorded_and_reported(self):
        d, runner = self.make_jobs(run=JobRun(fail="crash"))
        job, _msg = self.submit(runner)
        self.assertTrue(self.until(runner, lambda: self.outgoing(ALICE_JID)))
        row = self.row(runner, job["id"])
        self.assertEqual((row["outcome"], row["error"]), (jobs.FAILED, "crash: crash said no"))
        self.assertEqual(self.outgoing(ALICE_JID)[0]["text"],
                         "«count the files» could not be completed. Please tell an "
                         "administrator.")


# ── Restart ─────────────────────────────────────────────────────────────────


@_cli.needs_store
@td.needs_runner
class Restart(JobsCase):
    DEAD = f"{jobs.HOST}:999999:1"

    def orphan(self, runner, *, session="sess-old", host=None, owner=None):
        """A job a listener that is gone was running."""
        job, msg = self.submit(runner)
        row = runner.register.claim_next(owner_id=owner or self.DEAD,
                                         host=host or jobs.HOST, max_parallel=4,
                                         lease_seconds=60)
        runner.register.attach_process(row["id"], row["attempt_token"], owner or self.DEAD,
                                       pid=4321, session_id=session)
        return runner.register.get(row["id"]), msg

    def test_a_dead_listeners_job_is_interrupted_and_requeued(self):
        d, runner = self.make_jobs()
        row, msg = self.orphan(runner)
        self.assertTrue(self.until(runner, lambda: self.jrun.calls))
        self.assertEqual(self.killed, [4321])
        self.assertEqual(self.jrun.calls[0]["session"], ("resume", "sess-old"))
        self.assertTrue(self.until(runner, self.stopped(runner, row["id"])))
        self.assertEqual(self.row(runner, row["id"])["attempt"], 2)
        self.assertTrue(self.until(runner, lambda: self.outgoing(ALICE_JID)))
        self.assertEqual([r["text"] for r in self.outgoing(ALICE_JID)], ["The result."])

    def test_inspect_stops_it_and_reports_it(self):
        settings = td._settings(defaults={"debounce": 0, "job_recovery": "inspect"})
        d, runner = self.make_jobs(settings)
        row, msg = self.orphan(runner)
        self.assertTrue(self.until(runner, lambda: self.outgoing(ALICE_JID)))
        current = self.row(runner, row["id"])
        self.assertEqual((current["state"], current["outcome"]),
                         (jobs.STOPPED, jobs.INTERRUPTED))
        notice = self.outgoing(ALICE_JID)[0]
        self.assertTrue(notice["text"].startswith("Interrupted: «count the files»"))
        self.assertEqual(notice["quoted_id"], msg["id"])
        self.assertEqual(self.jrun.calls, [])

    def test_a_job_without_a_session_is_reported_not_rerun(self):
        d, runner = self.make_jobs()
        row, _msg = self.orphan(runner, session=None)
        self.assertTrue(self.until(runner, lambda: self.outgoing(ALICE_JID)))
        self.assertEqual(self.row(runner, row["id"])["outcome"], jobs.INTERRUPTED)
        self.assertEqual(self.jrun.calls, [])

    def test_a_live_lease_on_another_machine_is_left_alone_until_it_expires(self):
        d, runner = self.make_jobs()
        row, _msg = self.orphan(runner, host="elsewhere", owner="elsewhere:1:1")
        for _ in range(3):
            runner.tick()
        self.assertEqual(self.row(runner, row["id"])["state"], jobs.RUNNING)
        runner.register.db.execute(
            "UPDATE whatsapp_jobs SET lease_expires_at = now() - interval '1 second'"
            " WHERE id = %s", (row["id"],))
        self.assertTrue(self.until(runner, lambda: self.outgoing(ALICE_JID)))
        self.assertEqual(self.row(runner, row["id"])["outcome"], jobs.INTERRUPTED)
        self.assertEqual(self.killed, [])

    def test_a_result_left_undelivered_is_delivered_once_after_restart(self):
        d, runner = self.make_jobs()
        row, msg = self.orphan(runner)
        runner.register.finish(row["id"], row["attempt_token"], self.DEAD, jobs.SUCCEEDED,
                               result_text="Done before the restart.")
        self.assertEqual(self.outgoing(ALICE_JID), [])
        self.assertTrue(self.until(runner, lambda: self.outgoing(ALICE_JID)))
        _d2, again = self.make_jobs()
        for _ in range(3):
            runner.tick()
            again.tick()
        self.assertEqual([(r["text"], r["quoted_id"]) for r in self.outgoing(ALICE_JID)],
                         [("Done before the restart.", msg["id"])])
        self.assertEqual(self.jrun.calls, [])

    def test_stopping_the_listener_interrupts_and_requeues_a_running_job(self):
        d, runner = self.make_jobs(run=JobRun(block=True))
        job, _msg = self.submit(runner)
        self.assertTrue(self.until(runner, lambda: self.jrun.started.is_set()))
        runner.shutdown(5.0)
        row = self.row(runner, job["id"])
        self.assertEqual((row["state"], row["session_id"]),
                         (jobs.WAITING, self.jrun.calls[0]["sid"]))
        self.assertEqual(runner.register.slots_in_use(), 0)
        self.assertEqual(self.outgoing(ALICE_JID), [])


# ── Control ─────────────────────────────────────────────────────────────────


@_cli.needs_store
@td.needs_runner
class Control(JobsCase):
    def command(self, d, text):
        _msg, verdict = self.arrive(d, text=text, chat_id=OWN, from_me=1, sender=OWN,
                                    sender_device=0)
        self.assertEqual(verdict.get("kind"), "control", verdict)
        return self.outgoing(OWN)[-1]["text"]

    def test_status_lists_the_chats_open_jobs(self):
        d, runner = self.make_jobs()
        self.submit(runner, chat=OWN, requester=OWN_PHONE, text="summarise the week")
        self.submit(runner, text="someone else's")
        status = self.command(d, "/status")
        self.assertIn("jobs: 1 open in this chat", status)
        self.assertIn("waiting: summarise the week", status)
        self.assertNotIn("someone else's", status)

    def test_stop_cancels_the_chats_running_job(self):
        d, runner = self.make_jobs(run=JobRun(block=True))
        job, msg = self.submit(runner, chat=OWN, requester=OWN_PHONE)
        self.assertTrue(self.until(runner, lambda: self.jrun.started.is_set()))
        self.assertEqual(self.command(d, "/stop"), "Stopped.")
        self.assertTrue(self.jrun.calls[0]["cancel"].is_set())
        self.assertTrue(self.until(runner, lambda: len(self.outgoing(OWN)) == 2))
        row = self.row(runner, job["id"])
        self.assertEqual((row["state"], row["outcome"]), (jobs.STOPPED, jobs.CANCELLED))
        notice = self.outgoing(OWN)[-1]
        self.assertTrue(notice["text"].startswith("Stopped: «count the files»"))
        self.assertEqual(notice["quoted_id"], msg["id"])
        self.assertEqual(self.command(d, "/stop"), "Nothing is running right now.")


if __name__ == "__main__":
    unittest.main()
