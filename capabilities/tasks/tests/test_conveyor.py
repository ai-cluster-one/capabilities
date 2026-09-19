#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-agentworker==0.1.2"]
# ///
"""One turn of the conveyor: what is declared, what is claimed, what is settled.

The rules that decide a turn are pure and are checked with no store and no
engine at all. The verb itself is driven over a project written into a temp
directory, with the engine replaced by one that records what it was asked and
moves the fake store the way a worker would. The store-backed checks read
TASKS_TEST_DSN and skip when it is unset; every run works in a schema of its own
and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'callva-agentworker==0.1.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import pytest
from callva import agentworker
from callva.agentworker import Failure, FailureKind, Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

WORKERS = """
[workers.implementation]
selector = { types = ["defect", "change"] }
park_hint = "Check the lane before reading the task as stuck."

[workers.implementation.profile]
engine = "claude"
fence = "act"
model = "claude-opus-5"
effort = "max"
timeout_seconds = 10800

[workers.implementation.routines]
defect = ["development"]
change = ["development"]
"defect:verify" = ["development", "reachability"]

[workers.implementation.instructions]
defect = ["implementation.md", "standing.md"]
change = ["implementation.md", "standing.md"]
"defect:verify" = "verify.md"

[workers.implementation.limits]
attempts = 3
lease_seconds = 11400
hold_seconds_on_exhaustion = 1200
idle_failure_seconds = 120

[workers.evaluation]
selector = { types = ["proposal"] }

[workers.evaluation.profile]
engine = "claude"
fence = "read"
model = "claude-opus-5"
effort = "high"
timeout_seconds = 1800
budget_usd = 10
allow_tools = ["Bash(tasks:*)"]
add_dirs = ["${A_CHECKOUT}"]

[workers.evaluation.routines]
proposal = ["evaluation"]

