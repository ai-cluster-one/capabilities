#!/usr/bin/env python3
"""The two classes of work, end to end.

The dialogue turn registers, amends and cancels; the job runner drains the
register on its own budget and reports into the originating channel. What these
cover is the seam between them — everything the register does on its own is in
test_job_register.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_assistant_service import (  # noqa: E402
    Event,
    FakeClient,
    Message,
    import_daemon,
    settings,
    successful_result,
    wait_until,
)
sys.path.pop(0)


def job_settings(**overrides):
    base = settings(job_poll_interval=0.05, **overrides)
    base["environment"] = "development"
    return base


class JobRunnerTests(unittest.IsolatedAsyncioTestCase):

    async def stop_session(self, client, task):
        client.disconnected.set()
        await asyncio.wait_for(task, timeout=5)

    @staticmethod
    def as_cli(daemon):
        """A second connection to the same store, opened where the caller is.

        This is what `telegram jobs ...` does. The daemon's own register belongs
        to the event loop's thread and a worker is neither that thread nor that
        process, so a worker reaching the register reaches it this way — which
        is exactly why the surface is a CLI over a table rather than a call.
        """
        return daemon.jobs.open_register(
            daemon._records_module(), daemon.PROJECT_CAPABILITIES_DIR,
            daemon.ENVIRONMENT, url=daemon.STORE_URL)

    def daemon_with_store(self, td, **overrides):
        daemon = import_daemon(Path(td), job_settings(**overrides), store=True)
        # The store this daemon opens must be the throwaway one the fixture
        # made. A daemon that resolved it later, from the ambient environment,
        # would write a real machine's queue from a test.
        self.assertEqual(daemon.STORE_URL, str(Path(td) / "store.sqlite3"))
        daemon.WORKERS["stub"] = lambda *a: successful_result("done")
        return daemon

    # -- the register is reachable from the daemon ----------------------------

    async def test_the_daemon_opens_its_register_scoped_to_its_environment(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            register = daemon.job_register()
            self.addCleanup(daemon.close_job_register)
            self.assertEqual(register.environment, "development")
            self.assertEqual(register.surface, "telegram")
            self.assertIsNotNone(register.project_id)

    async def test_worker_popen_reports_pid_and_process_group_before_waiting(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            seen = []
            procs = {}
            rc, out, err = daemon.run_worker_proc(
                "job:test", [sys.executable, "-c", "print('ok')"], procs,
                on_start=lambda pid, pgid: seen.append((pid, pgid)))
            self.assertEqual((rc, out.strip(), err), (0, "ok", ""))
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], seen[0][1])
            self.assertGreater(seen[0][0], 0)

    async def test_a_project_without_a_store_keeps_answering(self):
        """A register that cannot be opened turns the job class off. It does not
        stop the conversation, which is the half that still works."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), job_settings())
            daemon.WORKERS["stub"] = lambda *a: successful_result("done")
            message = Message(50, text="Assistant, hello")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(lambda: any(
                item.get("text") == "done" for item in client.sent))
            self.assertTrue(any("register unavailable" in line
                                for line in daemon._test_logs))
            await self.stop_session(client, task)

    # -- registering from a dialogue turn -------------------------------------

    async def test_a_dialogue_turn_registers_a_job_and_the_runner_answers_it(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            dispatched = []

            def worker(chat, tail, state=None, procs=None):
                registered = (state or {}).get("registered_job")
                if registered is None:
                    store, cli = self.as_cli(daemon)
                    try:
                        cli.register(
                            channel_key=state["channel_key"], requested_by="777",
                            description="reconcile the ledger", engine="stub",
                            origin_message_id=state["current_request"]["message_id"])
                    finally:
                        store.close()
                    return successful_result("queued; I'll report when it's done")
                dispatched.append(registered)
                return successful_result("the ledger is reconciled")

            daemon.WORKERS["stub"] = worker
            message = Message(60, text="Assistant, reconcile the ledger")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(lambda: dispatched, timeout=6)
            await wait_until(lambda: any(
                item.get("text") == "the ledger is reconciled"
                for item in client.sent), timeout=6)

            register = daemon.job_register()
            rows = register.list()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], daemon.jobs.STOPPED)
            self.assertEqual(rows[0]["outcome"], daemon.jobs.SUCCEEDED)
            self.assertEqual(rows[0]["description"], "reconcile the ledger")
            self.assertEqual(rows[0]["requested_by"], "777")
            self.assertEqual(rows[0]["origin_message_id"], "60")
            self.assertEqual(rows[0]["environment"], "development")
            self.assertEqual(dispatched[0]["description"], "reconcile the ledger")
            await self.stop_session(client, task)

    async def test_the_prompt_names_the_job_surface_without_snapshotting_it(self):
        """The queue moves while a turn is being written, so a list pasted in at
        dispatch is stale by the time it is read. The worker is told where to
        ask instead."""
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            row = register.register(channel_key="123", requested_by="777",
                                    description="reconcile the ledger",
                                    engine="stub")
            seen = {}
            hold = asyncio.Event()
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                if (state or {}).get("registered_job") is not None:
                    asyncio.run_coroutine_threadsafe(hold.wait(), loop).result()
                    return successful_result("done")
                seen["prompt"] = daemon.build_prompt(tail, state)
                seen["state"] = dict(state or {})
                return successful_result("done")

            daemon.WORKERS["stub"] = worker
            message = Message(61, text="Assistant, what's the status")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(lambda: "prompt" in seen, timeout=6)
            self.assertIn("Registered jobs", seen["prompt"])
            self.assertIn("jobs list --limit 20", seen["prompt"])
            self.assertIn("At the start of every turn", seen["prompt"])
            self.assertIn("multi-step research", seen["prompt"])
            self.assertIn("With one active job", seen["prompt"])
            self.assertIn("With several active jobs", seen["prompt"])
            self.assertIn("database recency by itself", seen["prompt"])
            self.assertIn("preceding exchange named several jobs", seen["prompt"])
            self.assertIn("make no ledger change", seen["prompt"])
            self.assertNotIn(row["id"], seen["prompt"],
                             "the list is asked for, not handed over")
            self.assertNotIn("open_jobs", seen["state"])
            self.assertNotIn("job_outbox", seen["state"])
            hold.set()
            await self.stop_session(client, task)

    async def test_a_worker_turn_reaches_only_its_own_channels_jobs(self):
        """The shim pins the authorized chat onto every `jobs` call, so a turn
        cannot list or move another room's work."""
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            mine = register.register(channel_key="123", requested_by="777",
                                     description="mine", engine="stub")
            elsewhere = register.register(channel_key="999", requested_by="777",
                                          description="somebody else's",
                                          engine="stub")
            listed = [r["id"] for r in register.open_jobs("123")]
            self.assertIn(mine["id"], listed)
            self.assertNotIn(elsewhere["id"], listed)
            seen = {}

            def worker(chat, tail, state=None, procs=None):
                if (state or {}).get("registered_job") is None:
                    seen["env"] = daemon.worker_env(state)
                return successful_result("done")

            daemon.WORKERS["stub"] = worker
            message = Message(62, text="Assistant, hello")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(lambda: "env" in seen, timeout=6)
            self.assertEqual(seen["env"]["TELEGRAM_AUTHORIZED_CHAT_ID"], "123")
            self.assertEqual(seen["env"]["TELEGRAM_AUTHORIZED_REQUESTER_ID"], "777")
            self.assertEqual(seen["env"]["TELEGRAM_AUTHORIZED_ORIGIN_MESSAGE_ID"], "62")
            self.assertNotIn("TELEGRAM_JOB_OUTBOX", seen["env"])
            await self.stop_session(client, task)

    # -- arrival order and the slot budget ------------------------------------

    async def test_the_job_class_honours_its_own_slot_count(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            for label in ("first", "second", "third"):
                register.register(channel_key="123", requested_by="777",
                                  description=label, engine="stub")
            release = asyncio.Event()
            started = []
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                started.append((state or {}).get("registered_job", {}).get("description"))
                asyncio.run_coroutine_threadsafe(_wait(), loop).result()
                return successful_result("done")

            async def _wait():
                await release.wait()

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(lambda: len(started) >= 1, timeout=6)
            await asyncio.sleep(0.3)
            self.assertEqual(started, ["first"],
                             "one slot means one job, oldest first")
            release.set()
            await wait_until(lambda: len(started) == 3, timeout=8)
            self.assertEqual(started, ["first", "second", "third"])
            await self.stop_session(client, task)

    async def test_a_job_runs_on_the_engine_its_row_records(self):
        """A job registered under one engine is continued under that engine.
        Resumption is engine-specific, so following the channel's current
        setting would resume a session the new engine has never seen."""
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td, worker="codex")
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            register.register(channel_key="123", requested_by="777",
                              description="the stub one", engine="stub")
            seen = []

            def engine(name):
                def worker(*_a, **_k):
                    seen.append(name)
                    return successful_result("done")
                return worker

            daemon.WORKERS["stub"] = engine("stub")
            daemon.WORKERS["codex"] = engine("codex")
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(lambda: seen, timeout=6)
            self.assertEqual(seen, ["stub"],
                             "the channel runs codex; this row says stub")
            await self.stop_session(client, task)

    async def test_a_job_runs_on_the_model_its_row_records(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td, worker="codex")
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            register.register(channel_key="123", requested_by="777",
                              description="pinned model", engine="codex",
                              model="gpt-pinned")
            seen = []

            def worker(_chat, _tail, state=None, _procs=None):
                seen.append(state["settings"]["model"])
                return successful_result("done")

            daemon.WORKERS["codex"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(lambda: seen, timeout=6)
            self.assertEqual(seen, ["gpt-pinned"])
            await self.stop_session(client, task)

    async def test_result_delivery_retries_without_reexecuting_the_job(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            row = register.register(channel_key="123", requested_by="777",
                                    description="durable delivery", engine="stub")
            runs = []

            def worker(*_args, **_kwargs):
                runs.append("run")
                return successful_result("durable answer")

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            original_send = client.send_message
            failures = {"left": 1}

            async def flaky_send(*args, **kwargs):
                if failures["left"]:
                    failures["left"] -= 1
                    raise RuntimeError("Telegram unavailable")
                return await original_send(*args, **kwargs)

            client.send_message = flaky_send
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(
                lambda: register.get(row["id"])["delivery_state"] == "delivered",
                timeout=8)
            after = register.get(row["id"])
            self.assertEqual(runs, ["run"])
            self.assertEqual(after["attempt"], 1)
            self.assertGreaterEqual(after["delivery_attempts"], 2)
            self.assertTrue(any(item.get("text") == "durable answer"
                                for item in client.sent))
            await self.stop_session(client, task)

    # -- amendment ------------------------------------------------------------

    async def test_an_amendment_keeps_the_row_and_continues_the_session(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            row = register.register(channel_key="123", requested_by="777",
                                    description="reconcile the ledger",
                                    engine="stub")
            running = asyncio.Event()
            hold = asyncio.Event()
            runs = []
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                registered = (state or {}).get("registered_job")
                if registered is None:
                    store, cli = self.as_cli(daemon)
                    try:
                        cli.stage_amendment(
                            row["id"], "not that account, the other one")
                    finally:
                        store.close()
                    return successful_result("noted, folding that in")
                runs.append({"text": state["current_request"]["text"],
                             "resume": state.get("resume_session")})
                if len(runs) == 1:
                    state["on_worker_line"](json.dumps({
                        "type": "thread.started", "thread_id": "thread-1"}))
                    loop.call_soon_threadsafe(running.set)
                    state["cancel_event"].wait(timeout=8)
                    raise RuntimeError("worker process group killed")
                return successful_result("the ledger is reconciled")

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await asyncio.wait_for(running.wait(), timeout=6)
            self.assertEqual(register.get(row["id"])["session_id"], "thread-1")
            message = Message(70, text="Assistant, not that account")
            client.messages.append(message)
            await client.handler(Event(message))
            await wait_until(lambda: len(runs) == 2, timeout=8)

            self.assertEqual(len(register.list()), 1, "an amendment is not a new row")
            after = register.get(row["id"])
            self.assertEqual(after["amendments"], 1)
            self.assertIn("not that account, the other one", runs[1]["text"])
            self.assertEqual(runs[1]["resume"], "thread-1")
            self.assertEqual(after["session_id"], "thread-1")
            self.assertEqual(after["outcome"], daemon.jobs.SUCCEEDED)
            await self.stop_session(client, task)

    # -- cancellation ---------------------------------------------------------

    async def test_a_waiting_job_is_stopped_on_request(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            blocker = register.register(channel_key="123", requested_by="777",
                                        description="the long one", engine="stub")
            victim = register.register(channel_key="123", requested_by="777",
                                       description="drop this one", engine="stub")
            hold = asyncio.Event()
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                if (state or {}).get("registered_job") is None:
                    store, cli = self.as_cli(daemon)
                    try:
                        cli.request_stop(victim["id"])
                    finally:
                        store.close()
                    return successful_result("dropped")
                asyncio.run_coroutine_threadsafe(hold.wait(), loop).result()
                return successful_result("done")

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(
                lambda: register.get(blocker["id"])["state"] == daemon.jobs.RUNNING,
                timeout=6)
            message = Message(80, text="Assistant, leave that one")
            client.messages.append(message)
            await client.handler(Event(message))
            await wait_until(
                lambda: register.get(victim["id"])["outcome"] == daemon.jobs.CANCELLED,
                timeout=8)
            hold.set()
            await self.stop_session(client, task)

    async def test_a_stopped_job_is_continued_where_it_stopped(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            row = register.register(channel_key="123", requested_by="777",
                                    description="the long one", engine="stub")
            runs = []
            running = asyncio.Event()
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                runs.append({"text": state["current_request"]["text"],
                             "resume": state.get("resume_session")})
                if len(runs) == 1:
                    state["on_worker_line"](json.dumps({
                        "type": "thread.started", "thread_id": "thread-1"}))
                    loop.call_soon_threadsafe(running.set)
                    state["cancel_event"].wait(timeout=8)
                    raise RuntimeError("worker process group killed")
                return successful_result("done")

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await asyncio.wait_for(running.wait(), timeout=6)
            self.assertEqual(register.get(row["id"])["session_id"], "thread-1")
            register.request_stop(row["id"])
            await wait_until(
                lambda: register.get(row["id"])["outcome"] == daemon.jobs.CANCELLED,
                timeout=8)
            paused = register.get(row["id"])
            self.assertEqual(paused["session_id"], "thread-1")
            self.assertEqual(len(runs), 1, "a paused job is not retried")
            register.resume(row["id"])
            await wait_until(lambda: len(runs) == 2, timeout=8)
            self.assertEqual(runs[1]["resume"], "thread-1")
            self.assertEqual(register.get(row["id"])["outcome"],
                             daemon.jobs.SUCCEEDED)
            self.assertEqual(len(register.list()), 1,
                             "continuing is the same job, not a new one")
            await self.stop_session(client, task)

    # -- quota ----------------------------------------------------------------

    async def test_an_exhausted_subscription_pauses_the_queue(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            first = register.register(channel_key="123", requested_by="777",
                                      description="first", engine="stub")
            second = register.register(channel_key="123", requested_by="777",
                                       description="second", engine="stub")

            def worker(chat, tail, state=None, procs=None):
                raise RuntimeError(
                    "codex worker failed: You've hit your usage limit.")

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(
                lambda: register.get(first["id"])["outcome"] == daemon.jobs.QUOTA,
                timeout=6)
            await asyncio.sleep(0.4)
            self.assertEqual(register.get(second["id"])["state"], daemon.jobs.WAITING,
                             "a paused queue waits; it is not retried into the wall")
            self.assertTrue(any("usage limit" in (item.get("text") or "")
                                for item in client.sent))
            await self.stop_session(client, task)

    # -- restart --------------------------------------------------------------

    async def test_a_restart_recovers_work_that_claimed_to_be_running(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            row = register.register(channel_key="123", requested_by="777",
                                    description="interrupted work", engine="stub")
            register.start(row["id"], pid=999999)
            register.update(row["id"], lease_expires_at="2000-01-01T00:00:00+00:00",
                            owner_host=daemon.JOB_OWNER_HOST)
            hold = asyncio.Event()
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                asyncio.run_coroutine_threadsafe(hold.wait(), loop).result()
                return successful_result("done")

            daemon.WORKERS["stub"] = worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(
                lambda: register.get(row["id"])["state"] in (
                    daemon.jobs.WAITING, daemon.jobs.RUNNING),
                timeout=6)
            self.assertTrue(any("interrupted" in line for line in daemon._test_logs))
            hold.set()
            await self.stop_session(client, task)

    async def test_an_orderly_restart_requeues_the_live_job_on_its_session(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon_with_store(td)
            self.addCleanup(daemon.close_job_register)
            register = daemon.job_register()
            row = register.register(channel_key="123", requested_by="777",
                                    description="survive the restart", engine="stub")
            runs = []
            running = asyncio.Event()
            loop = asyncio.get_running_loop()

            def worker(chat, tail, state=None, procs=None):
                runs.append(state.get("resume_session"))
                if len(runs) == 1:
                    state["on_worker_line"](json.dumps({
                        "type": "thread.started", "thread_id": "thread-1"}))
                    loop.call_soon_threadsafe(running.set)
                    state["cancel_event"].wait(timeout=8)
                    raise RuntimeError("worker process group killed")
                return successful_result("continued")

            daemon.WORKERS["stub"] = worker
            first_client = FakeClient([])
            first = asyncio.create_task(daemon.run_session(first_client))
            await first_client.started.wait()
            await asyncio.wait_for(running.wait(), timeout=6)
            self.assertEqual(register.get(row["id"])["session_id"], "thread-1")

            await self.stop_session(first_client, first)
            register = daemon.job_register()
            interrupted = register.get(row["id"])
            self.assertEqual(interrupted["state"], daemon.jobs.WAITING)
            self.assertIsNone(interrupted["outcome"])
            self.assertEqual(interrupted["session_id"], "thread-1")

            second_client = FakeClient([])
            second = asyncio.create_task(daemon.run_session(second_client))
            await second_client.started.wait()
            await wait_until(lambda: len(runs) == 2, timeout=6)
            await wait_until(
                lambda: register.get(row["id"])["outcome"] == daemon.jobs.SUCCEEDED,
                timeout=6)
            self.assertEqual(runs, [None, "thread-1"])
            self.assertEqual(register.get(row["id"])["attempt"], 2)
            await self.stop_session(second_client, second)


if __name__ == "__main__":
    unittest.main()
