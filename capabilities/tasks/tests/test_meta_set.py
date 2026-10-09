#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.3.0"]
# ///
"""`meta set` with several keys: one write that lands whole or not at all.

A caller that needs several keys on a task names them all in one call, so a
task never carries some of them with nothing saying the rest are missing. How
the pairs are read and refused is checked with no store, and so is the worker
gate, against faked reads. The store-backed checks then prove the write itself -
merged, whole, judged key by key - and read TASKS_TEST_DSN, skipping when it is
unset; every run works in a schema of its own and drops it. The last of them
runs the CLI as a project would, so the write gate and the read-only switch are
met where the command applies them.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'capabilities-contract==0.3.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

HERE, THERE = "prj_meta_here", "prj_meta_there"
ENTRY = {"timezone": "UTC"}
OVERWRITE = mod._WORKER_HELD_ONLY["meta-overwrite"]


def _pairs(n: int) -> list[str]:
    return [token for i in range(n) for token in (f"k{i}", str(i))]


def _refused(capsys, call, *args) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    error = json.loads(capsys.readouterr().err)["error"]
    error["exit"] = exit_info.value.code
    return error


# --- Reading the pairs -------------------------------------------------------

def test_the_pairs_are_read_after_the_task_in_order():
    assert mod._meta_pairs(["t", "k", "v"]) == [("k", "v")]
    assert mod._meta_pairs(["t", "a", "1", "b", '"two"', "c", "x y"]) == [
        ("a", "1"), ("b", '"two"'), ("c", "x y")]


def test_a_value_that_spells_another_key_is_not_a_repeat():
    assert mod._meta_pairs(["t", "a", "b", "b", "a"]) == [("a", "b"), ("b", "a")]


@pytest.mark.parametrize("rest, dangling", [
    (["t", "k"], "k"),
    (["t", "a", "1", "b"], "b"),
    # A value with a space in it, unquoted: its second word is left over.
    (["t", "a", "1", "note", "two", "words"], "words"),
])
def test_a_key_left_without_a_value_is_refused_by_name(rest, dangling, capsys):
    error = _refused(capsys, mod._meta_pairs, rest)
    assert error["exit"] == 6 and error["code"] == "input"
    assert error["message"] == (f"meta set needs a value for every key, and "
                                f"{dangling!r} has none")
    assert "quote a value that contains spaces" in error["hint"]


@pytest.mark.parametrize("rest", [[], ["t"]])
def test_no_key_at_all_keeps_the_refusal_it_had(rest, capsys):
    assert _refused(capsys, mod._meta_pairs, rest) == {
        "code": "input", "message": "meta set needs a task, a key and a value",
        "hint": "quote a value that contains spaces", "exit": 6}


def test_a_key_named_twice_is_refused_by_name(capsys):
    error = _refused(capsys, mod._meta_pairs, ["t", "a", "1", "b", "2", "a", "3"])
    assert error["exit"] == 6 and error["code"] == "input"
    assert error["message"].startswith("meta set names 'a' more than once")
    # Each repeated key is named once, in the order its repeat arrives.
    error = _refused(capsys, mod._meta_pairs,
                     ["t", "b", "1", "a", "2", "a", "3", "b", "4", "a", "5"])
    assert error["message"].startswith("meta set names 'a', 'b' more than once")


@pytest.mark.parametrize("args", [
    ["set", "t", "k"],
    ["set", "t", "a", "1", "b"],
    ["set", "t", "a", "1", "a", "2"],
])
def test_a_refused_shape_never_reaches_the_store(args, monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    monkeypatch.setattr(mod, "_connect", lambda entry: pytest.fail(
        "a refused call reached the store"))
    assert _refused(capsys, mod.cmd_meta, ENTRY, args)["exit"] == 6


def _most_pairs_admitted(monkeypatch, capsys) -> int:
    """The number of pairs in the largest call `meta set` takes, found by asking
    it: a call that gets as far as the store is one it admitted."""

    class Admitted(Exception):
        pass

    def connect(entry):
        raise Admitted

    admitted = []
    with monkeypatch.context() as patch:
        patch.setattr(mod, "PROJECT", HERE)
        patch.setattr(mod, "_connect", connect)
        for n in range(1, 60):
            try:
                mod.cmd_meta(ENTRY, ["set", "t", *_pairs(n)])
            except Admitted:
                admitted.append(n)
            except SystemExit:
                capsys.readouterr()
    assert admitted == list(range(1, len(admitted) + 1))
    return len(admitted)


def test_no_call_the_parser_admits_outgrows_one_statement(monkeypatch, capsys):
    """jsonb_build_object takes at most 100 arguments, so one statement carries
    50 pairs at most. A call past that has to be refused before the store sees
    it, or the caller would be told the store failed."""
    assert 2 <= _most_pairs_admitted(monkeypatch, capsys) <= 50


def test_the_help_states_the_form():
    said = " ".join(mod.__doc__.split())
    for needle in ("tasks meta set <task> <key> <value> [<key> <value>...]",
                   "every pair lands or none does",
                   "a key the call does not name is left as it was",
                   "A key named twice, or a key left without a value, is refused "
                   "and nothing is written",
                   "Each value is one argument",
                   "quote a value that contains spaces: left unquoted, its words "
                   "are read as further keys and values",
                   "No flag takes a whole tag list or a whole metadata object",
                   "judged key by key, and one key it may not write refuses the "
                   "whole call"):
        assert needle in said, needle
    for stale in ("set exactly one key", "one element at a time"):
        assert stale not in said, stale


# --- The worker gate, with the reads faked and no store ----------------------

HELD, OTHER = "task-held", "task-other"
OPEN = {"id": "exec-1", "task_id": HELD, "status": "running"}


class Reached(Exception):
    """A write got past every gate and addressed the store."""


class FakeCursor:
    """The reads `meta set` makes before it writes - the task by reference, what
    it carries, and the raise by id - answered from memory. Anything else is the
    write, and a write arriving here means every gate let it through."""

    def __init__(self, carried: dict):
        self.carried, self.answer = carried, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = sql.strip()
        if "tasks_executions" in text:
            self.answer = [dict(OPEN)]
        elif text.startswith("select id, project_id"):
            self.answer = [{"id": params[0], "project_id": HERE}]
        elif text.startswith("select * from"):
            self.answer = [{"id": params[0], "metadata": dict(self.carried)}]
        else:
            raise Reached(text, params)

    def fetchall(self):
        return self.answer


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        pass


@pytest.fixture
def worker(monkeypatch):
    """`meta set` run by a worker holding HELD; `carried` is what the addressed
    task already has."""
    monkeypatch.setenv("TASKS_EXECUTION", "exec-1")
    monkeypatch.setattr(mod, "PROJECT", HERE)

    def run(args: list[str], carried: dict):
        monkeypatch.setattr(mod, "_connect", lambda entry: FakeConn(FakeCursor(carried)))
        mod.cmd_meta(ENTRY, ["set", *args])

    return run


def test_one_pair_is_the_statement_it_always_was(worker):
    with pytest.raises(Reached) as reached:
        worker([HELD, "k", "11"], {})
    sql, params = reached.value.args
    assert "set metadata = metadata || jsonb_build_object(%s::text, %s::jsonb)\n" in sql
    assert params == ("k", "11", HELD)


def test_new_keys_on_another_task_go_to_the_store_in_one_statement(worker):
    with pytest.raises(Reached) as reached:
        worker([OTHER, "a", "1", "b", '"two"', "c", "plain"], {"origin": "x"})
    sql, params = reached.value.args
    assert sql.count("jsonb_build_object(") == 1
    assert params == ("a", "1", "b", '"two"', "c", '"plain"', OTHER)


@pytest.mark.parametrize("args", [
    [OTHER, "origin", "2"],
    [OTHER, "new", "1", "origin", "2"],
    [OTHER, "origin", "2", "new", "1"],
])
def test_one_key_already_there_refuses_the_whole_call(args, worker, capsys):
    error = _refused(capsys, worker, args, {"origin": "x"})
    assert error["exit"] == 4 and error["code"] == "policy"
    assert error["message"] == OVERWRITE


def test_the_task_it_holds_takes_every_key(worker):
    with pytest.raises(Reached):
        worker([HELD, "origin", "2", "new", "1"], {"origin": "x"})


# --- Store ---------------------------------------------------------------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


def _entry(allow_write: bool = True) -> dict:
    return {"allow_write": allow_write}


@pytest.fixture
def store(monkeypatch):
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    _cli.bind_store(mod, monkeypatch, schema)
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.delenv("TASKS_ACTOR", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _cli.make_tables(mod, schema)
        try:
            yield _entry(), schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _task(entry, capsys, key: str, **carried) -> str:
    """One task of the project standing here, carrying `carried`, each key written
    by a call of its own."""
    mod.cmd_add(entry, ["--type", "probe", "--title", key, "--key", key,
                        "--status", "todo"])
    tid = _answer(capsys)["created"]
    for name, value in carried.items():
        mod.cmd_meta(entry, ["set", key, name, json.dumps(value)])
        capsys.readouterr()
    return tid


def _metadata(entry, capsys, ref: str) -> dict:
    mod.cmd_meta(entry, ["show", ref])
    return _answer(capsys)["metadata"]


@needs_store
def test_every_pair_lands_in_one_call_and_every_other_key_stays(store, capsys):
    entry, schema, conn = store
    tid = _task(entry, capsys, "m-1", keep=1, b="old")
    mod.cmd_meta(entry, ["set", "m-1", "a", "1", "b", '"two"', "c", '{"x": [1]}',
                         "d", "plain words"])
    merged = {"keep": 1, "b": "two", "a": 1, "c": {"x": [1]}, "d": "plain words"}
    assert _answer(capsys) == {"task": tid, "metadata": merged}
    assert _metadata(entry, capsys, "m-1") == merged


# Every way a value is read: JSON of each kind, and text that is not JSON.
VALUES = ("11", "-3", "1.5", "true", "null", '"text"', "[1, 2]", '{"a": 1}',
          "bare text", "", '"', "{not json", "0x1F")


@needs_store
def test_each_value_is_read_as_it_would_be_alone(store, capsys):
    entry, schema, conn = store
    _task(entry, capsys, "alone")
    _task(entry, capsys, "together")
    for i, raw in enumerate(VALUES):
        mod.cmd_meta(entry, ["set", "alone", f"k{i}", raw])
        capsys.readouterr()
    mod.cmd_meta(entry, ["set", "together",
                         *[token for i, raw in enumerate(VALUES)
                           for token in (f"k{i}", raw)]])
    together = _answer(capsys)["metadata"]
    assert together == _metadata(entry, capsys, "alone")
    assert (together["k0"], together["k5"], together["k8"]) == (11, "text", "bare text")


@needs_store
def test_a_pair_the_store_refuses_takes_the_whole_call_with_it(store, capsys):
    """NaN parses as JSON here and is no JSON to the store, so the store refuses
    that one value. The pairs written beside it in the same call do not land
    either: one statement, so the task is never left holding part of a call."""
    entry, schema, conn = store
    _task(entry, capsys, "whole", keep=1)
    error = _refused(capsys, mod._guarded, mod.cmd_meta, entry,
                     ["set", "whole", "a", "1", "b", "NaN", "c", "3"])
    assert error["exit"] == 5 and error["code"] == "store_error"
    assert _metadata(entry, capsys, "whole") == {"keep": 1}


@needs_store
def test_the_largest_call_the_parser_admits_lands_whole(store, capsys, monkeypatch):
    entry, schema, conn = store
    n = _most_pairs_admitted(monkeypatch, capsys)
    _task(entry, capsys, "wide", keep=1)
    mod.cmd_meta(entry, ["set", "wide", *_pairs(n)])
    assert _answer(capsys)["metadata"] == {"keep": 1, **{f"k{i}": i for i in range(n)}}


@needs_store
def test_a_worker_is_judged_key_by_key_and_a_refusal_writes_nothing(
        store, capsys, monkeypatch):
    entry, schema, conn = store
    _task(entry, capsys, "w-held")
    _task(entry, capsys, "w-other", origin="a report")
    mod.cmd_claim(entry, ["--key", "w-held", "--worker", "a worker"])
    execution = _answer(capsys)["execution"]["id"]
    monkeypatch.setenv("TASKS_EXECUTION", execution)

    # Keys not already there are an addition, however many a call names.
    mod.cmd_meta(entry, ["set", "w-other", "seen", "1", "by", '"a worker"'])
    other = {"origin": "a report", "seen": 1, "by": "a worker"}
    assert _answer(capsys)["metadata"] == other

    # One key already there refuses the call wherever it stands in it, with the
    # refusal a single overwrite gets, and no pair of the call lands.
    alone = _refused(capsys, mod.cmd_meta, entry, ["set", "w-other", "origin", '"mine"'])
    assert alone["exit"] == 4 and alone["code"] == "policy"
    assert alone["message"] == OVERWRITE
    for args in (["set", "w-other", "more", "2", "origin", '"mine"'],
                 ["set", "w-other", "origin", '"mine"', "more", "2"]):
        assert _refused(capsys, mod.cmd_meta, entry, args) == alone
    assert _metadata(entry, capsys, "w-other") == other

    # The task its raise holds takes every key, new and already there alike.
    mod.cmd_meta(entry, ["set", "w-held", "n", "1"])
    capsys.readouterr()
    mod.cmd_meta(entry, ["set", "w-held", "n", "2", "m", "3"])
    assert _answer(capsys)["metadata"] == {"n": 2, "m": 3}

    # Once the raise is over, its own former task is another task too.
    mod.cmd_release(entry, [execution, "--outcome", "ok"])
    capsys.readouterr()
    assert _refused(capsys, mod.cmd_meta, entry,
                    ["set", "w-held", "fresh", "1", "n", "9"]) == alone
    assert _metadata(entry, capsys, "w-held") == {"n": 2, "m": 3}
    mod.cmd_meta(entry, ["set", "w-held", "fresh", "1", "late", "2"])
    assert _answer(capsys)["metadata"] == {"n": 2, "m": 3, "fresh": 1, "late": 2}


@needs_store
def test_every_refusal_a_pair_gets_a_call_of_pairs_gets_and_nothing_lands(
        store, capsys, monkeypatch):
    entry, schema, conn = store
    tid = _task(entry, capsys, "r-1", keep=1)
    monkeypatch.setattr(mod, "PROJECT", THERE)
    theirs = _task(entry, capsys, "r-theirs", keep=1)
    monkeypatch.setattr(mod, "PROJECT", HERE)

    for args, refusal in ((["set", "r-1", "a", "1", "b"], (6, "input")),
                          (["set", "r-1", "a", "1", "a", "2"], (6, "input"))):
        error = _refused(capsys, mod.cmd_meta, entry, args)
        assert (error["exit"], error["code"]) == refusal, args
    for ref, refusal in (("nothing-here", (3, "not_found")), (theirs, (4, "policy"))):
        single = _refused(capsys, mod.cmd_meta, entry, ["set", ref, "a", "1"])
        assert (single["exit"], single["code"]) == refusal, ref
        assert _refused(capsys, mod.cmd_meta, entry,
                        ["set", ref, "a", "1", "b", "2"]) == single
    monkeypatch.setattr(mod, "PROJECT", None)
    single = _refused(capsys, mod.cmd_meta, entry, ["set", tid, "a", "1"])
    assert (single["exit"], single["code"]) == (6, "no_project")
    assert _refused(capsys, mod.cmd_meta, entry, ["set", tid, "a", "1", "b", "2"]) == single
    monkeypatch.setattr(mod, "PROJECT", HERE)

    assert _metadata(entry, capsys, "r-1") == {"keep": 1}
    assert _metadata(entry, capsys, theirs) == {"keep": 1}


# --- Store: the CLI as a project runs it --------------------------------------

@pytest.fixture()
def lab(tmp_path):
    """A project of its own with two connections to one schema: `local` writes and
    `reader` does not."""
    import psycopg
    schema = "tasks_test_" + secrets.token_hex(4)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "tasks").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1",
        "id": "prj_" + uuid.uuid4().hex[:12], "slug": "lab"}))
    (envelope / "tasks" / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {"local": _entry(),
                        "reader": _entry(allow_write=False)}}))
    _cli.write_store_setting(tmp_path / "config", schema)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                   "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                   "CAPABILITIES_STORE_URL", "AGENTKIT_STORE_URL", "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    lab = {"project": project, "env": env, "schema": schema, "tmp": tmp_path}
    assert _tasks(lab, "migrate").returncode == 0
    try:
        yield lab
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def _tasks(lab, *args: str, read_only: bool = False) -> subprocess.CompletedProcess:
    env = dict(lab["env"])
    if read_only:
        env["CAPABILITIES_READ_ONLY"] = "1"
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=env,
                          text=True, capture_output=True, timeout=180)


@needs_store
def test_the_cli_writes_every_pair_and_its_gates_refuse_them_whole(lab):
    made = _tasks(lab, "add", "--type", "probe", "--title", "cli", "--key", "c-1")
    assert made.returncode == 0, made.stdout + made.stderr
    task = json.loads(made.stdout)["created"]
    wrote = _tasks(lab, "meta", "set", "c-1", "a", "1", "b", "two words")
    assert wrote.returncode == 0, wrote.stdout + wrote.stderr
    assert json.loads(wrote.stdout) == {"task": task,
                                        "metadata": {"a": 1, "b": "two words"}}

    reader = _tasks(lab, "--connection", "reader", "meta", "set", "c-1", "a", "2", "c", "3")
    switched = _tasks(lab, "meta", "set", "c-1", "a", "2", "c", "3", read_only=True)
    for refused, gate in ((reader, "read_only"), (switched, "read_only_switch")):
        assert refused.returncode == 4, (gate, refused.stdout, refused.stderr)
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert error["code"] == gate
    shown = _tasks(lab, "meta", "show", "c-1")
    assert json.loads(shown.stdout)["metadata"] == {"a": 1, "b": "two words"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
