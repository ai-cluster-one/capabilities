#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""`blocked_by` as a field of its own: a column of task ids, validated when it is
written, honoured on every open task by the claim, and moved out of the metadata
it used to live in.

What the schema declares is checked with no store. Writing, claiming, the scan,
the backfill and the compatibility kept for one release are driven against a
real store with the harness replaced. The store-backed checks read
TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_escalation as esc  # noqa: E402
import test_hooks as hooks  # noqa: E402
from test_hooks import project, store, turns  # noqa: E402,F401  (fixtures)

mod = hooks.mod
needs_store = hooks.needs_store
add, answer, shown = hooks.add, hooks.answer, hooks.shown
HERE, THERE = hooks.HERE, "prj_elsewhere"
SCHEMA_SQL = (Path(_cli.CAPABILITY_DIR) / "schema.sql").read_text()
NOWHERE = "00000000-0000-4000-8000-000000000000"


def refused(capsys, verb, entry, args, code: int = 4) -> dict:
    with pytest.raises(SystemExit) as stopped:
        verb(entry, args)
    assert stopped.value.code == code, capsys.readouterr()
    return json.loads(capsys.readouterr().err)["error"]


def set_(entry, capsys, *args) -> dict:
    mod.cmd_set(entry, list(args))
    return answer(capsys)


def claim(entry, capsys, *args) -> dict:
    mod.cmd_claim(entry, ["--worker", "a worker", *args])
    return answer(capsys)


def theirs(entry, capsys, monkeypatch, key: str, status: str = "todo") -> str:
    """A task of another project sharing the store, by its id."""
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_add(entry, ["--type", "alpha", "--title", key, "--key", key, "--status", status])
    tid = answer(capsys)["created"]
    monkeypatch.setattr(mod, "PROJECT", HERE)
    return tid


# --- The typed home ----------------------------------------------------------

def test_the_schema_declares_the_column_for_a_new_store_and_an_old_one():
    assert "  blocked_by   uuid[]      not null default '{}'," in SCHEMA_SQL
    assert ("alter table tasks.tasks add column if not exists blocked_by uuid[] "
            "not null default '{}';") in SCHEMA_SQL
    # A store without it reads as behind, and adding it is additive: the next
    # command brings the store up to it on its own.
    assert ("tasks", "blocked_by") in mod._COLUMNS
    assert "tasks.blocked_by" not in mod._NOT_ADDITIVE


def test_the_help_states_the_field_and_its_migration():
    said = " ".join(mod.__doc__.split())
    for needle in ("--blocked-by TASK[,TASK...]",
                   "Clearable: objective, description, assignee, pickup, unique-key, "
                   "blocked-by",
                   "No task is claimable while its `blocked_by` lists a task that has "
                   "not ended, in `todo` as much as in `waiting`",
                   "`blocked_by` is no metadata key",
                   "both stop taking the key in the next release",
                   "`migrate --apply` moves into it",
                   "`would_backfill`"):
        assert needle in said, needle


@needs_store
def test_migrate_adds_the_column_to_an_older_store(store, capsys, monkeypatch):
    entry, schema, conn = store
    conn.execute(f"alter table {schema}.tasks drop column blocked_by")
    monkeypatch.setattr(mod, "BLOCKED_BY_KEPT", None)
    mod.cmd_migrate(entry, [])
    report = answer(capsys)
    assert report["would_add"] == ["tasks.blocked_by"] and report["applied"] is False
    mod.cmd_migrate(entry, ["--apply"])
    report = answer(capsys)
    assert report["added"] == ["tasks.blocked_by"] and report["applied"] is True
    column = conn.execute(
        """select data_type, is_nullable, column_default from information_schema.columns
            where table_schema = %s and table_name = 'tasks' and column_name = 'blocked_by'""",
        (schema,)).fetchone()
    assert column == ("ARRAY", "NO", "'{}'::uuid[]")