[workers.evaluation.instructions]
proposal = "evaluation.md"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project that declares two workers, the way a consuming project does."""
    (tmp_path / "routines").mkdir()
    for name in ("development", "reachability", "evaluation"):
        (tmp_path / "routines" / f"{name}.md").write_text(f"# {name}\n")
    envelope = tmp_path / "capabilities" / "tasks"
    (envelope / "instructions").mkdir(parents=True)
    for name in ("implementation.md", "verify.md", "evaluation.md", "standing.md"):
        (envelope / "instructions" / name).write_text(f"HOW TO RUN IT\n\n{name} text.\n")
    (envelope / "workers.toml").write_text(WORKERS)
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setenv("A_CHECKOUT", str(tmp_path / "elsewhere"))
    return tmp_path


def rewrite(project: Path, old: str, new: str) -> None:
    path = project / "capabilities" / "tasks" / "workers.toml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


# --- What is declared --------------------------------------------------------

def test_a_worker_is_read_whole(project):
    worker = mod._worker("implementation")
    assert worker["types"] == ["defect", "change"]
    assert (worker["profile"].engine, worker["profile"].fence) == ("claude", "act")
    assert worker["profile"].timeout_seconds == 10800
    assert worker["limits"]["attempts"] == 3
    assert worker["handler"] == "claude --model claude-opus-5 --effort max"
    assert worker["park_hint"].startswith("Check the lane")


def test_a_declared_variable_is_read_from_the_environment(project, tmp_path):
    """A machine-local path belongs in local environment configuration, so the
    declaration names it rather than carrying it."""
    assert mod._worker("evaluation")["profile"].add_dirs == (str(tmp_path / "elsewhere"),)


def test_a_variable_nothing_sets_is_refused_by_name(project, monkeypatch):
    monkeypatch.delenv("A_CHECKOUT")
    with pytest.raises(SystemExit) as exit_code:
        mod._worker("evaluation")
    assert exit_code.value.code == 6
    _rows, broken = mod._workers_report()
    assert any("A_CHECKOUT" in one for one in broken)


def test_an_unknown_worker_is_refused_and_the_known_ones_named(project, capsys):
    with pytest.raises(SystemExit) as exit_code:
        mod._worker("nobody")
    assert exit_code.value.code == 3
    error = json.loads(capsys.readouterr().err)["error"]
    assert "nobody" in error["message"] and "workers.toml" in error["message"]
    assert "evaluation, implementation" in error["hint"]


def test_a_missing_instruction_file_is_refused_by_worker_and_pair(project, capsys):
    (project / "capabilities" / "tasks" / "instructions" / "standing.md").unlink()
    with pytest.raises(SystemExit) as exit_code:
        mod._worker("implementation")
    assert exit_code.value.code == 3
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "implementation" in message and "'defect'" in message


def test_a_missing_routine_is_refused_by_worker_and_pair(project, capsys):
    (project / "routines" / "development.md").unlink()
    with pytest.raises(SystemExit) as exit_code:
        mod._worker("implementation")
    assert exit_code.value.code == 3
    assert "'development'" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_a_type_the_selector_takes_needs_somewhere_to_send_the_turn(project):
    rewrite(project, 'change = ["development"]\n', "")
    _rows, broken = mod._workers_report()
    assert any("takes 'change' and its routines name none" in one for one in broken)


def test_a_profile_the_schema_refuses_names_its_worker(project):
    rewrite(project, 'fence = "act"', 'fence = "sideways"')
    _rows, broken = mod._workers_report()
    assert any("implementation" in one and "fence" in one for one in broken)


def test_a_key_nothing_reads_is_a_declaration_doing_nothing(project):
    rewrite(project, "attempts = 3", "attempts = 3\nreview_rounds = 2")
    _rows, broken = mod._workers_report()
    assert any("review_rounds" in one for one in broken)


def test_a_project_declaring_no_workers_is_not_an_error(project):
    (project / "capabilities" / "tasks" / "workers.toml").unlink()
    assert mod._declarations() == {}
    rows, broken = mod._workers_report()
    assert (rows, broken) == ([], [])


def test_the_report_names_every_worker_and_what_is_wrong(project):
    rewrite(project, 'fence = "read"', 'fence = "sideways"')
    rows, broken = mod._workers_report()
    assert [row["worker"] for row in rows] == ["evaluation", "implementation"]
    assert [row["ok"] for row in rows] == [False, True]
    assert len(broken) == 1 and broken[0].startswith("evaluation:")


def test_a_stage_takes_its_own_pair_and_falls_back_when_it_has_none(project):
    worker = mod._worker("implementation")
    assert mod._for_pair(worker["routines"], "defect", "verify") == ["development",
                                                                    "reachability"]
    assert mod._for_pair(worker["routines"], "defect", "elsewhere") == ["development"]
    assert mod._for_pair(worker["instructions"], "defect", "verify") == "verify.md"


def test_several_instruction_files_are_read_in_the_order_they_are_named(
        project, monkeypatch, capsys):
    """A rule that holds for every pair a worker takes is written once."""
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", cost_usd=0.1, duration_ms=10,
                           num_turns=1), lands="complete", writes=True)
    one_turn(monkeypatch, capsys, store, engine)
    prompt = engine.seen["prompt"]
    assert "implementation.md text." in prompt and "standing.md text." in prompt
    assert prompt.index("implementation.md text.") < prompt.index("standing.md text.")


def test_a_stage_is_a_plain_name_or_nothing():
    assert mod._stage_of({"metadata": {"stage": " verify "}}) == "verify"
    assert mod._stage_of({"metadata": {"stage": ["verify"]}}) is None
    assert mod._stage_of({"metadata": {}}) is None
    assert mod._stage_of({}) is None


# --- What is claimed ---------------------------------------------------------

def test_the_selector_asks_for_broken_work_before_a_proposal(project, monkeypatch):
    asked: list = []

    def empty(entry, opts):
        asked.append(opts.get("type"))
        return {"claimed": None, "swept": [f"swept-{opts['type']}"]}

    monkeypatch.setattr(mod, "_claim", empty)
    answer = mod._take(None, mod._worker("implementation"), None)
    assert asked == ["defect", "change"]
    # A sweep can happen on a call that claims nothing, so what each attempt
    # swept is carried forward rather than lost with the empty answer.
    assert answer["swept"] == ["swept-defect", "swept-change"]


def test_a_named_task_is_asked_for_once_and_by_key(project, monkeypatch):
    seen: dict = {}

    def taken(entry, opts):
        seen.update(opts)
        return {"claimed": "t", "swept": []}

    monkeypatch.setattr(mod, "_claim", taken)
    mod._take(None, mod._worker("implementation"), "k-1")
    assert seen["key"] == "k-1" and "type" not in seen
    assert seen["worker"] == "implementation"
    assert seen["lease"] == "11400"


def test_a_wait_and_an_exhaustion_are_neither_of_them_spent():
    assert mod._spent([{"metrics": {"waiting": True, "cost_usd": 0.3}},
                       {"metrics": {"exhausted": True}},
                       {"metrics": {"cost_usd": 1.0}},
                       {"metrics": None}]) == 2


# --- What is settled ---------------------------------------------------------

def settle(landed, *, held=False, moved=False, unspent=False):
    return mod._settle(landed, held, moved, unspent)


def test_an_ending_reached_on_purpose_is_ok():
    assert settle("complete") == ("complete", "ok", {})
    assert settle("closed") == ("closed", "ok", {})


def test_a_turn_told_to_wait_on_somebody_is_ok_and_not_an_attempt():
    for landed in ("todo", "in_progress"):
        assert settle(landed, held=True) == ("todo", "ok", {"waiting": True})


def test_a_handoff_at_a_stage_is_ok():
    assert settle("todo", moved=True) == ("todo", "ok", {"handoff": True})


def test_a_gate_stop_is_a_handback_wherever_it_landed():
    assert settle("waiting") == ("waiting", "handback", {})
    assert settle("draft") == ("draft", "handback", {})


def test_a_task_left_in_progress_is_forced_back_and_failed():
    assert settle("in_progress") == ("todo", "failed", {})


def test_put_back_without_saying_why_is_a_failure():
    assert settle("todo") == ("todo", "failed", {})


def test_a_turn_that_never_reached_the_work_is_not_an_attempt():
    # Whatever the task says, and however far it got.
    for landed in ("todo", "complete", "waiting", "in_progress"):
        assert settle(landed, unspent=True) == ("todo", "failed", {"exhausted": True})


def test_a_pickup_already_passed_is_not_a_hold():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    assert mod._held_ahead({"pickup_at": (now + datetime.timedelta(days=1)).isoformat()})
    assert not mod._held_ahead({"pickup_at": (now - datetime.timedelta(days=1)).isoformat()})
    assert not mod._held_ahead({"pickup_at": None})
    assert not mod._held_ahead({"pickup_at": "not a moment"})


# --- What the turn is told ---------------------------------------------------

def a_task(kind="defect", **over):
    task = {"id": "id-1", "unique_key": "t-probe", "type": kind, "title": "probe",
            "status": "in_progress", "metadata": {}, "objective": "settle it"}
    task.update(over)
    return task


def test_the_prompt_is_the_frame_around_the_project_text(project):
    worker = mod._worker("implementation")
    prompt = mod._prompt(worker, a_task(), [{"description": "looked"}],
                         ["development"], "HOW TO RUN IT\n\nthe project's own words.",
                         1, None)
    assert "You are the worker `implementation`" in prompt
    assert "- `development`" in prompt
    assert "the project's own words." in prompt
    assert '"unique_key": "t-probe"' in prompt and '"looked"' in prompt
    assert 'tasks activity t-probe "..."' in prompt
    assert "WHAT THE STORE WILL LET YOU WRITE" in prompt
    assert "Do not close your own raise." in prompt
    # The recover paragraph and the stage line belong to turns that have one.
    assert "raised before" not in prompt and "is at stage" not in prompt


def test_a_second_raise_says_it_is_continuing(project):
    prompt = mod._prompt(mod._worker("implementation"), a_task(), [], ["development"],
                         "text", 2, None)
    assert "raised before and the previous turn did not finish" in prompt


def test_a_stage_is_named_as_the_state_it_starts_from(project):
    prompt = mod._prompt(mod._worker("implementation"), a_task(), [], ["development"],
                         "text", 1, "verify")
    assert "This task is at stage `verify`" in prompt


def test_the_raise_log_never_reaches_the_prompt(project):
    task = a_task(metadata={"raises": [{"cost": 3}], "origin": "gh"})
    prompt = mod._prompt(mod._worker("implementation"), task, [], ["development"],
                         "text", 1, None)
    assert "raises" not in prompt and '"origin": "gh"' in prompt


# --- One whole turn ----------------------------------------------------------

class Engine:
    """The library, with the turn itself replaced. `Profile` and `FailureKind`
    stay the real ones: what a profile is and what a failure is called are the
    library's, and a test that faked them would prove nothing about either."""

    Profile = agentworker.Profile
    Session = agentworker.Session
    FailureKind = FailureKind

    def __init__(self, result, *, lands=None, holds=None, writes=False, stage=None):
        self.result, self.lands, self.holds = result, lands, holds
        self.writes, self.stage, self.seen = writes, stage, {}

    def run(self, prompt, profile, cwd, *, session=None, environ=None, **kw):
        self.seen.update(prompt=prompt, profile=profile, cwd=cwd, session=session,
                         environ=environ, extra=kw)
        if self.writes:
            self.store.trail += 1
        if self.lands:
            self.store.task["status"] = self.lands
        if self.holds:
            self.store.task["pickup_at"] = self.holds
        if self.stage:
            self.store.task["metadata"]["stage"] = self.stage
        return self.result


