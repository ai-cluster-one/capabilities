#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""The two handler points and their one vocabulary: a worker's `before` hook
asked before a claim, its `after` hook asked once a raise is settled, and the
shipped handlers that read a harness's usage limit in how a raise ended - go,
hold until a moment, or escalate.

How a limit sentence is read is checked with no store, from the harnesses' own
sentences. What each verdict does - a hold writing nothing to the task, the
refusal clock counted from the first hold, attempts counted before a hook is
asked, a lane held until a moment, the one cool-down - is driven against a real
store with the harness replaced, and the service once through the CLI across a
restart. The store-backed checks read TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import datetime
import json
import sys
import time
import zoneinfo
from pathlib import Path

import pytest
from callva import harness_runner
from callva.harness_runner import FailureKind, Result
from callva.harness_runner.result import Failure

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_escalation as esc  # noqa: E402
import test_hooks as hooks  # noqa: E402
from test_hooks import lab, project, store, turns  # noqa: E402,F401  (fixtures)

mod = hooks.mod
needs_store = hooks.needs_store
add, answer, shown, tell = hooks.add, hooks.answer, hooks.shown, hooks.tell
raises_of, changes_of = hooks.raises_of, hooks.changes_of
tasks_cli, answer_of, poll_for = hooks.tasks_cli, hooks.answer_of, hooks.poll_for

UTC = datetime.timezone.utc
BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")


def ahead(seconds: float) -> str:
    return (datetime.datetime.now(UTC) + datetime.timedelta(seconds=seconds)) \
        .replace(microsecond=0).isoformat()


# --- Reading a harness's limit sentence --------------------------------------

# The sentences in the shape the harnesses print them, read from Claude Code 2.1.292
# and Codex 0.160.1 transcripts and binaries on 2026-10-06.
CLAUDE_FIVE_HOUR = "You've hit your session limit · resets 2:20am (Europe/Berlin)"
CLAUDE_SHORT = "You've hit your limit · resets 1:50pm (Europe/Berlin)"
CLAUDE_WEEKLY = "You've hit your weekly limit · resets Oct 9, 10am (Europe/Berlin)"
CLAUDE_WEEKLY_MINUTES = "You've hit your Opus limit · resets Oct 9, 10:30am (Europe/Berlin)"
CLAUDE_LEGACY = "Claude AI usage limit reached|1791500000"
CODEX_DATED = ("You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
               "to purchase more credits or try again at Sep 7th, 2026 9:06 AM.")
CODEX_TODAY = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro)"
               " or try again at 3:15 PM.")
CODEX_NO_MOMENT = "You've hit your usage limit."

NOW = datetime.datetime(2026, 10, 6, 21, 0, tzinfo=UTC)   # 23:00 on the 6th in Berlin


@pytest.mark.parametrize("said, expected", [
    (CLAUDE_FIVE_HOUR, datetime.datetime(2026, 10, 7, 2, 20, tzinfo=BERLIN)),
    (CLAUDE_SHORT, datetime.datetime(2026, 10, 7, 13, 50, tzinfo=BERLIN)),
    (CLAUDE_WEEKLY, datetime.datetime(2026, 10, 9, 10, 0, tzinfo=BERLIN)),
    (CLAUDE_WEEKLY_MINUTES, datetime.datetime(2026, 10, 9, 10, 30, tzinfo=BERLIN)),
    (CLAUDE_LEGACY, datetime.datetime.fromtimestamp(1791500000, UTC)),
    ("Claude usage limit reached. Your limit will reset at 5pm (Europe/Berlin).",
     datetime.datetime(2026, 10, 7, 17, 0, tzinfo=BERLIN)),
])
def test_claude_limit_sentences_name_their_reset(said, expected):
    assert mod._LIMIT_SAID.search(said)
    assert mod._reset_moment(said, NOW) == expected


def test_a_clock_time_already_past_today_is_tomorrows():
    morning = datetime.datetime(2026, 10, 7, 9, 0, tzinfo=BERLIN)
    moment = mod._reset_moment(CLAUDE_FIVE_HOUR, morning)
    assert moment == datetime.datetime(2026, 10, 8, 2, 20, tzinfo=BERLIN)


