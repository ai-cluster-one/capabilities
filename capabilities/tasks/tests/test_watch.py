#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""`watch` holds one connection and prints one JSON line per change a scope
reads, catches up after a lost connection, and ends cleanly.

These run the CLI as a project would, against TASKS_TEST_DSN in a schema of
their own that they drop, and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")
OTHER = "prj_other"


# --- Without a store ---------------------------------------------------------

def test_the_help_documents_the_verb_every_line_and_the_exits():
    doc = mod.__doc__
    reading = doc.split("READING")[1].split("WRITING")[0]
    assert "tasks watch [--project ID | --all-projects]" in reading
    watch = doc.split("\nWATCH\n")[1].split("\nCONTRACT\n")[0]
    for shape in ('"event": "ready"', '"event": K, "task": ROW', '"event": "counts"',
                  '"event": "lost"', '"catch_up": true', '"event": "resync"'):
        assert shape in watch, shape
    for kind in mod._WATCH_KINDS:
        assert f"`{kind}`" in watch, kind
    assert "SIGTERM" in watch and "stdin closes" in watch and "exits 5" in watch
    exits = doc.split("EXIT CODES")[1]
    assert "`watch` stopped" in exits and "For `watch`" in exits


def test_watch_takes_the_project_scope_and_nothing_that_shapes_rows(monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", "prj_here")
    monkeypatch.setattr(mod, "_connect", lambda *a, **k: pytest.fail("reached the store"))
    for args in (["--status", "todo"], ["--full"], ["--activities"], ["extra"]):
        with pytest.raises(SystemExit) as exit_info:
            mod.cmd_watch({}, args)
        assert exit_info.value.code == 6, args
        capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        mod.cmd_watch({}, ["--all-projects", "--project", OTHER])
    assert exit_info.value.code == 6


def test_the_schema_announces_each_kind_and_migrate_names_the_triggers():
    schema = (mod._bundle_dir() / "schema.sql").read_text()
    for kind in mod._WATCH_KINDS:
        assert f"'{kind}'" in schema, kind
    for table, trigger in mod._WATCH_TRIGGERS:
        assert f"create trigger {trigger} after" in schema
        assert f"on tasks.{table}" in schema
    assert f"pg_notify('{mod.WATCH_CHANNEL}'" in schema


# --- Against a store ---------------------------------------------------------

@pytest.fixture()
def lab(tmp_path):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "tasks").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    here = "prj_" + uuid.uuid4().hex[:12]
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": here, "slug": "lab"}))
    (envelope / "tasks" / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {"local": {
            "db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
            "db_user": info.get("user"), "db_name": info.get("dbname"),
            "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
            "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True}}}))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "TASKS_TEST_PASSWORD": info.get("password") or "",
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_STORE_URL", "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    lab = {"project": project, "env": env, "schema": schema, "here": here,
           "watches": []}
    migrated = _tasks(lab, "migrate", "--apply")
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    try:
        yield lab
    finally:
        for watch in lab["watches"]:
            if watch.proc.poll() is None:
                watch.proc.kill()
                watch.proc.wait()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def _tasks(lab, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=lab["env"],
                          text=True, capture_output=True, timeout=180)


def _answer(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


class Watch:
    """A running `tasks watch`, its lines read on a thread so a test can wait
    for one with a deadline."""

    def __init__(self, lab, *args: str):
        self.proc = subprocess.Popen([str(_cli.CLI_PATH), "watch", *args],
                                     cwd=lab["project"], env=lab["env"], text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        lab["watches"].append(self)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            self.lines.put(json.loads(raw))
        self.lines.put(None)

    def next(self, timeout: float = 60) -> dict:
        line = self.lines.get(timeout=timeout)
        assert line is not None, f"the watch ended: {self.proc.stderr.read()}"
        return line

    def until(self, wanted, timeout: float = 60) -> list[dict]:
        """Every line up to and including the first `wanted` accepts."""
        seen = []
        while True:
            line = self.next(timeout)
            seen.append(line)
            if wanted(line):
                return seen

    def quiet(self, seconds: float = 2.0) -> list[dict]:
        seen = []
        try:
            while True:
                line = self.lines.get(timeout=seconds)
                if line is None:
                    return seen
                seen.append(line)
        except queue.Empty:
            return seen


def _ready(lab, *args: str) -> tuple[Watch, dict]:
    watch = Watch(lab, *args)
    return watch, watch.next(timeout=120)


def _changes(lines: list[dict], task: str) -> list[str]:
    return [line["event"] for line in lines
            if line.get("task", {}).get("id") == task]


@needs_store
def test_the_first_line_is_ready_once_listening(lab):
    watch, ready = _ready(lab, "--json")
    assert ready["event"] == "ready"
    assert ready["project"] == lab["here"] and ready["all_projects"] is False
    assert "store" in ready
    made = _answer(_tasks(lab, "add", "--type", "change", "--title", "after ready"))
    line = watch.next()
    assert line["event"] == "task_changed" and line["task"]["id"] == made["created"]


@needs_store
def test_every_kind_arrives_with_the_task_as_a_list_row(lab):
    watch, _ready_line = _ready(lab)
    made = _answer(_tasks(lab, "add", "--type", "change", "--title", "kinds",
                          "--key", "k-1", "--status", "todo"))
    task = made["created"]
    seen = watch.until(lambda l: l["event"] == "counts")
    assert _changes(seen, task) == ["task_changed"]
    row = seen[0]["task"]
    listed = _answer(_tasks(lab, "list"))["tasks"][0]
    assert set(row) == set(listed)
    assert row["title"] == "kinds" and "objective" not in row
    assert seen[-1]["total"] == 1 and seen[-1]["counts"]["todo"] == 1

    _answer(_tasks(lab, "activity", "k-1", "did a thing"))
    seen = watch.until(lambda l: l["event"] == "counts")
    assert _changes(seen, task) == ["activity_added"]
    assert seen[0]["task"]["activities_count"] == 1

    # A claim writes nothing to the task: the raise announces it, with the task
    # shown in progress, and counted so.
    claimed = _answer(_tasks(lab, "claim", "--key", "k-1", "--worker", "w"))
    seen = watch.until(lambda l: l["event"] == "counts")
    assert _changes(seen, task) == ["run_started"]
    assert all(l["task"]["status"] == "in_progress" for l in seen if "task" in l)
    assert seen[-1]["counts"]["in_progress"] == 1 and seen[-1]["counts"]["todo"] == 0

    # A raise that ends without a move announces its end, the task where it rests.
    _answer(_tasks(lab, "release", claimed["execution"]["id"], "--outcome", "failed"))
    seen = watch.until(lambda l: l["event"] == "counts")
    assert _changes(seen, task) == ["run_ended"]
    assert all(l["task"]["status"] == "todo" for l in seen if "task" in l)
    assert seen[-1]["counts"]["in_progress"] == 0 and seen[-1]["counts"]["todo"] == 1

    claimed = _answer(_tasks(lab, "claim", "--key", "k-1", "--worker", "w"))
    watch.until(lambda l: l["event"] == "counts")
    _answer(_tasks(lab, "release", claimed["execution"]["id"], "--outcome", "ok"))
    seen = watch.until(lambda l: l["event"] == "counts")
    assert sorted(_changes(seen, task)) == ["run_ended", "task_changed"]
    assert seen[-1]["counts"]["complete"] == 1


@needs_store
def test_a_write_from_any_path_is_announced_and_a_rollback_is_not(lab):
    import psycopg
    watch, _ready_line = _ready(lab)
    task = _answer(_tasks(lab, "add", "--type", "change", "--title", "raw"))["created"]
    watch.until(lambda l: l["event"] == "counts")
    with psycopg.connect(DSN) as conn:
        conn.execute(f"update {lab['schema']}.tasks set title = 'rolled back' where id = %s",
                     (task,))
        conn.rollback()
        conn.execute(f"update {lab['schema']}.tasks set title = 'by hand' where id = %s",
                     (task,))
        conn.commit()
    seen = watch.until(lambda l: l["event"] == "counts")
    assert [l["task"]["title"] for l in seen if "task" in l] == ["by hand"]


@needs_store
def test_what_is_printed_is_what_the_scope_reads(lab):
    import psycopg
    here, all_, other = _ready(lab)[0], _ready(lab, "--all-projects")[0], \
        _ready(lab, "--project", OTHER)[0]
    mine = _answer(_tasks(lab, "add", "--type", "change", "--title", "mine"))["created"]
    with psycopg.connect(DSN) as conn:
        theirs = str(conn.execute(
            f"insert into {lab['schema']}.tasks (project_id, type, title) "
            "values (%s, 'change', 'theirs') returning id", (OTHER,)).fetchone()[0])
        conn.commit()
    seen_here = here.until(lambda l: l["event"] == "counts") + here.quiet()
    seen_other = other.until(lambda l: l["event"] == "counts") + other.quiet()
    seen_all = all_.quiet(5)
    assert _changes(seen_here, mine) and not _changes(seen_here, theirs)
    assert _changes(seen_other, theirs) and not _changes(seen_other, mine)
    assert _changes(seen_all, mine) and _changes(seen_all, theirs)
    assert [l["total"] for l in seen_here if l["event"] == "counts"] == [1]


@needs_store
def test_a_dropped_connection_catches_up_then_resyncs(lab):
    import psycopg
    watch, _ready_line = _ready(lab)
    task = _answer(_tasks(lab, "add", "--type", "change", "--title", "before"))["created"]
    watch.until(lambda l: l["event"] == "counts")
    with psycopg.connect(DSN) as conn:
        # The change commits after the watch's backend is gone, so its
        # notification reaches nobody and only the catch-up can carry it.
        conn.execute(f"update {lab['schema']}.tasks set title = 'while away' where id = %s",
                     (task,))
        killed = conn.execute(
            "select pg_terminate_backend(pid, 5000) from pg_stat_activity "
            "where application_name = 'tasks-watch' and pid <> pg_backend_pid()").fetchall()
        assert killed and all(row[0] for row in killed)
        conn.commit()
    seen = watch.until(lambda l: l["event"] == "resync", timeout=90)
    assert seen[0]["event"] == "lost"
    caught = [l for l in seen if l.get("catch_up")]
    assert [l["task"]["title"] for l in caught if l["task"]["id"] == task] == ["while away"]
    assert seen[-1]["caught_up"] == len(caught)
    assert watch.next()["event"] == "counts"
    # Listening again: a change after the resync arrives as an ordinary line.
    _answer(_tasks(lab, "set", task, "--title", "after"))
    line = watch.next()
    assert line["event"] == "task_changed" and line["task"]["title"] == "after"
    assert "catch_up" not in line


@needs_store
def test_closing_stdin_ends_it_with_exit_0(lab):
    watch, _ready_line = _ready(lab)
    watch.proc.stdin.close()
    assert watch.proc.wait(timeout=30) == 0
    assert watch.proc.stderr.read() == ""


@needs_store
def test_sigterm_ends_it_with_exit_0(lab):
    watch, _ready_line = _ready(lab)
    watch.proc.send_signal(signal.SIGTERM)
    assert watch.proc.wait(timeout=30) == 0
    assert watch.proc.stderr.read() == ""


@needs_store
def test_a_store_without_a_trigger_gains_it_before_ready(lab):
    """The store a release of `watch` left behind: it lacks a trigger, which is
    additive, so the watch brings the store up to this version and listens."""
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"drop trigger task_activities_notify_watch "
                     f"on {lab['schema']}.task_activities")
    watch, ready = _ready(lab)
    assert ready["event"] == "ready"
    watch.proc.stdin.close()
    assert watch.proc.wait(timeout=30) == 0
    [line] = watch.proc.stderr.read().splitlines()
    assert json.loads(line) == {"migrated": {
        "schema": lab["schema"], "created": [],
        "added": ["task_activities.task_activities_notify_watch"]}}
    assert _answer(_tasks(lab, "migrate"))["would_add"] == []


@needs_store
def test_a_store_without_the_triggers_is_refused_before_ready(lab):
    """Behind on something that is not additive as well, the store is brought
    up to date by nothing but `migrate --apply`, so the watch refuses."""
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"drop trigger task_activities_notify_watch "
                     f"on {lab['schema']}.task_activities")
        conn.execute(f"alter table {lab['schema']}.tasks "
                     f"drop constraint tasks_status_check")
        conn.execute(f"alter table {lab['schema']}.tasks add constraint tasks_status_check "
                     "check (status in ('draft','todo','in_progress','complete','closed'))")
    proc = subprocess.run([str(_cli.CLI_PATH), "watch"], cwd=lab["project"],
                          env=lab["env"], text=True, capture_output=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 5 and proc.stdout == ""
    error = json.loads(proc.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "schema_behind" and "migrate --apply" in error["hint"]
    assert "migrated" not in proc.stderr
    behind = _answer(_tasks(lab, "migrate"))
    assert "task_activities.task_activities_notify_watch" in behind["would_add"]


@needs_store
def test_a_disabled_capability_is_refused_with_exit_4(lab):
    settings = lab["project"] / "capabilities" / "settings.json"
    settings.write_text(json.dumps({"capabilities": {"tasks": {"enabled": False}}}))
    proc = subprocess.run([str(_cli.CLI_PATH), "watch"], cwd=lab["project"],
                          env=lab["env"], text=True, capture_output=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 4 and proc.stdout == ""


@needs_store
def test_a_hold_episode_row_announces_nothing_and_a_raise_still_does(lab):
    import psycopg
    watch, _ready_line = _ready(lab)
    task = _answer(_tasks(lab, "add", "--type", "change", "--title", "held",
                          "--key", "k-hold", "--status", "todo"))["created"]
    watch.until(lambda l: l["event"] == "counts")
    with psycopg.connect(DSN) as conn:
        conn.execute(f"""insert into {lab['schema']}.task_executions
                           (task_id, attempt, worker, status, metrics, started_at, ended_at)
                         values (%s, 0, 'w', 'handback', %s::jsonb, now(), now())""",
                     (task, json.dumps({"hold": {"said": "not now", "how": "held"}})))
        conn.commit()
    assert _changes(watch.quiet(), task) == []
    _answer(_tasks(lab, "claim", "--key", "k-hold", "--worker", "w"))
    seen = watch.until(lambda l: l["event"] == "counts")
    # A claim writes nothing to the task: the raise alone announces it.
    assert _changes(seen, task) == ["run_started"]


@needs_store
def test_a_store_whose_watch_function_announces_holds_catches_up(lab):
    import psycopg
    from psycopg.rows import dict_row
    schema = lab["schema"]
    ddl = mod._schema_ddl(schema)
    start = ddl.index(f"create or replace function {schema}.notify_watch()")
    end = ddl.index("$$ language plpgsql;", start) + len("$$ language plpgsql;")
    function = ddl[start:end]
    skip = function[function.index("        -- A hold episode"):
                    function.index("        task := new.task_id;")]
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(function.replace(skip, ""))
        with conn.cursor() as cur:
            _missing, behind = mod._behind(mod._catalog(cur, schema))
        assert behind == ["notify_watch()"]
        _answer(_tasks(lab, "list"))
        with conn.cursor() as cur:
            _missing, behind = mod._behind(mod._catalog(cur, schema))
        assert behind == []
