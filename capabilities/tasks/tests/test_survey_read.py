#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""Reading a store in one page: the order a scan answers in, how a search pages,
how many tasks hold each status, and the inputs that reach no answer.

The parsing half is checked without a store. The store-backed half reads
TASKS_TEST_DSN and skips when it is unset; every run works in a schema of its
own and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

HERE, THERE = "prj_here", "prj_there"


def _error(capsys) -> dict:
    return json.loads(capsys.readouterr().err)["error"]


def _refused(capsys, call, *args) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    captured = capsys.readouterr()
    error = json.loads(captured.err)["error"]
    error["exit"] = exit_info.value.code
    error["_stderr"] = captured.err
    return error


@pytest.fixture
def no_store(monkeypatch):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    monkeypatch.setattr(mod, "_connect", lambda entry: pytest.fail(
        "reached the store with an input that has no answer"))
    return {"timezone": "UTC"}


# --- Page size ---------------------------------------------------------------

SIZED = {"list": (mod.cmd_list, []), "search": (mod.cmd_search, ["q"]),
         "ready": (mod.cmd_ready, [])}


@pytest.mark.parametrize("verb", sorted(SIZED))
@pytest.mark.parametrize("flag", ("--per-page", "--limit"))
@pytest.mark.parametrize("value", ("0", "-1", "-50", "many"))
def test_a_page_size_below_one_is_refused_in_one_line(verb, flag, value, no_store,
                                                      capsys):
    handler, args = SIZED[verb]
    error = _refused(capsys, handler, no_store, [*args, flag, value])
    assert error["exit"] == 6 and error["code"] == "input"
    assert "1 or more" in error["message"] and flag in error["message"]
    assert "Traceback" not in error["_stderr"]
    assert len(error["_stderr"].strip().splitlines()) == 1


def test_a_page_size_of_one_or_more_is_taken():
    assert mod._per_page({"per-page": "1"}) == 1
    assert mod._per_page({"limit": "500"}) == 500
    assert mod._per_page({}) is None


# --- Order -------------------------------------------------------------------

@pytest.mark.parametrize("verb", ("list", "search"))
def test_an_unknown_sort_is_refused_naming_the_accepted_ones(verb, no_store, capsys):
    handler, args = SIZED[verb]
    error = _refused(capsys, handler, no_store, [*args, "--sort", "pickup-desc"])
    assert error["exit"] == 6 and error["code"] == "input"
    assert "pickup-desc" in error["message"] and "updated" in error["hint"]


def test_the_queue_takes_no_sort(no_store, capsys):
    error = _refused(capsys, mod.cmd_ready, no_store, ["--sort", "updated"])
    assert error["exit"] == 6 and error["message"] == "unknown flag --sort"


def test_unsaid_each_scan_keeps_its_own_order():
    assert mod._order({}, "pickup_at asc nulls last, created_at asc") == \
        "pickup_at asc nulls last, created_at asc"
    assert mod._order({"sort": "updated"}, "created_at asc").startswith("updated_at desc")


def test_every_sort_breaks_ties_by_id():
    for value, order in mod._SORTS.items():
        assert order.endswith(", id desc"), value
    assert set(mod._SORTS) == {"touched", "updated", "created", "pickup"}
    assert mod._SORTS["pickup"].startswith("pickup_at asc nulls last")


# --- Counts ------------------------------------------------------------------

@pytest.mark.parametrize("flag", ("--per-page", "--page", "--sort", "--activities",
                                  "--full"))
def test_counts_takes_the_filters_and_nothing_that_shapes_rows(flag, no_store, capsys):
    args = [flag] if flag in ("--full", "--activities") else [flag, "2"]
    error = _refused(capsys, mod.cmd_counts, no_store, args)
    assert error["exit"] == 6 and error["message"] == f"unknown flag {flag}"


def test_counts_refuses_all_projects_beside_a_project(no_store, capsys):
    error = _refused(capsys, mod.cmd_counts, no_store,
                     ["--all-projects", "--project", THERE])
    assert error["exit"] == 6
    assert "--all-projects" in error["message"] and "--project" in error["message"]


# --- End of options ----------------------------------------------------------