def test_codex_limit_sentences_name_their_reset_in_this_machines_zone():
    local = NOW.astimezone().tzinfo
    dated = mod._reset_moment(CODEX_DATED, datetime.datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
    assert (dated.year, dated.month, dated.day, dated.hour, dated.minute) == \
        (2026, 9, 7, 9, 6)
    assert dated.utcoffset() == datetime.datetime(2026, 9, 7, 9, 6).astimezone().utcoffset()
    today = mod._reset_moment(CODEX_TODAY, NOW)
    assert (today.hour, today.minute) == (15, 15) and today > NOW
    assert today.tzinfo is not None and local is not None
    assert mod._LIMIT_SAID.search(CODEX_DATED) and mod._LIMIT_SAID.search(CODEX_NO_MOMENT)
    assert mod._reset_moment(CODEX_NO_MOMENT, NOW) is None


class _Runner:
    FailureKind = FailureKind


def ending(said: str, *, harness: str = "claude", kind=FailureKind.QUOTA,
           ok: bool = False) -> Result:
    return Result(ok=ok, harness=harness, answer=said if harness == "claude" else "",
                  failure=None if ok else Failure(kind, said))


@pytest.mark.parametrize("said, harness", [
    (CLAUDE_FIVE_HOUR, "claude"), (CLAUDE_WEEKLY, "claude"), (CODEX_TODAY, "codex"),
])
def test_a_limit_ending_holds_the_lane_until_a_minute_past_the_reset(said, harness):
    before = datetime.datetime.now(UTC)
    hold = mod._limit_ending(ending(said, harness=harness), _Runner)
    reset = mod._reset_moment(said, before)
    until = datetime.datetime.fromisoformat(hold["until"])
    assert reset > before
    expected = min(reset + datetime.timedelta(seconds=mod._LANE_HOLD_MARGIN),
                   before + datetime.timedelta(seconds=mod._LANE_HOLD_LONGEST))
    assert abs((until - expected).total_seconds()) < 5
    assert until.utcoffset() == datetime.timedelta(0)
    assert hold["by"] == f"{harness} usage limit"
    assert said.split(".")[0][:20] in hold["said"]


def test_a_limit_naming_no_reset_holds_for_the_default():
    before = datetime.datetime.now(UTC)
    hold = mod._limit_ending(ending(CODEX_NO_MOMENT, harness="codex"), _Runner)
    until = datetime.datetime.fromisoformat(hold["until"])
    assert abs((until - before).total_seconds() - mod._LANE_HOLD_DEFAULT) < 5
    assert hold["by"] == "codex usage limit, no reset named"
    # The library naming a spent quota is enough, with or without a sentence.
    hold = mod._limit_ending(ending("429 Too Many Requests"), _Runner)
    assert hold["by"] == "claude usage limit, no reset named"


def test_other_endings_hold_no_lane():
    assert mod._limit_ending(ending(CLAUDE_FIVE_HOUR, ok=True), _Runner) is None
    assert mod._limit_ending(ending("connection reset", kind=FailureKind.ERROR),
                             _Runner) is None
    assert mod._limit_ending(ending("timed out", kind=FailureKind.TIMEOUT), _Runner) is None
    login = mod._limit_ending(ending("Not logged in · Please run /login",
                                     kind=FailureKind.NOT_AUTHENTICATED), _Runner)
    assert login["by"] == "claude not logged in"


# --- The cool-down, as declared ----------------------------------------------

def test_the_cool_down_is_read_from_the_old_keys_and_doctor_names_them(project):
    assert mod._worker("alpha")["limits"]["cool_down_seconds"] == mod._COOL_DOWN_DEFAULT
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\n"
                       "limits: {hold_seconds_on_exhaustion: 900}")
    worker = mod._worker("alpha")
    assert worker["limits"]["cool_down_seconds"] == 900
    assert "hold_seconds_on_exhaustion" not in worker["limits"]
    assert any("limits.hold_seconds_on_exhaustion is read as limits.cool_down_seconds"
               in one for one in mod._deprecations(mod._workers_report()[0]))
    settings = project / "capabilities" / "tasks" / "service"
    settings.mkdir(parents=True)
    (settings / "config.toml").write_text("version = 1\nretry_delay_seconds = 15\n")
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain")
    assert mod._worker("alpha")["limits"]["cool_down_seconds"] == 15
    said = mod._deprecations(mod._workers_report()[0])
    assert len(said) == 1 and "retry_delay_seconds is read as the cool-down" in said[0]
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\n"
                       "limits: {cool_down_seconds: 5}")
    assert mod._worker("alpha")["limits"]["cool_down_seconds"] == 5
    # The service settings still load with the old key, which it no longer reads.
    loaded = mod._read_service_settings(settings / "config.toml")
    assert "retry_delay_seconds" not in loaded