@needs_store
def test_a_store_without_the_column_is_read_as_before(store, capsys, monkeypatch):
    """A connection that cannot bring the store up to date is served as it would
    have been: every task blocked by nothing, and a write of blockers refused."""
    entry, schema, conn = store
    add(entry, capsys, "b-old")
    conn.execute(f"alter table {schema}.tasks drop column blocked_by")
    monkeypatch.setattr(mod, "BLOCKED_BY_KEPT", None)
    for flags in ([], ["--full"]):
        mod.cmd_list(entry, flags)
        [row] = answer(capsys)["tasks"]
        assert row["blocked_by"] == [] and row["blocked_by_status"] == {}
    assert claim(entry, capsys)["task"]["unique_key"] == "b-old"
    add(entry, capsys, "b-other")
    error = refused(capsys, mod.cmd_set, entry, ["b-other", "--blocked-by", "b-old"], 6)
    assert error["code"] == "schema_behind"


# --- Writing it --------------------------------------------------------------

@needs_store
def test_blockers_are_written_by_id_from_keys_and_ids(store, capsys, monkeypatch):
    entry, schema, conn = store
    first = add(entry, capsys, "b-first")
    second = add(entry, capsys, "b-second")
    add(entry, capsys, "b-task")
    elsewhere = theirs(entry, capsys, monkeypatch, "b-there")
    # Keys resolve within the project, ids in any; each is kept once, in order.
    written = set_(entry, capsys, "b-task", "--blocked-by",
                   f"b-first, {second},b-first,{elsewhere}")
    assert written["task"]["blocked_by"] == [first, second, elsewhere]
    assert written["fields"] == ["blocked_by"]
    # `set` names the whole list.
    assert set_(entry, capsys, "b-task", "--blocked-by", "b-second")["task"]["blocked_by"] \
        == [second]
    assert set_(entry, capsys, "b-task", "--clear", "blocked-by")["task"]["blocked_by"] == []
    # `add` takes it too.
    mod.cmd_add(entry, ["--type", "alpha", "--title", "x", "--key", "b-new",
                        "--blocked-by", "b-first"])
    answer(capsys)
    assert shown(entry, capsys, "b-new")["task"]["blocked_by"] == [first]


@needs_store
def test_a_name_that_is_no_task_here_is_refused(store, capsys, monkeypatch):
    entry, schema, conn = store
    add(entry, capsys, "b-task")
    theirs(entry, capsys, monkeypatch, "b-there")
    for name in ("b-nowhere", NOWHERE, "b-there"):
        error = refused(capsys, mod.cmd_set, entry, ["b-task", "--blocked-by", name])
        assert error["code"] == "policy"
        assert error["message"] == (f"a blocked_by may not name blockers that can never "
                                    f"all end: {name!r} names no task (a key names a task "
                                    f"only in {HERE})")
    error = refused(capsys, mod.cmd_add, entry, ["--type", "alpha", "--title", "x",
                                                 "--blocked-by", "b-nowhere"])
    assert "'b-nowhere' names no task" in error["message"]
    assert shown(entry, capsys, "b-task")["task"]["blocked_by"] == []


@needs_store
def test_itself_and_a_cycle_are_refused(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "b-a")
    add(entry, capsys, "b-b")
    add(entry, capsys, "b-c")
    error = refused(capsys, mod.cmd_set, entry, ["b-a", "--blocked-by", "b-a"])
    assert error["message"].endswith(": 'b-a' names itself")
    set_(entry, capsys, "b-a", "--blocked-by", "b-b")
    set_(entry, capsys, "b-b", "--blocked-by", "b-c")
    error = refused(capsys, mod.cmd_set, entry, ["b-c", "--blocked-by", "b-a"])
    assert error["code"] == "policy"
    assert error["message"].endswith("they wait on it in turn: b-c -> b-a -> b-b -> b-c")
    assert shown(entry, capsys, "b-c")["task"]["blocked_by"] == []
    # Through a blocker that has ended there is no cycle: it holds nothing.
    set_(entry, capsys, "b-b", "--status", "complete")
    assert set_(entry, capsys, "b-c", "--blocked-by", "b-a")["task"]["blocked_by"]