def test_a_lone_double_dash_ends_the_flags():
    opts = mod._flags(["--", "--literal"], ("type",), takes=1)
    assert opts["_loose"] == ["--literal"]
    opts = mod._flags(["--type", "x", "--", "--type", "y"], ("type",), takes=2)
    assert opts["type"] == "x" and opts["_loose"] == ["--type", "y"]
    # Past the end of the flags, a surplus is still refused rather than dropped.
    with pytest.raises(SystemExit):
        mod._flags(["--", "a", "b"], (), takes=1)


def test_the_connection_flag_is_not_looked_for_past_the_end_of_the_flags(
        monkeypatch):
    seen = {}
    monkeypatch.setattr(mod, "_gate", lambda: None)
    monkeypatch.setattr(mod, "_contract", lambda argv: None)
    monkeypatch.setattr(mod, "_connections_registry", lambda: (None, None))
    def select(reg, wanted):
        seen["wanted"] = wanted
        return "c", {}

    monkeypatch.setattr(mod, "_select_connection", select)
    monkeypatch.setattr(mod, "_schema", lambda entry: "tasks")
    monkeypatch.setattr(mod, "_declared_project", lambda: HERE)
    monkeypatch.setattr(mod, "cmd_search",
                        lambda entry, rest: seen.setdefault("rest", rest))
    monkeypatch.setattr(sys, "argv", ["tasks", "search", "--", "--connection", "x"])
    mod.main()
    assert seen["wanted"] is None
    assert seen["rest"] == ["--", "--connection", "x"]


def test_the_help_documents_every_new_surface_beside_its_neighbours():
    doc = mod.__doc__
    reading = doc.split("READING")[1].split("WRITING")[0]
    assert "tasks counts [filters]" in reading and "`--sort`" in reading
    assert "FILTERS  (list, ready, search, counts)" in doc
    order = doc.split("ORDER  (list, search)")[1].split("PAGING")[0]
    for value in ("touched", "updated", "created", "pickup"):
        assert f"--sort {value}" in order
    assert "no pickup last" in order and "before the page is cut" in order
    collections = doc.split("\nCOLLECTIONS\n")[1].split("\nACTIVITY\n")[0]
    assert "`last_touched_at`" in collections and "never null" in collections
    assert "`last_activity_at` is the newest trail entry" in collections
    paging = doc.split("PAGING  (list, ready, search)")[1].split("COLLECTIONS")[0]
    assert "1 or more" in paging and "every match" in paging
    assert "tasks search -- --literal" in doc.split("ARGUMENTS")[1]


