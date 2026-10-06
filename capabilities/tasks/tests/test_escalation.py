#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""Escalation: a task its worker's claim cannot move goes over to the next name
in the project's chain, ending at a person.

What the conveyor settings declare, and what `doctor` makes of them, is checked
with no store. What a claim escalates - a task its `before` hook holds back past
`stall_after`, and one whose raises in place reached `limits.attempts` - is
driven against a real store with the harness replaced, and `doctor` and the
paused lane's wait once through the CLI. The store-backed checks read
TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest
from callva import harness_runner
from callva.harness_runner import FailureKind, Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_hooks as hooks  # noqa: E402
import test_service as service  # noqa: E402
from test_hooks import project, store, turns  # noqa: E402,F401  (fixtures)
from test_service import lab  # noqa: E402,F401  (fixture)

mod = hooks.mod
needs_store = hooks.needs_store
add, answer, shown = hooks.add, hooks.answer, hooks.shown
raises_of, changes_of, tell = hooks.raises_of, hooks.changes_of, hooks.tell

CHAIN = 'escalate_to = ["supervisor", "owner"]\n'
SUPERVISOR = "takes: {status: [waiting], assignee: [supervisor]}\nprofile: plain"
HOOK_LINE = f"  before: {sys.executable} hooks/hook.py hooks/before --pre"


def conveyor(project: Path, text: str = CHAIN) -> Path:
    path = project / "capabilities" / "tasks" / "conveyor.toml"
    path.write_text(text)
    return path


def supervised(project: Path, hooked: bool = False) -> None:
    """A supervisor worker that takes the waiting tasks assigned to it."""
    hooks.write_worker(project, "supervisor",
                       SUPERVISOR + ("\nhooks:\n" + HOOK_LINE if hooked else ""))


def aged(conn, schema: str, key: str, hours: float) -> None:
    """Make a task's place older: it was created that long ago, and every move
    it made happened then too."""
    conn.execute(f"""update {schema}.tasks set created_at = now() - make_interval(hours => %s)
                      where unique_key = %s""", (hours, key))
    conn.execute(f"""update {schema}.task_changes set changed_at = now() - make_interval(hours => %s)
                      where task_id = (select id from {schema}.tasks where unique_key = %s)""",
                 (hours, key))


def raised(conn, schema: str, key: str, n: int, *, exhausted: int = 0,
           hours_ago: float = 0) -> list[str]:
    """`n` ended raises of the task that moved nothing, the first `exhausted` of
    them marked so, started `hours_ago` - by default now, after the task was
    created."""
    ids = []
    for at in range(n):
        metrics = json.dumps({"exhausted": True} if at < exhausted else {})
        row = conn.execute(
            f"""insert into {schema}.task_executions
                  (task_id, attempt, worker, status, metrics, started_at, ended_at)
                select id, %s, 'alpha', 'failed', %s::jsonb,
                       now() - make_interval(hours => %s), now() - make_interval(hours => %s)
                  from {schema}.tasks where unique_key = %s returning id""",
            (at + 1, metrics, hours_ago, hours_ago, key)).fetchone()
        ids.append(str(row[0]))
    return ids


def trail(entry, capsys, key: str) -> list[dict]:
    return shown(entry, capsys, key)["activities"]


# --- What is declared --------------------------------------------------------

def test_the_settings_load_with_their_default_ceiling(project):
    supervised(project)
    conveyor(project)
    settings, problems = mod._read_conveyor()
    assert problems == []
    assert settings["escalate_to"] == ["supervisor", "owner"]
    assert settings["stall_after"] == 6 * 3600
    conveyor(project, CHAIN + 'stall_after = "90m"\n')
    assert mod._read_conveyor()[0]["stall_after"] == 5400
    block, wrong, warning = mod._conveyor_report()
    assert wrong == [] and warning is None
    assert block["escalate_to"] == ["supervisor", "owner"] and block["stall_after"] == 5400