class Store:
    """The slice of the ledger one turn reads and writes."""

    def __init__(self, kind="defect", raises=None, stage=None):
        self.task = a_task(kind, metadata={"stage": stage} if stage else {})
        self.trail = 0
        self.raises = raises or []
        self.released: list = []
        self.notes: list = []
        self.held: list = []
        self.costs: list = []

    def install(self, monkeypatch, engine):
        engine.store = self
        monkeypatch.setattr(mod, "_agentworker", lambda: engine)
        monkeypatch.setattr(mod, "_claim", lambda entry, opts: {
            "claimed": "id-1", "attempt": len(self.raises) + 1,
            "execution": {"id": "exec-1"}, "task": dict(self.task),
            "activities": [], "swept": []})
        monkeypatch.setattr(mod, "_spent_attempts",
                            lambda entry, tid: mod._spent(self.raises))
        monkeypatch.setattr(mod, "_state", lambda entry, tid: (dict(self.task), self.trail))
        monkeypatch.setattr(mod, "_note",
                            lambda entry, tid, text: self.notes.append(text))
        monkeypatch.setattr(mod, "_hold_off",
                            lambda entry, tid, secs: self.held.append(secs) or "later")
        monkeypatch.setattr(mod, "_accumulate_cost",
                            lambda entry, tid, cost: self.costs.append(cost) or cost)
        monkeypatch.setattr(mod, "_release", self.release)

    def release(self, entry, ref, outcome, status, detail, system, pointer, metrics):
        self.released.append({"ref": ref, "outcome": outcome, "status": status,
                              "detail": detail, "run": f"{system}:{pointer}",
                              "metrics": json.loads(metrics)})
        return {}