# --- Against a real store ----------------------------------------------------

def frozen(conn, schema: str, key: str) -> dict:
    """What a hold must leave as it was: the task row and its history."""
    tid = conn.execute(f"select id from {schema}.tasks where unique_key = %s",
                       (key,)).fetchone()[0]
    row = conn.execute(f"select pickup_at, updated_at, status, assignee from {schema}.tasks "
                       "where id = %s", (tid,)).fetchone()
    changes = conn.execute(f"select count(*) from {schema}.task_changes where task_id = %s",
                           (tid,)).fetchone()[0]
    trail = conn.execute(f"select count(*) from {schema}.task_activities where task_id = %s",
                         (tid,)).fetchone()[0]
    return {"row": row, "changes": changes, "trail": trail}


def episodes(conn, schema: str, key: str) -> int:
    return conn.execute(
        f"""select count(*) from {schema}.task_executions
             where metrics ? 'hold'
               and task_id = (select id from {schema}.tasks where unique_key = %s)""",
        (key,)).fetchone()[0]


@needs_store
@pytest.mark.parametrize("chain", [False, True])
def test_repeated_holds_write_nothing_to_the_task(project, store, turns, capsys, chain):
    entry, schema, conn = store
    hooks.hooked(project)
    if chain:
        esc.supervised(project)
        esc.conveyor(project)
    add(entry, capsys, "t-held")
    esc.aged(conn, schema, "t-held", 2)
    first = frozen(conn, schema, "t-held")
    for told in ({"exit": 75, "print": ahead(3600)}, {"exit": 75}, {"exit": 3},
                 {"exit": 75, "print": ahead(60)}):
        tell(project, "before", "t-held", **told)
        mod.cmd_run(entry, ["alpha", "--apply"])
        report = answer(capsys)
        assert report["claimed"] is None and report["passed_over"][0]["verdict"] == "hold"
    assert frozen(conn, schema, "t-held") == first
    assert changes_of(entry, capsys, "t-held") == []
    assert shown(entry, capsys, "t-held")["task"]["pickup_at"] is None
    # With a chain, the first hold is remembered once among the raises, as no raise.
    assert episodes(conn, schema, "t-held") == (1 if chain else 0)
    assert raises_of(entry, capsys, "t-held") == []
    mod.cmd_list(entry, ["--full"])
    [row] = [one for one in answer(capsys)["tasks"] if one["unique_key"] == "t-held"]
    assert row["runs"]["count"] == 0 and turns == []


@needs_store
def test_exit_76_escalates_at_once_with_the_hooks_last_line(project, store, turns, capsys):
    entry, _schema, _conn = store
    hooks.hooked(project)
    esc.supervised(project)
    esc.conveyor(project)
    add(entry, capsys, "t-wrong")
    add(entry, capsys, "t-fine")
    tell(project, "before", "t-wrong", exit=76, print="the branch was deleted upstream")
    mod.cmd_run(entry, ["alpha"])
    dry = answer(capsys)
    assert dry["would_escalate"][0]["task"] == "t-wrong"
    assert shown(entry, capsys, "t-wrong")["task"]["status"] == "todo"
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-fine" and turns == ["t-fine"]
    [moved] = report["escalated"]
    assert (moved["task"], moved["to"]) == ("t-wrong", "supervisor")
    assert moved["why"] == "the branch was deleted upstream"
    task = shown(entry, capsys, "t-wrong")
    assert (task["task"]["status"], task["task"]["assignee"]) == ("waiting", "supervisor")
    [said] = task["activities"]
    assert said["actor"] == "tasks:scan"
    assert said["description"].startswith(
        "Escalated from nobody to supervisor: the branch was deleted upstream.")


@needs_store
def test_exit_76_without_a_chain_is_a_hold(project, store, turns, capsys):
    entry, _schema, _conn = store
    hooks.hooked(project)
    add(entry, capsys, "t-nowhere")
    tell(project, "before", "t-nowhere", exit=76, print="cannot proceed")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    [held] = report["passed_over"]
    assert held["verdict"] == "hold" and "until" not in held
    assert "no escalation chain" in held["why"] and held["said"] == "cannot proceed"
    assert shown(entry, capsys, "t-nowhere")["task"]["status"] == "todo"


# --- The refusal clock -------------------------------------------------------