@needs_store
def test_the_flag_is_read_strictly(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "b-a")
    add(entry, capsys, "b-b")
    error = refused(capsys, mod.cmd_set, entry, ["b-a", "--blocked-by", " , "], 6)
    assert "--clear blocked-by" in error["hint"]
    error = refused(capsys, mod.cmd_set, entry, ["b-a", "--blocked-by", "b-b",
                                                 "--clear", "blocked-by"], 6)
    assert "cannot be given together" in error["message"]


# --- Honoured on every open task ---------------------------------------------

@needs_store
def test_a_todo_task_with_an_open_blocker_is_offered_to_no_claim(store, capsys, project,
                                                                 turns):
    entry, schema, conn = store
    add(entry, capsys, "b-blocker")
    set_(entry, capsys, "b-blocker", "--assignee", "the owner", "--status", "waiting")
    add(entry, capsys, "b-blocked")
    set_(entry, capsys, "b-blocked", "--blocked-by", "b-blocker")
    assert claim(entry, capsys)["claimed"] is None
    mod.cmd_run(entry, ["alpha"])
    assert answer(capsys)["would_claim"] is None
    # Named with --key it is refused, naming why.
    error = refused(capsys, mod.cmd_claim, entry, ["--worker", "a worker", "--key",
                                                   "b-blocked"], 6)
    assert "waits on the tasks its blocked_by names" in error["message"]
    # Nor is it in the queue a worker consumes.
    mod.cmd_ready(entry, [])
    assert [row["unique_key"] for row in answer(capsys)["tasks"]] == []
    # Once every blocker has ended it is taken, and the ended blocker stays listed.
    set_(entry, capsys, "b-blocker", "--status", "complete")
    mod.cmd_ready(entry, [])
    assert [row["unique_key"] for row in answer(capsys)["tasks"]] == ["b-blocked"]
    taken = claim(entry, capsys)
    assert taken["task"]["unique_key"] == "b-blocked"
    blocker = shown(entry, capsys, "b-blocker")["task"]["id"]
    assert shown(entry, capsys, "b-blocked")["task"]["blocked_by"] == [blocker]


@needs_store
def test_a_closed_blocker_holds_nothing_and_stays_recorded(store, capsys):
    entry, schema, conn = store
    one = add(entry, capsys, "b-one")
    two = add(entry, capsys, "b-two")
    set_(entry, capsys, "b-one", "--assignee", "the owner", "--status", "waiting")
    set_(entry, capsys, "b-two", "--assignee", "the owner", "--status", "waiting")
    add(entry, capsys, "b-after")
    set_(entry, capsys, "b-after", "--blocked-by", "b-one,b-two")
    set_(entry, capsys, "b-one", "--status", "closed")
    # One open blocker still holds it.
    assert claim(entry, capsys)["claimed"] is None
    set_(entry, capsys, "b-two", "--status", "complete")
    assert claim(entry, capsys)["task"]["unique_key"] == "b-after"
    assert shown(entry, capsys, "b-after")["task"]["blocked_by"] == [one, two]


@needs_store
def test_a_waiting_task_returns_on_its_own_when_its_blockers_end(store, capsys,
                                                                 monkeypatch):
    entry, schema, conn = store
    here = add(entry, capsys, "b-here")
    elsewhere = theirs(entry, capsys, monkeypatch, "b-there")
    add(entry, capsys, "b-wait")
    set_(entry, capsys, "b-wait", "--blocked-by", f"b-here,{elsewhere}",
         "--status", "waiting", "--assignee", "the owner")
    set_(entry, capsys, "b-here", "--status", "complete")
    assert claim(entry, capsys)["returned"] == []
    # Another project's blocker, named by id, ends the wait as well.
    conn.execute(f"update {schema}.tasks set status = 'complete' where id = %s",
                 (elsewhere,))
    returned = claim(entry, capsys)
    assert returned["returned"] == ["b-wait"]
    task = shown(entry, capsys, "b-wait")["task"]
    assert task["blocked_by"] == [here, elsewhere]


# --- Dead ends read the column -----------------------------------------------

