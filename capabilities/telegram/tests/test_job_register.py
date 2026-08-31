#!/usr/bin/env python3
"""The job register: isolation, arrival order, amendment, and cancellation."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = TELEGRAM_DIR / "service"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _module("telegram_service_store_test", SERVICE_DIR / "store.py")
jobs = _module("telegram_service_jobs_test", SERVICE_DIR / "jobs.py")


class RegisterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.envelope = root / "capabilities"
        self.envelope.mkdir(parents=True)
        (self.envelope / "project.json").write_text(
            json.dumps({"id": "11111111-1111-4111-8111-111111111111",
                        "slug": "testproject", "store": "db"}))
        self.url = str(root / "store.sqlite3")
        self.store, self.reg = jobs.open_register(
            store, self.envelope, "development", url=self.url)
        self.addCleanup(self.store.close)

    def register(self, reg=None, **overrides):
        payload = {"channel_key": "-100:7", "requested_by": "42",
                   "description": "reconcile the ledger", "engine": "codex"}
        payload.update(overrides)
        return (reg or self.reg).register(**payload)

    # -- identity and isolation ----------------------------------------------

    def test_environment_isolates_the_queue(self):
        """The same machine runs development and production; a dev job must not
        be eligible for a production runner."""
        self.register()
        production = jobs.JobRegister(self.store, self.reg.project_id, "production",
                                      slug="testproject")
        self.assertIsNone(production.next_waiting())
        self.assertIsNotNone(self.reg.next_waiting())

    def test_project_isolates_the_queue(self):
        self.store.project_register("22222222-2222-4222-8222-222222222222", "other")
        other = jobs.JobRegister(self.store, self.store._project_id("other"),
                                 "development", slug="other")
        self.register()
        self.assertIsNone(other.next_waiting())

    def test_surface_isolates_the_queue(self):
        self.register()
        elsewhere = jobs.JobRegister(self.store, self.reg.project_id, "development",
                                     surface="slack", slug="testproject")
        self.assertIsNone(elsewhere.next_waiting())

    def test_a_register_without_a_project_is_refused(self):
        with self.assertRaises(jobs.JobError):
            jobs.JobRegister(self.store, None, "development")

    def test_an_unaddressable_environment_is_refused(self):
        with self.assertRaises(jobs.JobError):
            jobs.JobRegister(self.store, self.reg.project_id, "dev/prod")

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

    def test_amendment_keeps_the_row_and_the_session(self):
        row = self.register()
        self.reg.start(row["id"], session_id="thread-1")
        amended = self.reg.amend(row["id"])
        self.assertEqual(amended["id"], row["id"])
        self.assertEqual(amended["session_id"], "thread-1")
        self.assertEqual(amended["amendments"], 1)
        self.assertEqual(amended["state"], jobs.WAITING)
        self.assertIsNone(amended["pid"])
        resumed = self.reg.start(row["id"], pid=99)
        self.assertEqual(resumed["state"], jobs.RUNNING)
        self.assertEqual(resumed["attempt"], 2)

    def test_the_amendment_text_is_handed_over_once(self):
        """The text belongs in the session. Between the worker that wrote it and
        the runner that delivers it, it is a hand-off, not a register column."""
        row = self.register()
        self.reg.start(row["id"], session_id="thread-1")
        self.reg.amend(row["id"], "not that account, the other one")
        self.reg.amend(row["id"], "and hold the invoice")
        self.assertEqual(self.reg.take_amendment(row["id"]),
                         ["not that account, the other one", "and hold the invoice"])
        self.assertEqual(self.reg.take_amendment(row["id"]), [])
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
        self.reg.start(row["id"], session_id="thread-1")
        self.assertEqual(self.reg.amend_pending(), [])
        self.reg.stage_amendment(row["id"], "not that account")
        self.assertEqual([r["id"] for r in self.reg.amend_pending()], [row["id"]])
        self.reg.amend(row["id"])
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
        self.reg.start(row["id"], pid=1234)
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
        self.reg.interrupt_active("service restarted")
        self.assertEqual(self.reg.resume(row["id"])["state"], jobs.WAITING)
        self.assertEqual(self.reg.next_waiting()["id"], row["id"])


if __name__ == "__main__":
    unittest.main()