@needs_store
def test_the_clock_runs_from_the_first_hold_not_from_the_last_move(project, store, turns,
                                                                  capsys):
    """A task that waited its turn past the ceiling is not escalated at its
    first hold; held continuously for the ceiling, it is."""
    entry, schema, conn = store
    hooks.hooked(project)
    esc.supervised(project)
    esc.conveyor(project, esc.CHAIN + 'stall_after = "1h"\n')
    add(entry, capsys, "t-late")
    esc.aged(conn, schema, "t-late", 30)
    tell(project, "before", "t-late", exit=3, print="shut")
    for _ in range(2):
        mod.cmd_run(entry, ["alpha", "--apply"])
        assert "escalated" not in answer(capsys)
    assert episodes(conn, schema, "t-late") == 1
    esc.held_for(conn, schema, "t-late", 0.5)
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert "escalated" not in answer(capsys)
    esc.held_for(conn, schema, "t-late", 0.6)
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert moved["task"] == "t-late" and "past the 1h ceiling" in moved["why"]
    assert "the hook said: shut" in moved["why"]


@needs_store
def test_the_clock_errs_toward_later(project, store, turns, capsys):
    """An episode stops counting once a raise starts on the task, once the task
    moves, and once its lane is held since; and a lost episode only restarts
    the clock. None of them escalates."""
    entry, schema, conn = store
    hooks.hooked(project)
    esc.supervised(project)
    esc.conveyor(project, esc.CHAIN + 'stall_after = "1h"\n')
    tid = add(entry, capsys, "t-reset")
    esc.aged(conn, schema, "t-reset", 30)
    tell(project, "before", "t-reset", exit=3, print="shut")

    def held_twice_past_the_ceiling(between) -> list:
        mod.cmd_run(entry, ["alpha", "--apply"])
        assert "escalated" not in answer(capsys)
        conn.execute(f"""update {schema}.task_executions
                            set started_at = now() - interval '2 hours',
                                ended_at = now() - interval '2 hours'
                          where metrics ? 'hold' and task_id = %s""", (tid,))
        between()
        mod.cmd_run(entry, ["alpha", "--apply"])
        return answer(capsys).get("escalated") or []

    # A raise started after the first hold: the task was taken, the episode ended.
    def a_raise():
        conn.execute(f"""insert into {schema}.task_executions
                           (task_id, attempt, worker, status, started_at, ended_at)
                         values (%s, 1, 'alpha', 'ok', now() - interval '90 minutes',
                                 now() - interval '89 minutes')""", (tid,))
    assert held_twice_past_the_ceiling(a_raise) == []
    conn.execute(f"delete from {schema}.task_executions where task_id = %s", (tid,))

    # Its lane was held after the first hold: that time is not refusal.
    other = add(entry, capsys, "t-other", kind="beta")

    def a_lane_hold():
        conn.execute(f"""insert into {schema}.task_executions
                           (task_id, attempt, worker, status, metrics, started_at, ended_at)
                         values (%s, 1, 'alpha', 'failed', %s::jsonb,
                                 now() - interval '100 minutes', now() - interval '100 minutes')""",
                     (other, json.dumps({"exhausted": True, "lane_hold": {
                         "until": (datetime.datetime.now(UTC)
                                   - datetime.timedelta(minutes=30)).isoformat(),
                         "by": "claude usage limit"}})))
    assert held_twice_past_the_ceiling(a_lane_hold) == []
    conn.execute(f"delete from {schema}.task_executions where task_id in (%s, %s)",
                 (tid, other))

    # The record of the first hold is lost: the next hold starts the clock again.
    def lost():
        conn.execute(f"delete from {schema}.task_executions where task_id = %s", (tid,))
    assert held_twice_past_the_ceiling(lost) == []
    assert shown(entry, capsys, "t-reset")["task"]["status"] == "todo"


@needs_store
def test_attempts_are_counted_before_the_hook_is_asked(project, store, turns, capsys):
    entry, schema, conn = store
    hooks.hooked(project)
    esc.supervised(project)
    esc.conveyor(project)
    add(entry, capsys, "t-spent")
    esc.aged(conn, schema, "t-spent", 3)
    esc.raised(conn, schema, "t-spent", 3, hours_ago=2)
    tell(project, "before", "t-spent", exit=75, print=ahead(3600))
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    [moved] = report["escalated"]
    assert moved["task"] == "t-spent" and "raised 3 times in place" in moved["why"]
    assert hooks.seen(project, "before") == []
    assert shown(entry, capsys, "t-spent")["task"]["assignee"] == "supervisor"