# --- Store -------------------------------------------------------------------

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
             "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True,
             "timezone": "UTC"}
    monkeypatch.setenv("TASKS_TEST_PASSWORD", info.get("password") or "")
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.delenv("TASKS_ACTOR", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _add(monkeypatch, entry, capsys, project: str, key: str, **fields) -> None:
    monkeypatch.setattr(mod, "PROJECT", project)
    args = ["--type", "probe", "--title", f"title {key}", "--key", key]
    for flag, value in fields.items():
        args += [f"--{flag}", value]
    mod.cmd_add(entry, args)
    capsys.readouterr()
    monkeypatch.setattr(mod, "PROJECT", HERE)


@pytest.fixture
def seeded(store, monkeypatch, capsys):
    """Five tasks here and two there. Each is created a day apart and then moved
    in the reverse of that order, so the order a task was created in, the order
    it was moved in and the order it is picked up in are three different
    orders, and a sort that falls back to any of the others is caught."""
    entry, schema, conn = store
    statuses = ["todo", "todo", "draft", "waiting", "complete"]
    for n, status in enumerate(statuses):
        extra = {"assignee": "someone"} if status == "waiting" else {}
        _add(monkeypatch, entry, capsys, HERE, f"h-{n}", status=status,
             pickup=f"2026-01-{10 - n:02d}", **extra)
    _add(monkeypatch, entry, capsys, THERE, "t-0", status="todo")
    _add(monkeypatch, entry, capsys, THERE, "t-1", status="closed")
    keys = [f"h-{n}" for n in range(5)] + ["t-0", "t-1"]
    # The store stamps every update with the moment it ran; the moments here are
    # chosen, so that stamp is held off while they are written.
    conn.execute(f"alter table {schema}.tasks disable trigger tasks_touch_updated_at")
    for n, key in enumerate(keys):
        conn.execute(f"update {schema}.tasks set created_at = %s::timestamptz, "
                     f"updated_at = %s::timestamptz where unique_key = %s",
                     (f"2026-02-{n + 1:02d}T00:00Z", f"2026-03-{20 - n:02d}T00:00Z",
                      key))
    conn.execute(f"alter table {schema}.tasks enable trigger tasks_touch_updated_at")
    return entry, schema, conn


def _keys(answer: dict) -> list[str]:
    return [t["unique_key"] for t in answer["tasks"]]


@needs_store
def test_sort_updated_orders_the_whole_set_before_the_page_is_cut(seeded, capsys):
    entry, _schema, _conn = seeded
    pages = []
    for page in ("1", "2", "3"):
        mod.cmd_list(entry, ["--sort", "updated", "--per-page", "2", "--page", page])
        pages.append(_answer(capsys))
    assert [_keys(p) for p in pages] == [["h-0", "h-1"], ["h-2", "h-3"], ["h-4"]]
    assert pages[0]["pagination"]["total"] == 5
    # Across every project, the newest moved of them all come first.
    mod.cmd_list(entry, ["--all-projects", "--sort", "updated", "--per-page", "3"])
    assert _keys(_answer(capsys)) == ["h-0", "h-1", "h-2"]
    mod.cmd_search(entry, ["title", "--sort", "updated", "--per-page", "2",
                           "--page", "2"])
    assert _keys(_answer(capsys)) == ["h-2", "h-3"]


@needs_store
def test_unsaid_the_order_is_what_it_was(seeded, capsys):
    entry, _schema, _conn = seeded
    # list: soonest pickup first - here the reverse of creation.
    mod.cmd_list(entry, [])
    assert _keys(_answer(capsys)) == ["h-4", "h-3", "h-2", "h-1", "h-0"]
    # search: earliest created first.
    mod.cmd_search(entry, ["title"])
    assert _keys(_answer(capsys)) == ["h-0", "h-1", "h-2", "h-3", "h-4"]


@needs_store
def test_search_pages_the_way_list_does(seeded, capsys):
    entry, _schema, _conn = seeded
    # Unasked, one page holds every match, and says so.
    mod.cmd_search(entry, ["title"])
    whole = _answer(capsys)
    assert len(whole["tasks"]) == 5
    assert whole["pagination"] == {"current_page": 1, "last_page": 1, "per_page": 5,
                                   "total": 5, "from": 1, "to": 5}
    # Asked, it pages, with the keys `list` carries and their meaning.
    mod.cmd_search(entry, ["title", "--per-page", "2", "--page", "3"])
    third = _answer(capsys)
    mod.cmd_list(entry, ["--per-page", "2", "--page", "3"])
    listed = _answer(capsys)
    assert set(third["pagination"]) == set(listed["pagination"])
    assert third["pagination"] == {"current_page": 3, "last_page": 3, "per_page": 2,
                                   "total": 5, "from": 5, "to": 5}
    assert _keys(third) == ["h-4"]
    mod.cmd_search(entry, ["title", "--limit", "2", "--page", "2"])
    assert _keys(_answer(capsys)) == ["h-2", "h-3"]
    # Nothing matched is a whole, empty answer rather than a missing envelope.
    mod.cmd_search(entry, ["nothing like this"])
    empty = _answer(capsys)
    assert empty["tasks"] == [] and empty["pagination"]["total"] == 0


@needs_store
def test_counts_match_list_under_a_project_and_across_the_store(seeded, capsys):
    entry, _schema, _conn = seeded
    for scope in ([], ["--project", THERE], ["--all-projects"],
                  ["--all-projects", "--type", "probe"]):
        mod.cmd_counts(entry, scope)
        counted = _answer(capsys)
        assert "tasks" not in counted
        assert list(counted["counts"]) == list(mod.STATUSES)
        for status in mod.STATUSES:
            mod.cmd_list(entry, [*scope, "--status", status, "--per-page", "1"])
            assert counted["counts"][status] == \
                _answer(capsys)["pagination"]["total"], (scope, status)
        mod.cmd_list(entry, [*scope, "--per-page", "1"])
        assert counted["total"] == _answer(capsys)["pagination"]["total"]
    mod.cmd_counts(entry, [])
    assert _answer(capsys) == {"counts": {"draft": 1, "todo": 2, "in_progress": 0,
                                          "waiting": 1, "complete": 1, "closed": 0},
                               "total": 5}
    mod.cmd_counts(entry, ["--all-projects"])
    assert _answer(capsys)["counts"]["closed"] == 1


@pytest.fixture
def statements(monkeypatch):
    """Every statement the store is handed, written down in order."""
    real = mod._connect
    asked = []

    class Counted:
        """The real cursor, with every statement it is handed written down."""

        def __init__(self, cur):
            self.cur = cur

        def execute(self, sql, *args, **kwargs):
            asked.append(sql)
            return self.cur.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.cur, name)

        def __enter__(self):
            self.cur.__enter__()
            return self

        def __exit__(self, *exc):
            return self.cur.__exit__(*exc)

    class Watched:
        def __init__(self, conn):
            self.conn = conn

        def cursor(self, *args, **kwargs):
            return Counted(self.conn.cursor(*args, **kwargs))

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

    monkeypatch.setattr(mod, "_connect", lambda e: Watched(real(e)))
    return asked