def test_a_project_without_the_file_escalates_nothing_and_is_warned(project):
    assert mod._read_conveyor() == (None, [])
    assert mod._conveyor() is None
    block, wrong, warning = mod._conveyor_report()
    assert block is None and wrong == []
    assert "no escalation chain is set" in warning and "conveyor.toml" in warning


@pytest.mark.parametrize("text, said", [
    ('escalate_to = ["supervisor", "alpha"]\n',
     "ends at 'alpha', which is a worker here"),
    ('escalate_to = ["nobody-here", "owner"]\n',
     "names 'nobody-here', which is no worker here"),
    ('escalate_to = ["alpha", "owner"]\n',
     "names worker 'alpha', which takes no waiting task assigned to it"),
    ('escalate_to = ["default", "owner"]\n',
     "names worker 'default', which is switched off"),
])
def test_doctor_refuses_a_chain_nothing_can_end(project, text, said):
    supervised(project)
    conveyor(project, text)
    _block, wrong, _warning = mod._conveyor_report()
    assert any(said in one for one in wrong), wrong


@pytest.mark.parametrize("text, said", [
    ('escalate_to = []\n', "a list of names"),
    ('escalate_to = "owner"\n', "a list of names"),
    ('escalate_to = ["owner", "owner"]\n', "names 'owner' twice"),
    (CHAIN + 'stall_after = "soon"\n', "`stall_after` is a duration"),
    (CHAIN + 'stall_after = 0\n', "`stall_after` is a duration"),
    (CHAIN + 'cool_down = 3\n', "names 'cool_down', which nothing reads"),
    ('stall_after = "6h"\n', "nothing would ever be escalated"),
    ('escalate_to = [\n', "could not be read"),
])
def test_a_file_that_cannot_be_read_refuses_the_run(project, capsys, text, said):
    conveyor(project, text)
    _settings, problems = mod._read_conveyor()
    assert any(said in one for one in problems), problems
    with pytest.raises(SystemExit) as stopped:
        mod._conveyor()
    assert stopped.value.code == 6
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "conveyor_invalid"


def test_the_chain_moves_forward_and_ends_at_the_person(project):
    supervised(project)
    conveyor(project)
    escalation = mod._escalation_of(mod._worker("alpha"))
    assert mod._escalation_target(escalation, None) == "supervisor"
    assert mod._escalation_target(escalation, "alpha") == "supervisor"
    assert mod._escalation_target(escalation, "supervisor") == "owner"
    assert mod._is_person("owner", escalation)
    assert not mod._is_person("supervisor", escalation)
    assert not mod._is_person("default", escalation)
    assert not mod._is_person("", escalation)


# --- A hold past the ceiling -------------------------------------------------

def moment_ahead(hours: int = 1) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=hours)).replace(microsecond=0).isoformat()


