#!/usr/bin/env python3
"""A stand-in for `callva.harness_runner.run`, and the codex notifications
captured from live runs.

Every worker turn reaches the harness through `daemon.harness_run`. A case that
exercises the daemon's side of a turn - the profile it hands over, the call it
builds, how it reads the result, what it does with a start, an event or a
cancel - replaces that seam with `FakeHarness`, which answers in the runner's
own types. It starts a real child in a session of its own for the run's
duration and reports that child as the harness, so a pid the daemon records is
a real one and a cancel ends a real process.

`CODEX_EVENTS` are app-server notifications as the runner handed them to
`on_event` in live codex runs (codex 0.162.1, runner 0.10.0), trimmed to the
fields the daemon reads and with paths made neutral; `codex-app-server-events.json`
beside this file is where they are kept.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from callva import harness_runner as lib

FIXTURES = Path(__file__).resolve().parent / "codex-app-server-events.json"
CODEX_EVENTS = json.loads(FIXTURES.read_text())


def result(harness="codex", *, ok=True, answer="done", session_id="thread-1",
           model=None, kind=None, message="", tokens=None, cost_usd=None,
           duration_ms=None):
    """A runner Result, as a turn that ended this way reports it."""
    return lib.Result(
        ok=ok, harness=harness, answer=answer, session_id=session_id, model=model,
        tokens=lib.Tokens(**(tokens or {})), cost_usd=cost_usd, duration_ms=duration_ms,
        failure=None if ok else lib.Failure(lib.FailureKind(kind or "error"), message))


class FakeHarness:
    """Answers one `run()` call as a harness would.

    `answer`/`ok`/`kind`/`message` shape the Result; `events` are handed to
    `on_event` after the start; `seconds` is how long the harness runs unless
    cancelled; `before_end`, when given, is called with the call's keyword
    arguments while the harness runs, as a worker writing its outbox would.
    Every call is recorded in `calls`."""

    def __init__(self, *, answer="done", ok=True, kind=None, message="",
                 session_id=None, events=(), seconds=0.0, model=None, tokens=None,
                 cost_usd=None, duration_ms=None, before_end=None, opens_thread=True):
        self.answer, self.ok, self.kind, self.message = answer, ok, kind, message
        self.session_id = session_id
        self.events = list(events)
        self.seconds = seconds
        self.model, self.tokens = model, tokens
        self.cost_usd, self.duration_ms = cost_usd, duration_ms
        self.before_end = before_end
        self.opens_thread = opens_thread
        self.calls = []
        self.children = []
        self.started = threading.Event()

    def __call__(self, prompt, profile, cwd, *, session=None, environ=None,
                 extra_env=None, on_event=None, cancel=None, on_start=None,
                 stderr_tail=40):
        call = {"prompt": prompt, "profile": profile, "cwd": str(cwd), "session": session,
                "environ": dict(environ or {}), "extra_env": dict(extra_env or {}),
                "on_event": on_event, "cancel": cancel, "on_start": on_start}
        self.calls.append(call)
        harness = profile.harness
        if session is not None and session.kind == "resume":
            session_id = session.id
        else:
            session_id = self.session_id or (str(uuid.uuid4()) if harness == "claude"
                                             else "thread-1")
        if cancel is not None and cancel.is_set():
            return self._ended(harness, session_id, "cancelled", "turn cancelled")
        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({float(self.seconds)})"],
            start_new_session=True, stdin=subprocess.DEVNULL)
        self.children.append(child)
        if on_start is not None:
            try:
                on_start(lib.Started(harness, child.pid,
                                     session_id if self.opens_thread else None))
            except Exception:
                pass
        self.started.set()
        for event in self.events:
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    pass
        if self.before_end is not None:
            self.before_end(call)
        while child.poll() is None:
            if cancel is not None and cancel.is_set():
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                return self._ended(harness, session_id, "cancelled", "turn cancelled")
            time.sleep(0.02)
        if cancel is not None and cancel.is_set():
            return self._ended(harness, session_id, "cancelled", "turn cancelled")
        return lib.Result(
            ok=self.ok, harness=harness, answer=self.answer, session_id=session_id,
            model=self.model or profile.model, tokens=lib.Tokens(**(self.tokens or {})),
            cost_usd=self.cost_usd, duration_ms=self.duration_ms,
            failure=None if self.ok else lib.Failure(lib.FailureKind(self.kind or "error"),
                                                     self.message))

    @staticmethod
    def _ended(harness, session_id, kind, message):
        return lib.Result(ok=False, harness=harness, session_id=session_id,
                          failure=lib.Failure(lib.FailureKind(kind), message))


def harness_turn(daemon, worker, fake, state=None, procs=None):
    """One turn through the daemon's own `worker_harness`, on the profile
    `worker_turn` would resolve for it, with `fake` as the harness."""
    from unittest import mock
    state = dict(state or {})
    settings = state.setdefault("settings", {})
    state["profile"] = daemon.resolve_turn_profile(worker, settings)
    with mock.patch.object(daemon, "harness_run", fake):
        return daemon.worker_harness("123", [], state, {} if procs is None else procs)