@needs_store
def test_counts_is_one_question_of_the_store(seeded, capsys, statements):
    entry, _schema, _conn = seeded
    asked = statements
    mod.cmd_counts(entry, ["--all-projects"])
    assert _answer(capsys)["total"] == 7
    assert len(asked) == 1


@needs_store
def test_a_search_for_text_that_begins_with_dashes(store, capsys, monkeypatch):
    entry, _schema, _conn = store
    _add(monkeypatch, entry, capsys, HERE, "d-1")
    mod.cmd_set(entry, ["d-1", "--title", "run it with --literal on"])
    capsys.readouterr()
    mod.cmd_search(entry, ["--", "--literal"])
    answer = _answer(capsys)
    assert answer["query"] == "--literal" and _keys(answer) == ["d-1"]
    # Flags before the end of the flags still apply.
    mod.cmd_search(entry, ["--status", "todo", "--", "--literal"])
    assert _answer(capsys)["tasks"] == []
    # And without the marker it is a flag, refused as one.
    error = _refused(capsys, mod.cmd_search, entry, ["--literal"])
    assert error["exit"] == 6 and error["message"] == "unknown flag --literal"


# --- Last touched ------------------------------------------------------------

def _row(entry, capsys, key: str, *extra: str) -> dict:
    mod.cmd_list(entry, ["--all-projects", "--per-page", "100", *extra])
    return next(t for t in _answer(capsys)["tasks"] if t["unique_key"] == key)


def _at(value) -> "datetime.datetime":
    """A moment as written in an answer or in a test, compared as a moment: the
    zone an answer is rendered in is not what is being proven."""
    import datetime
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


@needs_store
def test_last_touched_is_the_latest_of_its_three_sources(store, capsys, monkeypatch):
    entry, schema, conn = store
    _add(monkeypatch, entry, capsys, HERE, "k-1")
    tid = conn.execute(f"select id from {schema}.tasks where unique_key = 'k-1'"
                       ).fetchone()[0]
    conn.execute(f"alter table {schema}.tasks disable trigger tasks_touch_updated_at")
    conn.execute(f"update {schema}.tasks set updated_at = '2026-01-01T00:00Z' "
                 f"where id = %s", (tid,))

    # A field moving, and nothing else yet.
    row = _row(entry, capsys, "k-1")
    assert _at(row["last_touched_at"]) == _at("2026-01-01T00:00Z")
    assert row["last_activity_at"] is None and row["activities_count"] == 0

    # The trail growing.
    conn.execute(f"insert into {schema}.task_activities (task_id, description, "
                 f"created_at) values (%s, 'did a thing', '2026-01-02T00:00Z')", (tid,))
    row = _row(entry, capsys, "k-1")
    assert _at(row["last_touched_at"]) == _at("2026-01-02T00:00Z")
    assert _at(row["last_activity_at"]) == _at("2026-01-02T00:00Z")
    assert row["activities_count"] == 1

    # A raise starting, then ending.
    eid = conn.execute(f"insert into {schema}.task_executions (task_id, attempt, "
                       f"started_at) values (%s, 1, '2026-01-03T00:00Z') returning id",
                       (tid,)).fetchone()[0]
    assert _at(_row(entry, capsys, "k-1")["last_touched_at"]) == \
        _at("2026-01-03T00:00Z")
    conn.execute(f"update {schema}.task_executions set status = 'ok', "
                 f"ended_at = '2026-01-04T00:00Z' where id = %s", (eid,))
    row = _row(entry, capsys, "k-1", "--full")
    assert _at(row["last_touched_at"]) == _at("2026-01-04T00:00Z")
    # What the trail says is still only the trail.
    assert _at(row["last_activity_at"]) == _at("2026-01-02T00:00Z")
    assert row["activities_count"] == 1

    # And a field moving after all of it wins again.
    conn.execute(f"update {schema}.tasks set updated_at = '2026-01-05T00:00Z' "
                 f"where id = %s", (tid,))
    mod.cmd_search(entry, ["k-1"])
    assert _at(_answer(capsys)["tasks"][0]["last_touched_at"]) == \
        _at("2026-01-05T00:00Z")
    conn.execute(f"alter table {schema}.tasks enable trigger tasks_touch_updated_at")