# --- The after-raise point and the limit handlers ----------------------------

@pytest.fixture
def ends(store, monkeypatch, capsys):
    """The harness replaced by one that ends each turn as the test sets it:
    `said` a limit sentence and `harness` claude or codex, or finishing the task
    when `said` is None."""
    entry, _schema, _conn = store
    told: dict = {"said": None, "harness": "claude", "leave": False, "ran": []}

    class Worker:
        Profile, Session, FailureKind = (harness_runner.Profile, harness_runner.Session,
                                         FailureKind)
        find_profile_file = staticmethod(harness_runner.find_profile_file)
        ProfileNotFound = harness_runner.ProfileNotFound

        def run(self, prompt, profile, cwd, *, session=None, environ=None, **kw):
            execution = kw["extra_env"]["TASKS_EXECUTION"]
            with mod._connect(entry) as conn, conn.cursor() as cur:
                cur.execute(f"""select t.unique_key from {mod.SCHEMA}.task_executions e
                                  join {mod.SCHEMA}.tasks t on t.id = e.task_id
                                 where e.id::text = %s""", (execution,))
                key = cur.fetchone()["unique_key"]
            told["ran"].append(key)
            if told["said"] is not None:
                return Result(ok=False, harness=told["harness"],
                              answer=told["said"] if told["harness"] == "claude" else "",
                              session_id=session.id, model="m",
                              failure=Failure(told.get("kind", FailureKind.QUOTA),
                                              told["said"]))
            monkeypatch.setenv("TASKS_EXECUTION", execution)
            mod.cmd_activity(entry, [key, "done"])
            if not told["leave"]:
                mod.cmd_set(entry, [key, "--status", "complete"])
            capsys.readouterr()
            monkeypatch.delenv("TASKS_EXECUTION")
            return Result(ok=True, harness="claude", answer="done", session_id=session.id,
                          model="m", cost_usd=0.0, duration_ms=1, num_turns=1)

    monkeypatch.setattr(mod, "_harness_runner", Worker)
    return told


def lane_until(conn, schema: str, key: str) -> datetime.datetime:
    return conn.execute(
        f"""select (metrics -> 'lane_hold' ->> 'until')::timestamptz from {schema}.task_executions
             where metrics ? 'lane_hold'
               and task_id = (select id from {schema}.tasks where unique_key = %s)""",
        (key,)).fetchone()[0]


def lift_lane(conn, schema: str) -> None:
    """Let every lane hold's moment pass."""
    conn.execute(f"""update {schema}.task_executions
                        set metrics = jsonb_set(metrics, '{{lane_hold,until}}',
                                                to_jsonb((now() - interval '1 second')::text))
                      where metrics ? 'lane_hold'""")


@needs_store
@pytest.mark.parametrize("said, harness", [
    (CLAUDE_FIVE_HOUR, "claude"), (CLAUDE_WEEKLY, "claude"), (CODEX_TODAY, "codex"),
    (CODEX_NO_MOMENT, "codex"),
])
def test_a_usage_limit_holds_the_lane_and_spends_no_attempt(project, store, ends, capsys,
                                                            said, harness):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    add(entry, capsys, "t-first")
    add(entry, capsys, "t-second")
    ends.update(said=said, harness=harness)
    started = datetime.datetime.now(UTC)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-first" and report["returned_unspent"] is True
    held = report["lane_held"]
    until = datetime.datetime.fromisoformat(held["until"])
    reset = mod._reset_moment(said, started)
    if reset is None:
        assert abs((until - started).total_seconds() - mod._LANE_HOLD_DEFAULT) < 10
    else:
        assert until > reset and until - reset <= datetime.timedelta(seconds=61)
    assert lane_until(conn, schema, "t-first") == until
    [raised] = raises_of(entry, capsys, "t-first")
    assert raised["status"] == "failed" and raised["metrics"]["exhausted"] is True
    # Not an attempt: nothing counts it in place.
    with mod._connect(entry) as c, c.cursor() as cur:
        assert mod._in_place(cur, raised["task_id"])["raises_in_place"] == 0
    # The lane is held for every claim: by hand, dry or applied, and the service's.
    ends.update(said=None)
    mod.cmd_run(entry, ["alpha"])
    dry = answer(capsys)
    assert dry["would_claim"] is None and dry["lane_held"]["until"] == held["until"]
    mod.cmd_run(entry, ["alpha", "--apply"])
    applied = answer(capsys)
    assert applied["claimed"] is None and applied["lane_held"]["until"] == held["until"]
    worker = mod._worker("alpha")
    assert mod._would_take(entry, worker, None)["would_claim"] is None
    with mod._connect(entry) as c:
        moment = mod._service_next_moment(c, {"alpha": 60})
    assert moment is not None and moment > 0
    # Another worker's lane is not held.
    hooks.write_worker(project, "beta", "takes: [beta]\nprofile: plain")
    add(entry, capsys, "t-beta", kind="beta")
    mod.cmd_run(entry, ["beta", "--apply"])
    assert answer(capsys)["claimed"] == "t-beta"
    # Once the moment passes, the lane runs again.
    lift_lane(conn, schema)
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert answer(capsys)["claimed"] == "t-second"
    assert ends["ran"] == ["t-first", "t-beta", "t-second"]