def one_turn(monkeypatch, capsys, store, engine, worker="implementation"):
    store.install(monkeypatch, engine)
    mod.cmd_run(None, [worker, "--apply"])
    return json.loads(capsys.readouterr().out), store.released[0]


def test_a_finished_turn_carries_its_measurements_and_its_pinned_session(
        project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", answer="done",
                           session_id=None, model="claude-opus-5-actual",
                           cost_usd=3.21, duration_ms=1_234_567, num_turns=42),
                    lands="complete", writes=True)
    report, released = one_turn(monkeypatch, capsys, store, engine)

    profile = engine.seen["profile"]
    assert (profile.engine, profile.fence) == ("claude", "act")
    assert engine.seen["session"].kind == "pinned"
    assert engine.seen["cwd"] == str(project)
    assert engine.seen["environ"] is os.environ
    # Handed to the turn, not exported: this process keeps writing as itself.
    assert engine.seen["extra"]["extra_env"] == {"TASKS_EXECUTION": "exec-1"}
    assert "TASKS_EXECUTION" not in os.environ

    assert (released["outcome"], released["status"]) == ("ok", "complete")
    assert released["detail"] is None
    assert released["run"] == f"claude:{engine.seen['session'].id}"
    assert released["metrics"]["cost_usd"] == 3.21
    assert released["metrics"]["num_turns"] == 42
    assert released["metrics"]["model"] == "claude-opus-5-actual"
    assert "elapsed_ms" in released["metrics"]
    assert "exhausted" not in released["metrics"]
    assert store.costs == [3.21] and report["cost_total"] == 3.21
    assert report["trail_grew_by"] == 1 and report["turn"]["ok"] is True
    assert "answer" not in report["turn"] and "raw" not in report["turn"]
    assert store.notes == [] and store.held == []