@pytest.fixture
def four_orders(seeded):
    """The seeded store, arranged so the four sorts give four different orders:
    a trail entry and a raise lift two tasks above their last move, and the
    pickups are neither the creation order nor its reverse, with two tasks here
    and both there holding none."""
    entry, schema, conn = seeded
    pickups = {"h-0": "2026-01-03", "h-1": None, "h-2": "2026-01-01",
               "h-3": "2026-01-02", "h-4": None}
    conn.execute(f"alter table {schema}.tasks disable trigger tasks_touch_updated_at")
    for key, pickup in pickups.items():
        conn.execute(f"update {schema}.tasks set pickup_at = %s::timestamptz "
                     f"where unique_key = %s", (pickup, key))
    conn.execute(f"update {schema}.tasks set pickup_at = null where project_id = %s",
                 (THERE,))
    conn.execute(f"alter table {schema}.tasks enable trigger tasks_touch_updated_at")
    ids = {r[0]: str(r[1]) for r in conn.execute(
        f"select unique_key, id from {schema}.tasks")}
    conn.execute(f"insert into {schema}.task_activities (task_id, description, "
                 f"created_at) values (%s, 'late word', '2026-03-25T00:00Z')",
                 (ids["h-3"],))
    conn.execute(f"insert into {schema}.task_executions (task_id, attempt, "
                 f"started_at) values (%s, 1, '2026-03-24T00:00Z')", (ids["h-1"],))
    return entry, ids


def _no_pickup_last(ids: dict, keys: list[str]) -> list[str]:
    return sorted(keys, key=lambda k: ids[k], reverse=True)


def _walk(call, entry, capsys, args: list[str], per: int) -> list[str]:
    """Every page of a scan in turn, so what is proven is the order of the whole
    set and not of one page."""
    keys, page = [], 1
    while True:
        call(entry, [*args, "--per-page", str(per), "--page", str(page)])
        answer = _answer(capsys)
        keys += _keys(answer)
        if page >= answer["pagination"]["last_page"]:
            return keys
        page += 1


@needs_store
@pytest.mark.parametrize("scope", ("project", "all"))
@pytest.mark.parametrize("verb", ("list", "search"))
def test_each_sort_orders_the_whole_set_across_pages(four_orders, capsys, scope, verb):
    entry, ids = four_orders
    here = ["h-0", "h-1", "h-2", "h-3", "h-4"]
    there = ["t-0", "t-1"]
    expected = {
        "touched": ["h-3", "h-1", "h-0", "h-2", "h-4"] + (there if scope == "all" else []),
        "updated": here + (there if scope == "all" else []),
        "created": (["t-1", "t-0"] if scope == "all" else []) + here[::-1],
        "pickup": ["h-2", "h-3", "h-0"] + _no_pickup_last(
            ids, ["h-1", "h-4"] + (there if scope == "all" else [])),
    }
    call = {"list": mod.cmd_list, "search": mod.cmd_search}[verb]
    lead = ["title"] if verb == "search" else []
    narrow = ["--all-projects"] if scope == "all" else ["--project", HERE]
    for value, keys in expected.items():
        assert _walk(call, entry, capsys, [*lead, *narrow, "--sort", value], 2) == keys, value