@needs_store
def test_a_lane_held_by_a_limit_runs_no_stall_clock_until_its_moment(project, store, ends,
                                                                     capsys):
    """A task its hook held before the limit, and holds again once the lane
    runs, is not escalated for the time the lane stood held: its clock starts
    again from the hold after the moment."""
    entry, schema, conn = store
    hooks.hooked(project)
    esc.supervised(project)
    esc.conveyor(project, esc.CHAIN + 'stall_after = "1h"\n')
    add(entry, capsys, "t-gated")
    esc.aged(conn, schema, "t-gated", 30)
    tell(project, "before", "t-gated", exit=3, print="gate shut")
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert "escalated" not in answer(capsys)
    esc.held_for(conn, schema, "t-gated", 0.9)
    # Another task of the lane runs into the limit.
    add(entry, capsys, "t-limit")
    ends.update(said=CLAUDE_FIVE_HOUR)
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert answer(capsys)["claimed"] == "t-limit"
    mod.cmd_run(entry, ["alpha", "--apply"])
    assert answer(capsys)["claimed"] is None
    # The moment passes once the task's first hold is past the ceiling.
    esc.held_for(conn, schema, "t-gated", 0.5)
    lift_lane(conn, schema)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert "escalated" not in report
    assert shown(entry, capsys, "t-gated")["task"]["status"] == "todo"
    assert episodes(conn, schema, "t-gated") == 2


@needs_store
@pytest.mark.parametrize("told, held, escalated", [
    ({"exit": 0}, False, False),
    ({"exit": 75, "print": "later"}, True, False),
    ({"exit": 76, "print": "the change needs a person"}, False, True),
])
def test_the_after_hook_answers_go_hold_or_escalate(project, store, ends, capsys, told,
                                                   held, escalated):
    """The turn leaves its task where it was; the hook's answer decides what
    follows."""
    entry, _schema, _conn = store
    ends.update(leave=True)
    hooks.hooked(project, before=False, after=True)
    esc.supervised(project)
    esc.conveyor(project)
    add(entry, capsys, "t-done")
    add(entry, capsys, "t-next")
    moment = ahead(3600)
    if told.get("print") == "later":
        told = {**told, "print": moment}
    tell(project, "after", "t-done", **told)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-done"
    [seen] = hooks.seen(project, "after")
    assert seen["env"]["TASKS_HARNESS_SAID"] == "done"
    assert seen["env"]["TASKS_HARNESS_FAILURE"] == ""
    assert seen["env"]["TASKS_OUTCOME"] == "failed"
    assert seen["env"]["TASKS_LANDED"] == "todo"
    assert ("lane_held" in report) is held
    if held:
        assert report["lane_held"]["until"] == moment
        assert report["after_hook"]["verdict"] == "hold"
    if escalated:
        [moved] = report["escalated"]
        assert moved["why"] == "the change needs a person" and moved["to"] == "supervisor"
        task = shown(entry, capsys, "t-done")["task"]
        assert (task["status"], task["assignee"]) == ("waiting", "supervisor")
    else:
        assert "escalated" not in report
        assert shown(entry, capsys, "t-done")["task"]["status"] == "todo"
    # The next claim: the lane held, or the next task taken, t-done cooling.
    mod.cmd_run(entry, ["alpha", "--apply"])
    after = answer(capsys)
    assert after["claimed"] == (None if held else "t-next")
    if held:
        assert after["lane_held"]["until"] == moment