@needs_store
def test_a_hold_past_the_ceiling_escalates_instead_of_deferring(project, store, turns,
                                                                capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-stuck")
    aged(conn, schema, "t-stuck", 7)
    later = moment_ahead()
    tell(project, "before", "t-stuck", exit=75, print=later)
    created = shown(entry, capsys, "t-stuck")["task"]["created_at"]

    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None and turns == []
    [moved] = report["escalated"]
    assert (moved["task"], moved["from"], moved["to"]) == ("t-stuck", None, "supervisor")
    assert report.get("passed_over") == []
    task = shown(entry, capsys, "t-stuck")["task"]
    assert (task["status"], task["assignee"], task["pickup_at"]) == ("waiting", "supervisor",
                                                                     None)
    [entry_] = trail(entry, capsys, "t-stuck")
    assert entry_["actor"] == "tasks:scan"
    text = entry_["description"]
    assert text.startswith("Escalated from nobody to supervisor: ")
    assert later in text
    assert f"In place since {datetime.datetime.fromisoformat(created).isoformat()}" in text
    assert "0 raise(s) in place" in text and "6h ceiling" in text
    assert raises_of(entry, capsys, "t-stuck") == []
    moves = {(c["field"], c["new_value"], c["actor"]) for c in changes_of(entry, capsys,
                                                                          "t-stuck")}
    assert moves == {("status", "waiting", "tasks:scan"),
                     ("assignee", "supervisor", "tasks:scan")}


@needs_store
def test_a_skip_past_the_ceiling_escalates_with_the_hooks_last_line(project, store, turns,
                                                                    capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project, CHAIN + 'stall_after = "2h"\n')
    add(entry, capsys, "t-shut")
    add(entry, capsys, "t-free")
    aged(conn, schema, "t-shut", 3)
    tell(project, "before", "t-shut", exit=3, print="SHUT lane: open work")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-free" and turns == ["t-free"]
    [moved] = report["escalated"]
    assert moved["task"] == "t-shut" and "SHUT lane: open work" in moved["why"]
    assert "skipped: exited 3" in moved["why"]
    assert shown(entry, capsys, "t-shut")["task"]["status"] == "waiting"


@needs_store
def test_a_hold_under_the_ceiling_defers_as_before(project, store, turns, capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-young")
    aged(conn, schema, "t-young", 5)
    later = moment_ahead()
    tell(project, "before", "t-young", exit=75, print=later)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None and "escalated" not in report
    [held] = report["passed_over"]
    assert held["verdict"] == "defer" and held["pickup_set"] is True
    task = shown(entry, capsys, "t-young")["task"]
    assert task["status"] == "todo" and task["assignee"] is None
    assert datetime.datetime.fromisoformat(task["pickup_at"]) == \
        datetime.datetime.fromisoformat(later)
    assert trail(entry, capsys, "t-young") == []


@needs_store
def test_an_appointment_that_passed_restarts_the_clock(project, store, turns, capsys):
    """A pickup a person appointed holds the task until it comes; time before
    it was no refusal."""
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-booked")
    mod.cmd_set(entry, ["t-booked", "--pickup", "2020-01-01"])
    capsys.readouterr()
    aged(conn, schema, "t-booked", 30)
    # The appointment came an hour ago.
    conn.execute(f"""update {schema}.task_changes
                        set new_value = (now() - interval '1 hour')::text
                      where field = 'pickup'""")
    conn.execute(f"update {schema}.tasks set pickup_at = now() - interval '1 hour'")
    tell(project, "before", "t-booked", exit=3, print="not yet")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert "escalated" not in report and report["passed_over"][0]["verdict"] == "skip"


@needs_store
def test_without_apply_the_escalation_is_named_and_not_made(project, store, turns, capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-dry")
    aged(conn, schema, "t-dry", 7)
    tell(project, "before", "t-dry", exit=75, print=moment_ahead())
    before = shown(entry, capsys, "t-dry")["task"]
    mod.cmd_run(entry, ["alpha"])
    dry = answer(capsys)
    assert dry["would_claim"] is None and dry["passed_over"] == []
    [would] = dry["would_escalate"]
    assert (would["task"], would["to"]) == ("t-dry", "supervisor")
    assert shown(entry, capsys, "t-dry")["task"] == before
    assert trail(entry, capsys, "t-dry") == []


@needs_store
def test_key_on_a_held_task_past_the_ceiling_escalates_and_says_so(project, store, turns,
                                                                  capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-named")
    aged(conn, schema, "t-named", 7)
    tell(project, "before", "t-named", exit=3, print="closed")
    with pytest.raises(SystemExit) as stopped:
        mod.cmd_run(entry, ["alpha", "--key", "t-named", "--apply"])
    assert stopped.value.code == 6
    assert "escalated to supervisor" in capsys.readouterr().err
    assert shown(entry, capsys, "t-named")["task"]["assignee"] == "supervisor"


# --- Waiting its turn is not refusal ----------------------------------------

@needs_store
def test_a_task_waiting_its_turn_is_never_escalated(project, store, turns, capsys):
    entry, schema, conn = store
    hooks.write_worker(project, "beta", "takes: [beta]\nprofile: plain")
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-queued")
    add(entry, capsys, "t-beta", kind="beta")
    aged(conn, schema, "t-queued", 30)
    # Another worker's claim does not touch it.
    mod.cmd_run(entry, ["beta", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-beta" and "escalated" not in report
    assert shown(entry, capsys, "t-queued")["task"]["status"] == "todo"
    # Its own taker's claim takes it as always.
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-queued" and "escalated" not in report
    assert turns == ["t-beta", "t-queued"]
    assert all(one["actor"] != "tasks:scan" for one in trail(entry, capsys, "t-queued"))


@needs_store
def test_a_hooked_task_let_through_after_a_long_wait_is_claimed(project, store, turns,
                                                               capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-go")
    aged(conn, schema, "t-go", 30)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-go" and "escalated" not in report


# --- Attempts in place -------------------------------------------------------

@pytest.fixture
def stubborn(store, monkeypatch, capsys):
    """The harness replaced by a worker that writes on the trail and moves
    nothing, so every raise ends in place."""
    entry, _schema, _conn = store
    ran: list[str] = []

    class Worker:
        Profile, Session, FailureKind = (harness_runner.Profile, harness_runner.Session,
                                         FailureKind)
        find_profile_file = staticmethod(harness_runner.find_profile_file)
        ProfileNotFound = harness_runner.ProfileNotFound

        def run(self, prompt, profile, cwd, *, session=None, environ=None, **kw):
            execution = kw["extra_env"]["TASKS_EXECUTION"]
            monkeypatch.setenv("TASKS_EXECUTION", execution)
            with mod._connect(entry) as conn, conn.cursor() as cur:
                cur.execute(f"""select t.unique_key from {mod.SCHEMA}.task_executions e
                                  join {mod.SCHEMA}.tasks t on t.id = e.task_id
                                 where e.id::text = %s""", (execution,))
                key = cur.fetchone()["unique_key"]
            ran.append(key)
            mod.cmd_activity(entry, [key, "tried and got nowhere"])
            capsys.readouterr()
            monkeypatch.delenv("TASKS_EXECUTION")
            return Result(ok=True, harness="claude", answer="tried", session_id=session.id,
                          model="m", cost_usd=0.0, duration_ms=1, num_turns=1)

    monkeypatch.setattr(mod, "_harness_runner", Worker)
    return ran


@needs_store
def test_three_raises_in_place_escalate_on_the_next_claim(project, store, stubborn,
                                                          capsys):
    entry, _schema, _conn = store
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\n"
                       "park_hint: Check the lane before raising it again.")
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-loop")
    for _ in range(3):
        mod.cmd_run(entry, ["alpha", "--apply"])
        assert answer(capsys)["claimed"] == "t-loop"
    assert stubborn == ["t-loop"] * 3
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None and stubborn == ["t-loop"] * 3
    [moved] = report["escalated"]
    assert moved["to"] == "supervisor" and moved["raises_in_place"] == 3
    assert "raised 3 times in place" in moved["why"]
    assert "Check the lane before raising it again" in moved["why"]
    task = shown(entry, capsys, "t-loop")["task"]
    assert (task["status"], task["assignee"]) == ("waiting", "supervisor")
    scans = [one for one in trail(entry, capsys, "t-loop") if one["actor"] == "tasks:scan"]
    assert len(scans) == 1 and "3 raise(s) in place" in scans[0]["description"]
    assert len(raises_of(entry, capsys, "t-loop")) == 3


@needs_store
def test_raises_that_moved_it_or_were_exhausted_do_not_count(project, store, turns,
                                                             capsys):
    entry, schema, conn = store
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-two")
    raised(conn, schema, "t-two", 2)
    add(entry, capsys, "t-tired")
    raised(conn, schema, "t-tired", 3, exhausted=1)
    add(entry, capsys, "t-moved")
    aged(conn, schema, "t-moved", 3)
    _first, _second, third = raised(conn, schema, "t-moved", 3, hours_ago=2)
    # The third raise handed the task to someone: a move, stamped with the raise.
    conn.execute(f"""insert into {schema}.task_changes
                       (task_id, field, old_value, new_value, execution_id, actor, changed_at)
                     select id, 'assignee', null, 'alpha', %s, 'alpha',
                            now() - interval '90 minutes'
                       from {schema}.tasks where unique_key = 't-moved'""", (third,))
    conn.execute(f"update {schema}.tasks set assignee = 'alpha' where unique_key = 't-moved'")
    for _ in range(3):
        mod.cmd_run(entry, ["alpha", "--apply"])
        report = answer(capsys)
        assert "escalated" not in report, report
    assert sorted(turns) == ["t-moved", "t-tired", "t-two"]


@needs_store
def test_claim_bookkeeping_is_no_move(project, store, turns, capsys):
    """A raise that took the task and put it back where it was moved nothing:
    its status rows into and out of in_progress do not restart the place."""
    entry, schema, conn = store
    supervised(project)
    conveyor(project)
    tid = add(entry, capsys, "t-books")
    aged(conn, schema, "t-books", 3)
    for at in (1, 2, 3):
        execution = conn.execute(
            f"""insert into {schema}.task_executions
                  (task_id, attempt, worker, status, started_at, ended_at)
                values (%s, %s, 'alpha', 'failed', now() - interval '1 hour',
                        now() - interval '50 minutes') returning id""", (tid, at)).fetchone()[0]
        conn.execute(f"""insert into {schema}.task_changes
                           (task_id, field, old_value, new_value, execution_id, changed_at)
                         values (%s, 'status', 'todo', 'in_progress', %s,
                                 now() - interval '1 hour'),
                                (%s, 'status', 'in_progress', 'todo', %s,
                                 now() - interval '50 minutes')""",
                     (tid, execution, tid, execution))
    with mod._connect(entry) as c, c.cursor() as cur:
        facts = mod._in_place(cur, tid)
    assert facts["raises_in_place"] == 3
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert answer(capsys)["escalated"][0]["task"] == "t-books"


@needs_store
def test_key_at_the_attempts_ceiling_escalates_and_says_so(project, store, turns, capsys):
    entry, schema, conn = store
    supervised(project)
    conveyor(project)
    add(entry, capsys, "t-key")
    raised(conn, schema, "t-key", 3)
    mod.cmd_run(entry, ["alpha", "--key", "t-key"])
    dry = answer(capsys)
    assert dry["would_claim"] is None and dry["would_escalate"][0]["task"] == "t-key"
    with pytest.raises(SystemExit) as stopped:
        mod.cmd_run(entry, ["alpha", "--key", "t-key", "--apply"])
    assert stopped.value.code == 6
    assert "escalated to supervisor instead of claimed" in capsys.readouterr().err
    assert shown(entry, capsys, "t-key")["task"]["status"] == "waiting" and turns == []


@needs_store
def test_without_the_settings_attempts_count_as_before(project, store, stubborn, capsys):
    """No chain: raises in place escalate nothing, and the whole-life count
    parks the task past the ceiling as it always did."""
    entry, _schema, _conn = store
    add(entry, capsys, "t-old-rule")
    for _ in range(3):
        mod.cmd_run(entry, ["alpha", "--apply"])
        assert "escalated" not in answer(capsys)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report.get("parked") is True and "escalated" not in report
    assert all(one["actor"] != "tasks:scan" for one in trail(entry, capsys, "t-old-rule"))


# --- The chain ---------------------------------------------------------------

@needs_store
def test_the_supervisor_that_cannot_move_it_passes_it_to_the_person(project, store, turns,
                                                                    capsys):
    entry, schema, conn = store
    supervised(project, hooked=True)
    conveyor(project)
    add(entry, capsys, "t-chain")
    raised(conn, schema, "t-chain", 3)
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert answer(capsys)["escalated"][0]["to"] == "supervisor"
    # On the supervisor, its hook holds it back past the ceiling.
    aged(conn, schema, "t-chain", 7)
    tell(project, "before", "t-chain", exit=3, print="needs the owner")
    mod.cmd_run(entry, ["supervisor", "--apply"])
    report = answer(capsys)
    [moved] = report["escalated"]
    assert (moved["from"], moved["to"]) == ("supervisor", "owner")
    task = shown(entry, capsys, "t-chain")["task"]
    assert (task["status"], task["assignee"]) == ("waiting", "owner")
    scans = [one["description"] for one in trail(entry, capsys, "t-chain")
             if one["description"].startswith("Escalated")]
    assert len(scans) == 2 and scans[1].startswith("Escalated from supervisor to owner")


@needs_store
def test_a_task_on_the_person_is_never_escalated(project, store, turns, capsys):
    entry, schema, conn = store
    supervised(project)
    hooks.write_worker(project, "owner-desk",
                       "takes: {status: [waiting], assignee: [owner]}\nprofile: plain\n"
                       "hooks:\n" + HOOK_LINE)
    conveyor(project)
    add(entry, capsys, "t-person")
    conn.execute(f"""update {schema}.tasks set status = 'waiting', assignee = 'owner'
                      where unique_key = 't-person'""")
    aged(conn, schema, "t-person", 30)
    tell(project, "before", "t-person", exit=3, print="still thinking")
    mod.cmd_run(entry, ["owner-desk", "--apply"])
    report = answer(capsys)
    assert "escalated" not in report and report["passed_over"][0]["verdict"] == "skip"
    raised(conn, schema, "t-person", 5)
    tell(project, "before", "t-person", exit=0)
    mod.cmd_run(entry, ["owner-desk", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-person" and "escalated" not in report


# --- A paused lane -----------------------------------------------------------

@needs_store
def test_a_paused_lane_shows_how_long_its_oldest_due_task_waited(project, store,
                                                                 monkeypatch, capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-new")
    add(entry, capsys, "t-oldest")
    aged(conn, schema, "t-oldest", 9)
    monkeypatch.setattr(mod, "_service_connection", lambda wanted: ("local", entry))
    status = {"pause": {"holding": ["alpha"]}}
    mod._paused_waits(status, None)
    waited = status["pause"]["waited"]["alpha"]
    assert waited["task"] == "t-oldest"
    assert 9 * 3600 - 60 <= waited["waited_seconds"] <= 9 * 3600 + 60
    assert waited["waited"].endswith("h") or waited["waited"].endswith("m")
    # A lane nothing holds is not asked about.
    status = {"pause": {"holding": []}}
    mod._paused_waits(status, None)
    assert "waited" not in status["pause"]


@needs_store
def test_through_the_cli_doctor_and_a_paused_lane(lab):
    project = lab["project"]
    cli = service.tasks_cli
    doctor = service.answer_of(cli(lab, "doctor"))
    assert doctor["escalation"] is None and "no escalation chain" in doctor["warning"]

    conveyor(project, 'escalate_to = ["alpha", "owner"]\n')
    refused = cli(lab, "doctor")
    assert refused.returncode == 6
    assert "takes no waiting task assigned to it" in refused.stderr
    hooks.write_worker(project, "supervisor", SUPERVISOR)
    conveyor(project, 'escalate_to = ["supervisor", "alpha"]\n')
    refused = cli(lab, "doctor")
    assert refused.returncode == 6 and "ends at 'alpha'" in refused.stderr
    conveyor(project, 'escalate_to = ["supervisr", "owner"]\nstall_after = "4h"\n')
    refused = cli(lab, "doctor")
    assert refused.returncode == 6 and "'supervisr', which is no worker here" in refused.stderr
    conveyor(project, CHAIN + 'stall_after = "4h"\n')
    doctor = service.answer_of(cli(lab, "doctor"))
    assert doctor["escalation"]["escalate_to"] == ["supervisor", "owner"]
    assert doctor["escalation"]["stall_after"] == 4 * 3600
    assert "warning" not in doctor

    service.answer_of(cli(lab, "add", "--type", "alpha", "--title", "a", "--key", "t-held",
                          "--status", "todo"))
    service.answer_of(cli(lab, "service", "init"))
    service.answer_of(cli(lab, "service", "pause", "alpha"))
    status = service.answer_of(cli(lab, "service", "status"))
    assert status["pause"]["holding"] == ["alpha"]
    assert status["pause"]["waited"]["alpha"]["task"] == "t-held"
    assert status["pause"]["waited"]["alpha"]["waited_seconds"] >= 0
    assert "supervisor" not in status["pause"]["waited"]