@needs_store
def test_a_page_costs_the_same_questions_however_large(four_orders, capsys,
                                                      statements):
    entry, _ids = four_orders
    counts = []
    for per in ("1", "7"):
        for call, lead in ((mod.cmd_list, []), (mod.cmd_search, ["title"])):
            statements.clear()
            call(entry, [*lead, "--all-projects", "--sort", "touched", "--per-page", per])
            capsys.readouterr()
            counts.append((call.__name__, len(statements)))
    assert counts[:2] == counts[2:]


@needs_store
def test_unsaid_the_order_and_the_trail_fields_are_what_they_were(four_orders, capsys):
    entry, _ids = four_orders
    mod.cmd_list(entry, ["--project", HERE])
    listed = _answer(capsys)
    # Soonest pickup, none last, then creation order: the default it always had.
    assert _keys(listed) == ["h-2", "h-3", "h-0", "h-1", "h-4"]
    mod.cmd_search(entry, ["title"])
    assert _keys(_answer(capsys)) == ["h-0", "h-1", "h-2", "h-3", "h-4"]
    rows = {t["unique_key"]: t for t in listed["tasks"]}
    assert rows["h-3"]["activities_count"] == 1
    assert _at(rows["h-3"]["last_activity_at"]) == _at("2026-03-25T00:00Z")
    assert _at(rows["h-3"]["last_touched_at"]) == _at("2026-03-25T00:00Z")
    assert rows["h-1"]["last_activity_at"] is None
    assert rows["h-1"]["activities_count"] == 0
    assert all(t["last_touched_at"] for t in listed["tasks"])


# --- Direction ---------------------------------------------------------------

@pytest.mark.parametrize("verb", ("list", "search"))
def test_order_without_a_sort_is_refused_naming_what_is_accepted(verb, no_store,
                                                                 capsys):
    handler, args = SIZED[verb]
    error = _refused(capsys, handler, no_store, [*args, "--order", "asc"])
    assert error["exit"] == 6 and error["code"] == "input"
    assert "--sort" in error["message"] and "asc|desc" in error["message"]
    assert len(error["_stderr"].strip().splitlines()) == 1


@pytest.mark.parametrize("verb", ("list", "search"))
@pytest.mark.parametrize("value", ("up", "ASC", "", "descending"))
def test_an_unknown_order_is_refused_naming_what_is_accepted(verb, value, no_store,
                                                             capsys):
    handler, args = SIZED[verb]
    error = _refused(capsys, handler, no_store,
                     [*args, "--sort", "created", "--order", value])
    assert error["exit"] == 6 and error["code"] == "input"
    assert "asc, desc" in error["message"]
    assert len(error["_stderr"].strip().splitlines()) == 1


@pytest.mark.parametrize("call", (mod.cmd_ready, mod.cmd_counts))
def test_only_the_two_scans_take_an_order(call, no_store, capsys):
    error = _refused(capsys, call, no_store, ["--order", "asc"])
    assert error["exit"] == 6 and error["message"] == "unknown flag --order"


def test_without_an_order_every_sort_runs_as_it_did():
    for value, order in mod._SORTS.items():
        assert mod._order({"sort": value}, "unsaid") == order
    assert mod._order({}, "unsaid") == "unsaid"


def test_an_order_turns_the_key_and_its_tie_break_together():
    assert mod._order({"sort": "touched", "order": "asc"}, "") == \
        "last_touched_at asc, id asc"
    assert mod._order({"sort": "created", "order": "desc"}, "") == \
        "created_at desc, id desc"
    for direction in ("asc", "desc"):
        assert mod._order({"sort": "pickup", "order": direction}, "") == \
            f"pickup_at {direction} nulls last, id {direction}"