def test_a_quota_failure_returns_the_attempt_and_holds_the_task(
        project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=False, engine="claude", session_id=None,
                           model="claude-opus-5", cost_usd=0.42, duration_ms=331_000,
                           num_turns=8,
                           failure=Failure(FailureKind.QUOTA, "You've hit your limit")))
    report, released = one_turn(monkeypatch, capsys, store, engine)

    assert (released["outcome"], released["status"]) == ("failed", "todo")
    assert released["detail"] == "You've hit your limit"
    assert released["metrics"]["exhausted"] is True
    # What the turn cost is kept on every path.
    assert released["metrics"]["cost_usd"] == 0.42
    assert store.held == [1200] and report["returned_unspent"] is True
    assert report["turn"]["failure"]["kind"] == "quota"
    # The turn wrote nothing, so the raise leaves the entry that says so.
    assert len(store.notes) == 1 and "left no record" in store.notes[0]


def test_a_long_failure_that_did_work_is_a_plain_attempt(project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=False, engine="claude", session_id=None,
                           model="claude-opus-5", duration_ms=10_800_100,
                           failure=Failure(FailureKind.TIMEOUT, "timed out after 10800s")),
                    writes=True)
    report, released = one_turn(monkeypatch, capsys, store, engine)

    assert (released["outcome"], released["status"]) == ("failed", "todo")
    assert released["detail"] == "timed out after 10800s"
    assert "exhausted" not in released["metrics"]
    assert store.held == [] and "returned_unspent" not in report
    assert store.notes == []


def test_a_failure_in_seconds_that_wrote_nothing_never_started(
        project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=False, engine="claude", session_id=None,
                           failure=Failure(FailureKind.ERROR, "connection reset")))
    report, released = one_turn(monkeypatch, capsys, store, engine)
    assert released["metrics"]["exhausted"] is True
    assert store.held == [1200] and report["returned_unspent"] is True


def test_a_turn_that_waits_on_somebody_is_ok_and_left_held(project, monkeypatch, capsys):
    import datetime
    tomorrow = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=1)).isoformat()
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", session_id=None, cost_usd=0.3,
                           duration_ms=10, num_turns=2),
                    lands="todo", holds=tomorrow, writes=True)
    report, released = one_turn(monkeypatch, capsys, store, engine)

    assert (released["outcome"], released["status"]) == ("ok", "todo")
    assert released["metrics"]["waiting"] is True
    assert report["waiting"] is True
    # The hold is the turn's own: the runner adds none of its own on top.
    assert store.held == []


def test_a_handoff_at_a_stage_is_ok_and_named(project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", session_id=None, cost_usd=0.2,
                           duration_ms=10, num_turns=2),
                    lands="todo", stage="verify", writes=True)
    report, released = one_turn(monkeypatch, capsys, store, engine)
    assert (released["outcome"], released["status"]) == ("ok", "todo")
    assert released["metrics"]["handoff"] is True and report["handoff"] is True


def test_a_gate_stop_hands_the_task_back(project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", session_id=None, cost_usd=0.7,
                           duration_ms=10, num_turns=5), lands="waiting", writes=True)
    _report, released = one_turn(monkeypatch, capsys, store, engine)
    assert (released["outcome"], released["status"]) == ("handback", "waiting")
    assert "waiting" not in released["metrics"]