@needs_store
def test_a_todo_task_blocked_by_a_draft_is_escalated(project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    mod.cmd_add(entry, ["--type", "alpha", "--title", "d", "--key", "b-draft"])
    answer(capsys)
    add(entry, capsys, "b-held")
    set_(entry, capsys, "b-held", "--blocked-by", "b-draft")
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert moved["task"] == "b-held"
    assert moved["why"] == "its blockers can never all end: 'b-draft' is a draft, which " \
                           "only a person releases"
    task = shown(entry, capsys, "b-held")["task"]
    assert (task["status"], task["blocked_by"]) == ("waiting", [])


# --- The backfill ------------------------------------------------------------

@needs_store
def test_the_backfill_moves_what_resolves_and_reports_the_rest(store, capsys,
                                                               monkeypatch):
    entry, schema, conn = store
    first = add(entry, capsys, "f-first")
    second = add(entry, capsys, "f-second")
    elsewhere = theirs(entry, capsys, monkeypatch, "f-there")

    def old(key: str, kept, project: str = HERE) -> str:
        return str(conn.execute(
            f"""insert into {schema}.tasks (project_id, type, title, unique_key, status,
                                            metadata, updated_at)
                values (%s, 'alpha', %s, %s, 'waiting', %s::jsonb, '2026-01-01T00:00:00Z')
                returning id""",
            (project, key, key, json.dumps({"blocked_by": kept, "cost_total": 1}))
        ).fetchone()[0])

    mixed = old("f-mixed", ["f-first", second, elsewhere, "f-there", "f-gone", " "])
    whole = old("f-whole", ["f-second"])
    junk = old("f-junk", "not a list")
    gone = old("f-gone-only", ["f-gone"])
    keyed_there = old("f-their", ["f-there"], project=THERE)

    mod.cmd_migrate(entry, [])
    dry = answer(capsys)["would_backfill"]
    assert (dry["tasks"], dry["moved"], dry["tasks_written"], dry["unresolvable"]) == \
        (5, 5, 3, 3)
    assert dry["by_project"][HERE]["unresolvable"] == [
        {"task": "f-mixed", "task_id": mixed, "names": ["f-there", "f-gone"],
         "why": "names no task"},
        {"task": "f-junk", "task_id": junk, "names": "not a list",
         "why": "not a list of tasks"},
        {"task": "f-gone-only", "task_id": gone, "names": ["f-gone"],
         "why": "names no task"}]
    assert dry["by_project"][THERE] == {"tasks": 1, "moved": 1, "unresolvable": []}
    # The dry run wrote nothing.
    assert conn.execute(f"select blocked_by from {schema}.tasks where id = %s",
                        (mixed,)).fetchone()[0] == []

    mod.cmd_migrate(entry, ["--apply"])
    applied = answer(capsys)
    assert applied["backfilled"]["moved"] == 5 and "warning" in applied

    def row(tid):
        return conn.execute(f"""select blocked_by, metadata, updated_at::text
                                  from {schema}.tasks where id = %s""", (tid,)).fetchone()

    ids, meta, touched = row(mixed)
    assert [str(one) for one in ids] == [first, second, elsewhere]
    assert meta == {"blocked_by": ["f-there", "f-gone"], "cost_total": 1}
    assert touched.startswith("2026-01-01")
    ids, meta, _ = row(whole)
    assert [str(one) for one in ids] == [second] and meta == {"cost_total": 1}
    ids, meta, _ = row(junk)
    assert ids == [] and meta == {"blocked_by": "not a list", "cost_total": 1}
    ids, meta, _ = row(keyed_there)
    assert [str(one) for one in ids] == [elsewhere] and meta == {"cost_total": 1}

    # Again: nothing moves, the same names are reported.
    mod.cmd_migrate(entry, ["--apply"])
    again = answer(capsys)["backfilled"]
    assert (again["moved"], again["tasks_written"], again["unresolvable"]) == (0, 0, 3)
    assert [str(one) for one in row(mixed)[0]] == [first, second, elsewhere]

    # doctor names what is left for this project.
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        left = mod._blocked_by_backfill(cur, schema, apply=False, project=HERE)
    assert (left["tasks"], left["moved"], left["unresolvable"]) == (3, 0, 3)


# --- Compatibility for one release -------------------------------------------

@needs_store
def test_meta_set_writes_the_field_and_says_it_is_deprecated(store, capsys):
    entry, schema, conn = store
    first = add(entry, capsys, "c-first")
    add(entry, capsys, "c-task")
    mod.cmd_meta(entry, ["set", "c-task", "blocked_by", '["c-first"]', "note", "kept"])
    out = capsys.readouterr()
    said = json.loads(out.out)
    assert said["blocked_by"] == [first]
    assert said["metadata"] == {"note": "kept", "blocked_by": [first]}
    assert "set <task> --blocked-by" in said["deprecated"][0]
    assert json.loads(out.err)["deprecated"] == said["deprecated"]
    stored = conn.execute(f"select blocked_by, metadata from {schema}.tasks "
                          f"where unique_key = 'c-task'").fetchone()
    assert [str(one) for one in stored[0]] == [first] and stored[1] == {"note": "kept"}
    # The same refusals as `set --blocked-by`.
    error = refused(capsys, mod.cmd_meta, entry, ["set", "c-task", "blocked_by",
                                                  '["c-nowhere"]'])
    assert "'c-nowhere' names no task" in error["message"]
    error = refused(capsys, mod.cmd_meta, entry, ["set", "c-task", "blocked_by",
                                                  '"c-first"'], 6)
    assert "takes a list of tasks" in error["message"]
    # `meta rm` empties it.
    mod.cmd_meta(entry, ["rm", "c-task", "blocked_by"])
    removed = json.loads(capsys.readouterr().out)
    assert removed["blocked_by"] == [] and removed["deprecated"]
    assert shown(entry, capsys, "c-task")["task"]["blocked_by"] == []


@needs_store
def test_the_metadata_mirrors_the_field_for_one_release(store, capsys):
    entry, schema, conn = store
    first = add(entry, capsys, "c-first")
    add(entry, capsys, "c-task")
    set_(entry, capsys, "c-task", "--blocked-by", "c-first")
    task = shown(entry, capsys, "c-task")["task"]
    assert task["blocked_by"] == [first] and task["metadata"] == {"blocked_by": [first]}
    mod.cmd_meta(entry, ["show", "c-task"])
    assert answer(capsys)["metadata"] == {"blocked_by": [first]}
    mod.cmd_list(entry, ["--full"])
    rows = {row["unique_key"]: row for row in answer(capsys)["tasks"]}
    assert rows["c-task"]["metadata"] == {"blocked_by": [first]}
    assert rows["c-task"]["blocked_by_status"] == {first: "todo"}
    assert rows["c-first"]["metadata"] == {}
    # Names a backfill could not move are shown as stored, not hidden.
    conn.execute(f"""update {schema}.tasks set metadata = '{{"blocked_by": ["c-gone"]}}'
                      where unique_key = 'c-task'""")
    assert shown(entry, capsys, "c-task")["task"]["metadata"] == {"blocked_by": ["c-gone"]}


@needs_store
def test_a_worker_writes_blockers_under_the_metadata_scope(store, capsys, project,
                                                           monkeypatch):
    """A worker file that lets a turn write `blocked_by` on another task names it
    under `metadata`, as it did when blockers were a metadata key."""
    entry, schema, conn = store
    add(entry, capsys, "w-held")
    add(entry, capsys, "w-other")
    add(entry, capsys, "w-gate")
    add(entry, capsys, "w-gate-2")
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\n"
                       "writes: {held: {status: [todo]}, other: {metadata: [blocked_by]}}")
    mod.cmd_claim(entry, ["--worker", "alpha", "--key", "w-held"])
    execution = answer(capsys)["execution"]["id"]
    conn.execute(f"""update {schema}.task_executions
                        set metrics = jsonb_build_object('worker_writes', %s::jsonb)
                      where id = %s""",
                 (json.dumps({"held": {"status": ["todo"]},
                              "other": {"metadata": ["blocked_by"]}}), execution))
    monkeypatch.setenv("TASKS_EXECUTION", execution)
    assert set_(entry, capsys, "w-other", "--blocked-by", "w-gate")["task"]["blocked_by"]
    assert set_(entry, capsys, "w-other", "--blocked-by", "w-gate-2")["task"]["blocked_by"]
    assert set_(entry, capsys, "w-other", "--clear", "blocked-by")["task"]["blocked_by"] == []
    # Without it, a worker may add blockers to a task that has none, and no more.
    conn.execute(f"""update {schema}.task_executions set metrics = '{{}}'::jsonb
                      where id = %s""", (execution,))
    assert set_(entry, capsys, "w-other", "--blocked-by", "w-gate")["task"]["blocked_by"]
    error = refused(capsys, mod.cmd_set, entry, ["w-other", "--blocked-by", "w-gate-2"])
    assert "write over a metadata key" in error["message"]
    error = refused(capsys, mod.cmd_set, entry, ["w-other", "--clear", "blocked-by"])
    assert "remove metadata" in error["message"]


