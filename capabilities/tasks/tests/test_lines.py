#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract>=0.8,<1",
#                 "callva-harness-runner==0.10.1", "pyyaml>=6"]
# ///
"""Lines: named conveyors a project's workers are grouped into.

A line is kept as versions the way a worker is and is the one home of
membership; a task carries the line it runs on and a worker's claim takes only
its own line's tasks; a line paused in the store holds every service of the
project, beside the machine's own pause; and `lines status` reads each worker's
congestion. A worker's own `max_parallel` is its cap, the service settings'
lane table its fallback. The verbs are driven through the CLI against a store
in TASKS_TEST_DSN, the daemon one step at a time as the service tests drive it,
and the store-backed checks skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract>=0.8,<1' \\
        --with 'callva-harness-runner==0.10.1' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_service as service  # noqa: E402
import test_workers_store as stored  # noqa: E402
from test_service import project, store  # noqa: E402,F401  (fixtures)
from test_workers_store import lab, cli, ok, refused, save  # noqa: E402,F401

mod = service.mod
needs_store = stored.needs_store

WORKER = """---
description: Takes changes.
takes: [change]
profile: plain
---

Do the change.
"""


def line(lab, name: str, text: str, *extra: str):
    return cli(lab, "lines", "save", name, "-", *extra, stdin=text)


def two_lines(lab) -> None:
    """alpha on line `one` and beta on line `two`, both taking `change`."""
    ok(save(lab, "alpha", WORKER))
    ok(save(lab, "beta", WORKER))
    ok(line(lab, "one", "description: The first.\nworkers: [alpha]\n"))
    ok(line(lab, "two", "description: The second.\nworkers: [beta]\n"))


def add(lab, *args: str) -> str:
    return ok(cli(lab, "add", "--type", "change", "--title", "t", "--status", "todo",
                  *args))["created"]


# --- The definition ------------------------------------------------------------

@needs_store
def test_a_line_is_kept_as_versions_and_listed_shown_and_retired(lab):
    first = ok(line(lab, "one", "description: The first.\nworkers: [alpha, beta]\n"))
    assert first["created"] is True and first["line"] == "one"
    assert "'alpha', 'beta' are no worker of this project yet" in first["warning"]
    assert ok(line(lab, "one", "description: The first.\nworkers: [alpha, beta]\n")
              )["created"] is False
    shown = ok(cli(lab, "lines", "show", "one"))
    assert shown["spec"] == {"description": "The first.", "workers": ["alpha", "beta"]}
    assert ok(line(lab, "one", shown["text"]))["created"] is False
    second = ok(line(lab, "one", "description: The first.\nworkers: [beta, alpha]\n",
                     "--base", first["version"]))
    assert second["created"] is True
    assert [one["version"] for one in ok(cli(lab, "lines", "history", "one"))["versions"]] \
        == [first["version"], second["version"]]
    listed = ok(cli(lab, "lines", "list"))["lines"]
    assert [(one["line"], one["workers"]) for one in listed] == [("one", ["beta", "alpha"])]
    assert refused(line(lab, "one", "description: x\nworkers: [a]\n", "--base",
                        first["version"]), 6)["code"] == "conflict"
    assert refused(line(lab, "default", "description: x\nworkers: [a]\n"), 6)["code"] == "input"
    assert refused(line(lab, "bad", "workers: [a, a]\nwho: x\n"), 6)["code"] == "line_invalid"
    assert ok(cli(lab, "lines", "retire", "one"))["retired"] == second["version"]
    assert ok(cli(lab, "lines", "list"))["lines"] == []
    assert refused(cli(lab, "lines", "show", "one"), 3)["code"] == "not_found"


@needs_store
def test_a_worker_is_on_one_line_and_the_store_says_so(lab):
    import psycopg
    ok(line(lab, "one", "description: The first.\nworkers: [alpha]\n"))
    error = refused(line(lab, "two", "description: The second.\nworkers: [beta, alpha]\n"), 6)
    assert error["code"] == "conflict" and "'alpha' is on line 'one'" in error["message"]
    # The store refuses it whoever writes.
    with psycopg.connect(stored.DSN) as conn, pytest.raises(psycopg.errors.ExclusionViolation):
        conn.execute(f"""insert into {lab['schema']}.tasks_lines (project_id, name, spec)
                         values (%s, 'two', '{{"workers": ["alpha"]}}')""", (lab["ids"]["here"],))
    # Another project's line may name a worker of the same name.
    ok(cli(lab, "lines", "save", "two", "-", where="there",
           stdin="description: Theirs.\nworkers: [alpha]\n"))


# --- The task's line and the claim --------------------------------------------

@needs_store
def test_a_task_on_a_line_is_claimed_only_by_that_lines_workers(lab):
    two_lines(lab)
    # Two workers on two lines taking one type do not take the same task.
    assert ok(cli(lab, "doctor"))["ok"] is True
    on_one, on_two = add(lab, "--line", "one"), add(lab, "--line", "two")
    nowhere = add(lab)
    assert ok(cli(lab, "show", on_one))["task"]["line"] == "one"
    assert ok(cli(lab, "show", nowhere))["task"]["line"] is None
    assert refused(cli(lab, "add", "--type", "change", "--title", "t", "--line", "three"),
                   3)["code"] == "not_found"
    assert ok(cli(lab, "run", "alpha"))["would_claim"] == on_one
    assert ok(cli(lab, "run", "beta"))["would_claim"] == on_two
    assert ok(cli(lab, "run", "alpha", "--key", on_two))["would_claim"] is None
    error = refused(cli(lab, "run", "alpha", "--key", on_two, "--apply"), 6)
    assert "is on line two, and worker 'alpha' is on line one" in error["message"]
    ran = ok(cli(lab, "run", "alpha", "--apply"))
    assert ran["claimed"] == on_one
    assert ok(cli(lab, "run", "alpha"))["would_claim"] is None
    # Moved to line one, the task is alpha's; `default` moves it back.
    ok(cli(lab, "set", nowhere, "--line", "one"))
    assert ok(cli(lab, "run", "alpha"))["would_claim"] == nowhere
    ok(cli(lab, "set", nowhere, "--line", "default"))
    assert ok(cli(lab, "show", nowhere))["task"]["line"] is None
    # A claim by hand names no worker and reads no line.
    assert ok(cli(lab, "claim", "--type", "change"))["claimed"] in (on_two, nowhere)


@needs_store
def test_a_turn_creates_on_its_line_and_moves_a_line_only_when_granted(lab):
    two_lines(lab)
    task = add(lab, "--line", "one")
    held = ok(cli(lab, "claim", "--key", task, "--worker", "alpha"))["execution"]["id"]
    turn = {"TASKS_EXECUTION": held}
    made = ok(cli(lab, "add", "--type", "change", "--title", "found", env=turn))
    assert made["line"] == "one"
    assert ok(cli(lab, "show", made["created"]))["task"]["line"] == "one"
    for args in (["add", "--type", "change", "--title", "t", "--line", "two"],
                 ["set", task, "--line", "two"]):
        error = refused(cli(lab, *args, env=turn), 4)
        assert error["code"] == "policy" and "unless its `writes` grants `line`" in \
            error["message"]
    assert ok(cli(lab, "show", task))["task"]["line"] == "one"


def test_a_worker_moves_a_line_only_where_its_writes_grant_it():
    def lookup(execution):
        return {"task_id": "held", "status": "running"}

    env = {"TASKS_EXECUTION": "raise"}
    assert mod._worker_refusal("line:two", "held", lookup, env) == mod._WORKER_LINE_REFUSAL
    granted = {"held": {"line": ["two"]}, "other": {"line": "*"}, "new": {}}
    assert mod._worker_refusal("line:two", "held", lookup, env, granted) is None
    assert mod._worker_refusal("line:other", "elsewhere", lookup, env, granted) is None
    said = mod._worker_refusal("line:three", "held", lookup, env, granted)
    assert said == ("a worker may not put its own task on line three; its worker's "
                    "`writes.held.line` allows only two")
    assert mod._worker_refusal("line:two", "held", lookup, {}) is None


# --- The pause and the congestion read ----------------------------------------

@needs_store
def test_a_line_is_paused_in_the_store_and_status_reads_its_congestion(lab):
    two_lines(lab)
    first, second = add(lab, "--line", "one"), add(lab, "--line", "one")
    add(lab, "--line", "two")
    held = ok(cli(lab, "claim", "--key", first, "--worker", "alpha"))["execution"]
    assert refused(cli(lab, "lines", "pause", "one"), 6)["code"] == "input"
    assert refused(cli(lab, "lines", "pause", "three", "--reason", "x"), 3)["code"] \
        == "not_found"
    paused = ok(cli(lab, "lines", "pause", "one", "--reason", "release freeze",
                    env={"TASKS_ACTOR": "operator"}))
    assert (paused["state"], paused["reason"], paused["set_by"], paused["changed"]) == (
        "paused", "release freeze", "operator", True)
    status = ok(cli(lab, "lines", "status", "--json"))["lines"]
    assert [one["line"] for one in status] == ["one", "two", "default"]
    one = status[0]
    assert (one["state"], one["reason"], one["set_by"]) == ("paused", "release freeze",
                                                           "operator")
    assert one["description"] == "The first."
    [alpha] = one["workers"]
    assert alpha["worker"] == "alpha" and alpha["due"] == 1
    assert [(r["execution"], r["task_id"], r["node"]) for r in alpha["running"]] == [
        (held["id"], first, held["node"])]
    assert alpha["oldest_due"]["task"] == second
    assert alpha["oldest_due"]["waited_seconds"] >= 0 and alpha["oldest_due"]["due_since"]
    assert status[1]["state"] == "running" and status[1]["workers"][0]["due"] == 1
    assert [w["worker"] for w in status[2]["workers"]] == ["default", "supervisor"]
    assert [one["line"] for one in ok(cli(lab, "lines", "status", "two"))["lines"]] == ["two"]
    resumed = ok(cli(lab, "lines", "resume", "one"))
    assert (resumed["state"], resumed["reason"], resumed["changed"]) == ("running", None, True)
    assert ok(cli(lab, "lines", "status", "one"))["lines"][0]["state"] == "running"
    # The default line pauses too, and a read-only form needs no write grant.
    ok(cli(lab, "lines", "pause", "default", "--reason", "quiet"))
    assert ok(cli(lab, "lines", "status", "default"))["lines"][0]["state"] == "paused"


@needs_store
def test_a_line_paused_in_the_store_holds_every_service_of_the_project(
        project, store, capsys, monkeypatch, tmp_path):
    """Two daemons serving one project - two machines, each with its own state
    - both start no turn on a paused line's worker, and both take a resume up
    from the store's versions, long before their poll."""
    entry, schema, conn = store
    monkeypatch.setattr(mod, "_pause_wait", lambda *args: None)
    service.write_settings(project, "version = 1\npoll_seconds = 3600\nmax_parallel = 4\n")
    docs = tmp_path / "line-one.yaml"
    docs.write_text("description: The first.\nworkers: [alpha]\n")
    mod.cmd_lines(entry, ["save", "one", str(docs)])
    mod.cmd_lines(entry, ["pause", "one", "--reason", "freeze"])
    capsys.readouterr()
    service.add(entry, capsys, "alpha", line="one")
    service.add(entry, capsys, "beta")
    first = service.Harness(project, entry)
    monkeypatch.setattr(mod, "_STATE_HOME", tmp_path / "elsewhere")
    second = service.Harness(project, entry)
    with first as h1, second as h2:
        for h in (h1, h2):
            assert h.daemon.declaration["lanes"][0]["line"] == "one"
        h1.settle(2.0)
        h2.settle(2.0)
        for h in (h1, h2):
            assert h.running("alpha") == 0 and h.running("beta") == 1
            status = h.daemon.status()
            assert status["line_pause"]["holding"] == ["alpha"]
            assert status["line_pause"]["lines"]["one"]["reason"] == "freeze"
            [alpha] = [lane for lane in status["lanes"] if lane["worker"] == "alpha"]
            assert alpha["line"] == "one" and alpha["held_by"] == ["line_pause"]
            assert status["pause"]["holding"] == []
        # The second machine's own pause is as it always was, and both holds
        # are named there; the first machine's pause is untouched.
        mod.cmd_service_pause(["alpha"])
        capsys.readouterr()
        h2.dispatcher.step()
        [alpha] = [lane for lane in h2.daemon.status()["lanes"] if lane["worker"] == "alpha"]
        assert alpha["held_by"] == ["service_pause", "line_pause"]
        assert h1.daemon.status()["pause"]["holding"] == []
        mod.cmd_lines(entry, ["resume", "one"])
        capsys.readouterr()
        h1.steps(lambda: h1.running("alpha") == 1, seconds=4)
        assert "resume" in h1.daemon.last_wake["reasons"]
        h2.settle(2.0)
        assert h2.running("alpha") == 0
        status = h2.daemon.status()
        assert status["line_pause"]["holding"] == [] and status["pause"]["holding"] == ["alpha"]
    log = (h1.daemon.state_dir / "daemon.log").read_text()
    assert "line one paused in the store by" in log and ": freeze; starting no turn on alpha" in log
    assert "line one resumed in the store" in log


# --- The worker's cap ----------------------------------------------------------

def test_a_workers_cap_is_its_own_and_the_lane_table_its_fallback(project):
    service.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\nmax_parallel: 3")
    service.write_settings(project, """
        version = 1
        [lanes.alpha]
        max_parallel = 2
        [lanes.beta]
        max_parallel = 2
    """)
    lanes = {lane["worker"]: lane["max_parallel"]
             for lane in mod._service_declaration()["lanes"]}
    assert lanes == {"alpha": 3, "beta": 2}
    said = mod._deprecations([])
    assert any("its lanes.beta.max_parallel is read as worker beta's max_parallel" in one
               for one in said)
    service.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\nmax_parallel: 0")
    assert "its `max_parallel` is a whole number of at least 1" in service.refused()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