def test_a_task_at_the_ceiling_is_parked_and_no_turn_is_started(
        project, monkeypatch, capsys):
    store = Store(raises=[{"metrics": {}} for _ in range(4)])
    engine = Engine(Result(ok=True, engine="claude"))
    report, released = one_turn(monkeypatch, capsys, store, engine)
    assert report["parked"] is True and engine.seen == {}
    assert released["outcome"] == "handback"
    assert "Raised 3 times without finishing" in store.notes[0]
    # The project's own sentence goes back with it.
    assert "Check the lane" in store.notes[0]


def test_a_type_the_worker_has_no_pair_for_is_parked_rather_than_dispatched(
        project, monkeypatch, capsys):
    """`--key` reaches past the selector, so a task of a type this worker does
    not declare can be claimed. It must not reach a turn with half a prompt."""
    store = Store(kind="triage")
    engine = Engine(Result(ok=True, engine="claude"))
    report, released = one_turn(monkeypatch, capsys, store, engine)
    assert report["parked"] is True and engine.seen == {}
    assert "declares no routines for triage" in store.notes[0]


def test_a_session_id_other_than_the_pinned_one_is_recorded(project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", session_id="not-the-pinned-one",
                           cost_usd=1.0, duration_ms=10, num_turns=1),
                    lands="complete", writes=True)
    _report, released = one_turn(monkeypatch, capsys, store, engine)
    assert released["metrics"]["session_id_actual"] == "not-the-pinned-one"


def test_a_lapsed_raise_is_said_on_the_trail_rather_than_thrown(
        project, monkeypatch, capsys):
    store = Store()
    engine = Engine(Result(ok=True, engine="claude", cost_usd=0.1, duration_ms=10,
                           num_turns=1), lands="complete", writes=True)
    store.install(monkeypatch, engine)

    def swept(*a, **kw):
        raise mod.Refusal(6, "conflict", "that raise is not open", "it lapsed")

    monkeypatch.setattr(mod, "_release", swept)
    mod.cmd_run(None, ["implementation", "--apply"])
    report = json.loads(capsys.readouterr().out)
    assert report["release_refused"] == "that raise is not open"
    assert any("could not be closed" in note for note in store.notes)