@needs_store
def test_an_after_hook_does_not_reopen_a_finished_task(project, store, turns, capsys):
    entry, _schema, _conn = store
    hooks.hooked(project, before=False, after=True)
    esc.supervised(project)
    esc.conveyor(project)
    add(entry, capsys, "t-over")
    tell(project, "after", "t-over", exit=76, print="look at this")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert "escalated" not in report and report["after_hook"]["verdict"] == "escalate"
    assert shown(entry, capsys, "t-over")["task"]["status"] == "complete"
    assert report["lane_held"]["by"] == "the after hook of worker 'alpha'"


# --- The cool-down -----------------------------------------------------------

@needs_store
def test_a_failed_raise_cools_its_task_for_a_run_by_hand_too(project, store, capsys,
                                                           monkeypatch):
    entry, schema, conn = store
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\n"
                       "limits: {cool_down_seconds: 2}")
    tid = add(entry, capsys, "t-cool")
    conn.execute(f"""insert into {schema}.task_executions
                       (task_id, attempt, worker, status, started_at, ended_at)
                     values (%s, 1, 'alpha', 'failed', now(), now())""", (tid,))
    mod.cmd_run(entry, ["alpha"])
    assert answer(capsys)["would_claim"] is None
    with mod._connect(entry) as c:
        moment = mod._service_next_moment(c, {"alpha": 2})
    assert moment is not None and 0 < moment <= 2
    time.sleep(2.2)
    mod.cmd_run(entry, ["alpha"])
    assert answer(capsys)["would_claim"] == "t-cool"
    # A raise that moved its task cools nothing.
    conn.execute(f"""update {schema}.task_executions set status = 'ok', ended_at = now()
                      where task_id = %s""", (tid,))
    mod.cmd_run(entry, ["alpha"])
    assert answer(capsys)["would_claim"] == "t-cool"
    # A task named with --key is taken whatever cools it.
    conn.execute(f"""update {schema}.task_executions set status = 'failed', ended_at = now()
                      where task_id = %s""", (tid,))
    mod.cmd_run(entry, ["alpha", "--key", "t-cool"])
    assert answer(capsys)["would_claim"] == "t-cool"


# --- The service, across a restart -------------------------------------------

def settings_file(lab) -> Path:
    return lab["project"] / "capabilities" / "tasks" / "service" / "config.toml"


def started_turns(lab) -> list[str]:
    lines = answer_of(tasks_cli(lab, "service", "logs", "--tail", "400"))["lines"]
    return [line for line in lines if " started: worker alpha" in line]


@needs_store
def test_the_service_keeps_a_lane_hold_across_a_restart(lab):
    """A raise whose after hook held the lane: the service starts no turn on
    that lane before the moment, also after it is restarted, and runs the lane
    once the moment passes."""
    project = lab["project"]
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\nhooks:\n"
                       f"  after: {sys.executable} hooks/hook.py hooks/after")
    answer_of(tasks_cli(lab, "service", "init"))
    settings_file(lab).write_text("version = 1\npoll_seconds = 3600\n"
                                  "shutdown_grace_seconds = 5\n")
    moment = ahead(30)
    tell(project, "after", "t-one", exit=75, print=moment)
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "one", "--key", "t-one",
                        "--status", "todo"))
    answer_of(tasks_cli(lab, "service", "start"))
    poll_for(lambda: answer_of(tasks_cli(lab, "show", "t-one"))["task"]["status"]
             == "complete", 60)
    poll_for(lambda: any("lane held until" in line for line in answer_of(
        tasks_cli(lab, "service", "logs", "--tail", "400"))["lines"]), 30)
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "two", "--key", "t-two",
                        "--status", "todo"))
    time.sleep(2)
    answer_of(tasks_cli(lab, "service", "stop"))
    answer_of(tasks_cli(lab, "service", "start"))
    time.sleep(3)
    assert len(started_turns(lab)) == 1
    assert answer_of(tasks_cli(lab, "show", "t-two"))["task"]["status"] == "todo"
    poll_for(lambda: answer_of(tasks_cli(lab, "show", "t-two"))["task"]["status"]
             == "complete", 60)
    [raised] = answer_of(tasks_cli(lab, "runs", "t-two"))["executions"]
    assert datetime.datetime.fromisoformat(raised["started_at"]) >= \
        datetime.datetime.fromisoformat(moment)
    answer_of(tasks_cli(lab, "service", "stop", "--end-turns"))