# --- Through the CLI ---------------------------------------------------------

import test_service as service  # noqa: E402
from test_service import lab  # noqa: E402,F401  (fixture)


@needs_store
def test_through_the_cli_doctor_names_what_migrate_moves(lab):
    import psycopg

    cli = service.tasks_cli
    schema = json.loads((lab["project"] / "capabilities" / "tasks" / "connections.json")
                        .read_text())["connections"]["local"]["db_schema"]
    gate = service.answer_of(cli(lab, "add", "--type", "alpha", "--title", "gate",
                                 "--key", "l-gate", "--status", "todo"))["created"]
    service.answer_of(cli(lab, "add", "--type", "alpha", "--title", "old",
                          "--key", "l-old", "--status", "todo"))
    with psycopg.connect(hooks.DSN, autocommit=True) as conn:
        conn.execute(f"""update {schema}.tasks
                            set metadata = '{{"blocked_by": ["l-gate", "l-gone"]}}'
                          where unique_key = 'l-old'""")
    doctor = service.answer_of(cli(lab, "doctor"))
    assert doctor["blocked_by_in_metadata"]["moved"] == 1
    assert doctor["blocked_by_in_metadata"]["unresolvable"] == 1
    assert "still keep blocked_by in their metadata" in doctor["warning"]
    applied = service.answer_of(cli(lab, "migrate", "--apply"))
    assert applied["backfilled"]["moved"] == 1
    shown_ = service.answer_of(cli(lab, "show", "l-old"))["task"]
    assert shown_["blocked_by"] == [gate]
    assert shown_["metadata"] == {"blocked_by": ["l-gone"]}
    # The task is now held by its gate, through the CLI's own claim.
    ran = service.answer_of(cli(lab, "run", "alpha"))
    assert ran["would_claim"] == "l-gate"
    # The deprecated write says so on stderr and still lands in the field.
    proc = cli(lab, "meta", "set", "l-old", "blocked_by", '["l-gate"]')
    assert proc.returncode == 0 and '"deprecated"' in proc.stderr
    doctor = service.answer_of(cli(lab, "doctor"))
    assert "blocked_by_in_metadata" not in doctor


def test_a_frame_that_lets_a_turn_write_blockers_says_how(project):
    hooks.write_worker(project, "probe", "takes: [alpha]\nprofile: plain\n"
                       "writes: {held: {status: [todo]}, other: {metadata: [blocked_by]}}")
    prompt = mod._prompt(mod._worker("probe"), {"id": "id-1", "metadata": {}}, [], 1, None)
    assert "`tasks set <task> --blocked-by <task>[,<task>...]`" in prompt
    hooks.write_worker(project, "probe", "takes: [alpha]\nprofile: plain\n"
                       "writes: {held: {status: [todo]}}")
    prompt = mod._prompt(mod._worker("probe"), {"id": "id-1", "metadata": {}}, [], 1, None)
    assert "--blocked-by" not in prompt