def test_nothing_claimable_is_an_answer_and_not_a_failure(project, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_claim", lambda entry, opts: {"claimed": None, "swept": []})
    mod.cmd_run(None, ["implementation", "--apply"])
    report = json.loads(capsys.readouterr().out)
    assert report == {"worker": "implementation", "claimed": None, "swept": [],
                      "applied": True}


def test_without_apply_nothing_is_claimed_and_no_turn_is_started(
        project, monkeypatch, capsys):
    claimed: list = []
    engine = Engine(Result(ok=True, engine="claude"))
    monkeypatch.setattr(mod, "_agentworker", lambda: engine)
    monkeypatch.setattr(mod, "_claim",
                        lambda entry, opts: claimed.append(opts) or {"claimed": None})
    monkeypatch.setattr(mod, "_would_take",
                        lambda entry, worker, key: {"worker": worker["name"],
                                                    "would_claim": "t-probe",
                                                    "applied": False})
    mod.cmd_run(None, ["implementation"])
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert claimed == [] and engine.seen == {}


def test_run_needs_a_worker(project, capsys):
    with pytest.raises(SystemExit) as exit_code:
        mod.cmd_run(None, ["--apply"])
    assert exit_code.value.code == 6
    assert "run needs a worker" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_run_is_a_write_verb():
    assert "run" in mod.WRITE_VERBS


# --- Against a real store ----------------------------------------------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


@pytest.fixture
def store(monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    entry = {"db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
             "db_user": info.get("user"), "db_name": info.get("dbname"),
             "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
             "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True}
    monkeypatch.setenv("TASKS_TEST_PASSWORD", info.get("password") or "")
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


@needs_store
def test_a_whole_turn_against_the_store(project, store, monkeypatch, capsys):
    """The claim, the raise, the landing and the release, with only the engine
    replaced: everything else is the ledger doing what it does."""
    entry, _schema, _conn = store
    mod.cmd_add(entry, ["--type", "defect", "--title", "A probe", "--key", "t-probe",
                        "--objective", "settle it", "--status", "todo"])
    capsys.readouterr()

    class Worker:
        """A turn that writes back the way a worker does, under its own raise."""

        def run(self, prompt, profile, cwd, *, session=None, environ=None, **kw):
            execution = kw["extra_env"]["TASKS_EXECUTION"]
            monkeypatch.setenv("TASKS_EXECUTION", execution)
            mod.cmd_activity(entry, ["t-probe", "reproduced it, then fixed it"])
            mod.cmd_set(entry, ["t-probe", "--status", "complete"])
            capsys.readouterr()
            monkeypatch.delenv("TASKS_EXECUTION")
            self.prompt = prompt
            return Result(ok=True, engine="claude", answer="done", session_id=session.id,
                          model="claude-opus-5", cost_usd=1.5, duration_ms=900,
                          num_turns=7)

    worker = Worker()
    worker.Profile, worker.Session, worker.FailureKind = (agentworker.Profile,
                                                          agentworker.Session,
                                                          FailureKind)
    monkeypatch.setattr(mod, "_agentworker", lambda: worker)

    mod.cmd_run(entry, ["implementation"])
    assert _answer(capsys) == {"worker": "implementation", "would_claim": "t-probe",
                               "applied": False}

    mod.cmd_run(entry, ["implementation", "--apply"])
    report = _answer(capsys)
    assert report["claimed"] == "t-probe" and report["attempt"] == 1
    assert report["trail_grew_by"] == 1 and report["cost_total"] == 1.5
    assert "You are the worker `implementation`" in worker.prompt
    assert "settle it" in worker.prompt

    mod.cmd_runs(entry, ["t-probe"])
    [raised] = _answer(capsys)["executions"]
    assert (raised["status"], raised["worker"]) == ("ok", "implementation")
    assert raised["handler"] == "claude --model claude-opus-5 --effort max"
    assert raised["run_system"] == "claude" and raised["metrics"]["cost_usd"] == 1.5

    mod.cmd_show(entry, ["t-probe"])
    shown = _answer(capsys)
    assert shown["task"]["status"] == "complete"
    assert shown["task"]["metadata"]["cost_total"] == 1.5
    assert [one["description"] for one in shown["activities"]] == [
        "reproduced it, then fixed it"]

    # Nothing left claimable, and the answer says so rather than failing.
    mod.cmd_run(entry, ["implementation", "--apply"])
    assert _answer(capsys)["claimed"] is None


@needs_store
def test_a_turn_that_wrote_nothing_is_returned_held_and_marked(
        project, store, monkeypatch, capsys):
    entry, _schema, _conn = store
    mod.cmd_add(entry, ["--type", "defect", "--title", "A probe", "--key", "t-quota",
                        "--status", "todo"])
    capsys.readouterr()

    class Spent:
        Profile, Session, FailureKind = (agentworker.Profile, agentworker.Session,
                                         FailureKind)

        def run(self, prompt, profile, cwd, **kw):
            return Result(ok=False, engine="claude", cost_usd=0.4,
                          failure=Failure(FailureKind.QUOTA, "hit your limit"))

    monkeypatch.setattr(mod, "_agentworker", Spent)
    mod.cmd_run(entry, ["implementation", "--apply"])
    report = _answer(capsys)
    assert report["returned_unspent"] is True and report["held_until"]

    mod.cmd_show(entry, ["t-quota"])
    shown = _answer(capsys)
    assert shown["task"]["status"] == "todo" and shown["task"]["pickup_at"]
    assert "left no record" in shown["activities"][0]["description"]

    mod.cmd_runs(entry, ["t-quota"])
    [raised] = _answer(capsys)["executions"]
    assert raised["metrics"]["exhausted"] is True
    # The raise happened and keeps its row, but it was never an attempt.
    assert mod._spent_attempts(entry, str(raised["task_id"])) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