@needs_store
@pytest.mark.parametrize("scope", ("project", "all"))
@pytest.mark.parametrize("verb", ("list", "search"))
def test_both_directions_of_every_sort_across_pages(four_orders, capsys, scope, verb):
    entry, ids = four_orders
    here = ["h-0", "h-1", "h-2", "h-3", "h-4"]
    there = ["t-0", "t-1"] if scope == "all" else []
    nulls = ["h-1", "h-4"] + there
    newest_first = {
        "touched": ["h-3", "h-1", "h-0", "h-2", "h-4"] + there,
        "updated": here + there,
        "created": there[::-1] + here[::-1],
    }
    expected = {}
    for value, keys in newest_first.items():
        expected[(value, "desc")] = keys
        expected[(value, "asc")] = keys[::-1]
    expected[("pickup", "asc")] = ["h-2", "h-3", "h-0"] + sorted(nulls, key=ids.get)
    expected[("pickup", "desc")] = ["h-0", "h-3", "h-2"] + sorted(
        nulls, key=ids.get, reverse=True)
    call = {"list": mod.cmd_list, "search": mod.cmd_search}[verb]
    lead = ["title"] if verb == "search" else []
    narrow = ["--all-projects"] if scope == "all" else ["--project", HERE]
    for (value, direction), keys in expected.items():
        walked = _walk(call, entry, capsys,
                       [*lead, *narrow, "--sort", value, "--order", direction], 2)
        assert walked == keys, (value, direction)


@pytest.fixture
def ties(four_orders):
    """The four orders, with a tie on every key: two tasks created at one
    moment, moved at one moment, touched at one moment, and due at one moment."""
    entry, ids = four_orders
    import psycopg
    schema = mod.SCHEMA
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"alter table {schema}.tasks disable trigger tasks_touch_updated_at")
        conn.execute(f"update {schema}.tasks set created_at = (select created_at from "
                     f"{schema}.tasks where unique_key = 'h-2') where unique_key = 'h-1'")
        conn.execute(f"update {schema}.tasks set updated_at = (select updated_at from "
                     f"{schema}.tasks where unique_key = 'h-3') where unique_key = 'h-4'")
        conn.execute(f"update {schema}.tasks set updated_at = '2026-03-24T00:00Z' "
                     f"where unique_key = 'h-2'")
        conn.execute(f"update {schema}.tasks set pickup_at = (select pickup_at from "
                     f"{schema}.tasks where unique_key = 'h-2') where unique_key = 'h-3'")
        conn.execute(f"alter table {schema}.tasks enable trigger tasks_touch_updated_at")
    return entry, ids


@needs_store
@pytest.mark.parametrize("verb", ("list", "search"))
def test_a_reversed_order_is_the_exact_mirror_ties_included(ties, capsys, verb):
    entry, ids = ties
    call = {"list": mod.cmd_list, "search": mod.cmd_search}[verb]
    lead = ["title"] if verb == "search" else []
    tied = {"created": ("h-1", "h-2"), "updated": ("h-3", "h-4"),
            "touched": ("h-1", "h-2"), "pickup": ("h-2", "h-3")}
    for value, pair in tied.items():
        up = _walk(call, entry, capsys, [*lead, "--all-projects", "--sort", value,
                                         "--order", "asc"], 3)
        down = _walk(call, entry, capsys, [*lead, "--all-projects", "--sort", value,
                                           "--order", "desc"], 3)
        low, high = sorted(pair, key=ids.get)
        assert up.index(low) + 1 == up.index(high), (value, up)
        assert down.index(high) + 1 == down.index(low), (value, down)
        if value == "pickup":
            # No pickup has no place on the axis, so it is last both ways and
            # only the two halves mirror.
            dated = [k for k in up if k not in ("h-1", "h-4", "t-0", "t-1")]
            assert up[:len(dated)] == dated and down[:len(dated)] == dated[::-1]
            assert up[len(dated):] == down[len(dated):][::-1]
        else:
            assert up == down[::-1], value


@needs_store
def test_an_order_costs_no_more_questions(four_orders, capsys, statements):
    entry, _ids = four_orders
    counts = []
    for per in ("1", "7"):
        for call, lead in ((mod.cmd_list, []), (mod.cmd_search, ["title"])):
            statements.clear()
            call(entry, [*lead, "--all-projects", "--sort", "touched", "--order", "asc",
                         "--per-page", per])
            capsys.readouterr()
            counts.append(len(statements))
    statements.clear()
    mod.cmd_list(entry, ["--all-projects", "--sort", "touched", "--per-page", "7"])
    capsys.readouterr()
    assert counts[:2] == counts[2:] and counts[0] == len(statements)


def test_the_help_files_order_beside_sort():
    order = mod.__doc__.split("ORDER  (list, search)")[1].split("PAGING")[0]
    assert "--order asc|desc" in order and "Needs --sort" in order
    assert "no pickup stay last both ways" in order


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