@needs_store
def test_the_service_escalates_a_held_task_by_the_clock_kept_across_a_restart(lab):
    """The first hold the service's turn met is remembered in the store, so a
    service started again escalates the task once it has been held past the
    ceiling, and the hold before it starts no turn for it before its poll."""
    import psycopg
    project = lab["project"]
    hooks.hooked(project)
    (project / "capabilities" / "tasks" / "conveyor.toml").write_text(
        'escalate_to = ["owner"]\nstall_after = "1h"\n')
    answer_of(tasks_cli(lab, "service", "init"))
    settings_file(lab).write_text("version = 1\npoll_seconds = 3600\n"
                                  "shutdown_grace_seconds = 5\n")
    moment = ahead(25)
    tell(project, "before", "t-gate", exit=75, print=moment)
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "gate", "--key", "t-gate",
                        "--status", "todo"))
    answer_of(tasks_cli(lab, "service", "start"))
    poll_for(lambda: len(hooks.seen(project, "before")) >= 1, 60)
    # Held until a moment ahead: no turn asks about it again before it comes.
    time.sleep(4)
    assert len(hooks.seen(project, "before")) == 1
    poll_for(lambda: len(hooks.seen(project, "before")) >= 2, 60)
    assert datetime.datetime.now(UTC) >= datetime.datetime.fromisoformat(moment)
    tell(project, "before", "t-gate", exit=3, print="still shut")
    answer_of(tasks_cli(lab, "service", "stop"))
    shown_ = answer_of(tasks_cli(lab, "show", "t-gate"))["task"]
    assert shown_["status"] == "todo" and shown_["pickup_at"] is None
    schema = json.loads((project / "capabilities" / "tasks" / "connections.json")
                        .read_text())["connections"]["local"]["db_schema"]
    with psycopg.connect(hooks.DSN, autocommit=True) as conn:
        [count] = conn.execute(f"""select count(*) from {schema}.task_executions
                                     where metrics ? 'hold'""").fetchone()
        assert count == 1
        # The task has stood in its place for three hours, held for two of them.
        conn.execute(f"update {schema}.tasks set created_at = now() - interval '3 hours'")
        conn.execute(f"update {schema}.task_changes set changed_at = now() - interval '3 hours'")
        conn.execute(f"""update {schema}.task_executions
                            set started_at = now() - interval '2 hours',
                                ended_at = now() - interval '2 hours'
                          where metrics ? 'hold'""")
    answer_of(tasks_cli(lab, "service", "start"))
    poll_for(lambda: answer_of(tasks_cli(lab, "show", "t-gate"))["task"]["assignee"]
             == "owner", 60)
    answer_of(tasks_cli(lab, "service", "stop", "--end-turns"))


@pytest.mark.parametrize("kind", [FailureKind.ERROR, FailureKind.CRASH,
                                  FailureKind.INVALID_OUTPUT])
def test_a_codex_limit_sentence_holds_whatever_failure_the_library_names(kind):
    """Codex's sentence is the fact: the library may name the failure something
    other than a spent quota, and the lane is held all the same."""
    before = datetime.datetime.now(UTC)
    hold = mod._limit_ending(ending(CODEX_TODAY, harness="codex", kind=kind), _Runner)
    reset = mod._reset_moment(CODEX_TODAY, before)
    until = datetime.datetime.fromisoformat(hold["until"])
    assert hold["by"] == "codex usage limit"
    assert until - reset <= datetime.timedelta(seconds=mod._LANE_HOLD_MARGIN + 5)
    hold = mod._limit_ending(ending(CODEX_NO_MOMENT, harness="codex", kind=kind), _Runner)
    assert hold["by"] == "codex usage limit, no reset named"


@needs_store
def test_a_codex_limit_under_another_failure_kind_holds_the_lane(project, store, ends,
                                                                 capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-first")
    add(entry, capsys, "t-second")
    ends.update(said=CODEX_TODAY, harness="codex", kind=FailureKind.ERROR)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-first" and report["returned_unspent"] is True
    assert report["lane_held"]["by"] == "codex usage limit"
    [raised] = raises_of(entry, capsys, "t-first")
    assert raised["metrics"]["exhausted"] is True and "lane_hold" in raised["metrics"]
    ends.update(said=None)
    mod.cmd_run(entry, ["alpha", "--apply"])
    applied = answer(capsys)
    assert applied["claimed"] is None and applied["lane_held"]["by"] == "codex usage limit"
    assert ends["ran"] == ["t-first"]
