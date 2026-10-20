#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.6.0",
#                 "callva-harness-runner==0.8.0", "pyyaml>=6"]
# ///
"""A task assigned to another project, and the grant that lets that project take it.

A project takes another project's tasks only by a grant, `accepts_from` in a
worker's `takes`. A write leaving an open task on another project's id is
judged by that project's grants in the same transaction: refused with exit 4
`not_accepted` when none takes it, answered with `accepted_by` when one does,
and warned, never refused, when no service of that project runs. The grant's
claim takes the task until a task of its own project takes it in. The verbs are
driven as consuming projects drive them, through the CLI, against a store in
TASKS_TEST_DSN, and the store-backed checks skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.6.0' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
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

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")
FAKE_CLAUDE = Path(__file__).resolve().parent / "fakes" / "claude"

PROFILE = f"""harness = "claude"
model = "claude-opus-5"
timeout_seconds = 600
permission_mode = "bypassPermissions"
cli_path = "{FAKE_CLAUDE}"
"""

LOCAL = """---
takes: [chore]
profile: plain
---

Do the chores.
"""


def intake(*projects: str, statuses=("todo", "waiting"), types=("change",)) -> str:
    return f"""---
description: Takes in what other projects hand here.
takes:
  - status: [{', '.join(statuses)}]
    type: [{', '.join(types)}]
    accepts_from: [{', '.join(projects)}]
profile: plain
---

Take it in, or hand it back.
"""


# --- What a grant is, with no store --------------------------------------------

def _filters(raw, tags=(), assignee=None):
    said: list[str] = []
    filters, _shorthand = mod._take_filters(raw, list(tags), assignee, said.append)
    return filters, said


def test_a_grant_is_a_filter_key_and_names_no_assignee():
    [granted], said = _filters({"type": ["change"], "accepts_from": ["prj_a"]})
    assert said == [] and granted["accepts_from"] == ["prj_a"]
    _filters_, said = _filters({"type": ["change"], "accepts_from": ["prj_a"],
                                "assignee": ["x"]})
    assert any("names no `assignee`" in one for one in said)
    _filters_, said = _filters({"type": ["change"], "accepts_from": ["prj_a"]},
                               assignee="x")
    assert any("names no `assignee`" in one for one in said)


def test_a_grant_names_project_ids_as_written():
    said = mod._grant_problems({"accepts_from": ["${OTHER}", "prj_here", "prj_a"]},
                               "prj_here")
    assert len(said) == 2
    assert "cannot substitute it" in said[0] and "this project, prj_here" in said[1]


def test_a_grant_selects_only_the_projects_it_names():
    [granted], _ = _filters({"type": ["change"], "accepts_from": ["prj_a"]})
    [own], _ = _filters({"type": ["change"]})
    task = {"type": "change", "tags": [], "assignee": "prj_here"}
    assert mod._selects(granted, {**task, "project_id": "prj_a"}, "todo")
    assert not mod._selects(granted, {**task, "project_id": "prj_b"}, "todo")
    # A grant and a filter without one never select the same task, so two
    # workers holding them do not overlap; two grants overlap in a project both
    # name.
    assert not mod._could_share(granted, own)
    [other], _ = _filters({"type": ["change"], "accepts_from": ["prj_b"]})
    assert not mod._could_share(granted, other)
    [both], _ = _filters({"type": ["change"], "accepts_from": ["prj_b", "prj_a"]})
    assert mod._could_share(granted, both)


def test_a_grant_takes_no_type_away_from_the_catch_all():
    found = {"source": "store", "where": "worker 'intake'",
             "spec": {"takes": [{"type": ["change"], "accepts_from": ["prj_a"]},
                                {"type": ["defect"]}]}, "body": "x"}
    assert mod._types_named(found) == {"defect"}


# --- Through the CLI, against a real store -----------------------------------

def _project(root: Path, project_id: str) -> Path:
    (root / ".git").mkdir(parents=True)
    envelope = root / "capabilities"
    tasks = envelope / "tasks"
    (tasks / "profiles").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id,
        "slug": "lab-" + secrets.token_hex(3)}))
    (tasks / "connections.json").write_text(json.dumps({
        "default": "local", "connections": {"local": {"allow_write": True}}}))
    (tasks / "profiles" / "plain.toml").write_text(PROFILE)
    return root


@pytest.fixture()
def lab(tmp_path):
    import psycopg
    schema = "tasks_test_" + secrets.token_hex(4)
    ids = {name: "prj_" + uuid.uuid4().hex[:12] for name in ("here", "there", "third")}
    _cli.write_store_setting(tmp_path / "config", schema)
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "FAKE_ENGINE_TASKS": str(_cli.CLI_PATH)})
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                   "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                   "CLAUDE_PROJECT_DIR", *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    lab = {name: _project(tmp_path / name, project) for name, project in ids.items()}
    lab.update(ids=ids, env=env, schema=schema, tmp=tmp_path)
    assert cli(lab, "migrate").returncode == 0
    try:
        yield lab
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def cli(lab, *args: str, where: str = "here", stdin: str | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    project = lab[where]
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=project,
                          env={**lab["env"], "CLAUDE_PROJECT_DIR": str(project),
                               "CAPABILITIES_PROJECT_ENVELOPE": str(project / "capabilities"),
                               **(env or {})},
                          input=stdin, text=True, capture_output=True, timeout=180)


def ok(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def refused(proc: subprocess.CompletedProcess, exit_code: int) -> dict:
    assert proc.returncode == exit_code, proc.stdout + proc.stderr
    return json.loads(proc.stderr.strip().splitlines()[-1])["error"]


def save(lab, name: str, text: str, where: str):
    return ok(cli(lab, "workers", "save", name, "-", where=where, stdin=text))


def handed(lab, *extra: str, kind: str = "change", status: str = "todo",
           where: str = "here", to: str = "there") -> subprocess.CompletedProcess:
    return cli(lab, "add", "--type", kind, "--title", "for another project",
               "--status", status, "--assignee", lab["ids"][to], *extra, where=where)


@needs_store
def test_a_project_with_no_grant_is_refused_at_the_write(lab):
    save(lab, "local", LOCAL, "there")
    error = refused(handed(lab), 4)
    assert error["code"] == "not_accepted"
    assert "none of its enabled workers names `accepts_from`" in error["message"]
    assert f"accepts {lab['ids']['there']}" in error["hint"]
    assert ok(cli(lab, "counts"))["total"] == 0
    # A draft has not been released to anybody, and a name the store knows as
    # no project is a person's, as always.
    assert ok(handed(lab, status="draft"))["status"] == "draft"
    unknown = ok(cli(lab, "add", "--type", "change", "--title", "t", "--status", "todo",
                     "--assignee", "prj_nobody_here"))
    assert "accepted_by" not in unknown


@needs_store
def test_a_grant_accepts_the_task_and_names_who_takes_it(lab):
    save(lab, "intake", intake(lab["ids"]["here"]), "there")
    added = ok(handed(lab))
    assert added["accepted_by"] == {"project": lab["ids"]["there"], "worker": "intake"}
    # Nothing serves that project, which is said and never refused.
    assert "no tasks service of" in added["warnings"][0]
    # Not the type its grant takes: refused, saying what it accepts and from whom.
    error = refused(handed(lab, kind="defect"), 4)
    assert error["code"] == "not_accepted"
    assert "by worker 'intake'" in error["message"]
    assert f"from {lab['ids']['here']}" in error["message"]
    # Not from a project its grant names.
    assert refused(handed(lab, where="third"), 4)["code"] == "not_accepted"


@needs_store
def test_set_judges_the_move_as_it_stands_once_written(lab):
    save(lab, "intake", intake(lab["ids"]["here"], statuses=("waiting",)), "there")
    task = ok(handed(lab, status="draft"))["created"]
    # A draft is no one's yet; released into todo it is judged, and refused,
    # the task left where it was.
    error = refused(cli(lab, "set", task, "--status", "todo"), 4)
    assert error["code"] == "not_accepted"
    assert ok(cli(lab, "show", task))["task"]["status"] == "draft"
    moved = ok(cli(lab, "set", task, "--status", "waiting"))
    assert moved["accepted_by"]["worker"] == "intake"
    assert refused(cli(lab, "set", task, "--type", "defect"), 4)["code"] == "not_accepted"
    # A field that moves nothing of where it stands is not judged.
    assert "accepted_by" not in ok(cli(lab, "set", task, "--title", "renamed"))


@needs_store
def test_accepts_answers_ahead_of_time(lab):
    save(lab, "intake", intake(lab["ids"]["here"]), "there")
    save(lab, "other", intake(lab["ids"]["third"], types=("defect",)), "there")
    answer = ok(cli(lab, "accepts", lab["ids"]["there"]))
    assert answer["from"] == lab["ids"]["here"] and answer["known"] is True
    assert answer["accepts"] == [{"worker": "intake", "status": ["todo", "waiting"],
                                  "type": ["change"], "tags": []}]
    assert answer["service_running"] is False
    none = ok(cli(lab, "accepts", lab["ids"]["here"], where="there"))
    assert none["accepts"] == [] and "keeps no task and no worker" in none["why"]


@needs_store
def test_a_running_service_is_seen_from_another_project(lab):
    import psycopg
    save(lab, "intake", intake(lab["ids"]["here"]), "there")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("select pg_try_advisory_lock(%s, hashtext(%s))",
                     (mod._PRESENCE_CLASS, mod._presence_key(lab["schema"],
                                                             lab["ids"]["there"])))
        added = ok(handed(lab))
        assert "warnings" not in added
        assert ok(cli(lab, "accepts", lab["ids"]["there"]))["service_running"] is True
    assert "warnings" in ok(handed(lab))


@needs_store
def test_the_grant_takes_the_task_until_a_task_of_its_own_takes_it_in(lab):
    save(lab, "default", "---\nenabled: false\n---\n", "there")
    save(lab, "intake", intake(lab["ids"]["here"]), "there")
    origin = ok(handed(lab, status="waiting"))["created"]
    record = lab["tmp"] / "turns.jsonl"
    dry = ok(cli(lab, "run", "intake", where="there"))
    assert dry["would_claim"] == origin
    ran = ok(cli(lab, "run", "intake", "--apply", where="there",
                 env={"FAKE_ENGINE_INTAKE": "1", "FAKE_ENGINE_RECORD": str(record)}))
    assert ran["claimed"] == origin
    linked = ran["taken_in"]
    [raised] = ok(cli(lab, "runs", origin))["executions"]
    assert raised["status"] == "ok" and raised["metrics"]["taken_in"] == linked
    # The turn was told whose task it holds and how a task is taken in.
    [turn] = [json.loads(line) for line in record.read_text().splitlines()]
    assert "ANOTHER PROJECT'S TASK" in turn["prompt"]
    assert f"origin_ref {origin}" in turn["prompt"]
    # The origin rests where it was, still over to that project; the task that
    # took it in is that project's own and names it.
    shown = ok(cli(lab, "show", origin))
    assert shown["task"]["status"] == "waiting"
    assert shown["task"]["assignee"] == lab["ids"]["there"]
    assert "cost_total" not in (shown["task"]["metadata"] or {})
    assert shown["activities"][-1]["origin_project"] == lab["ids"]["there"]
    mine = ok(cli(lab, "show", linked, where="there"))["task"]
    assert mine["project_id"] == lab["ids"]["there"]
    assert mine["metadata"]["origin"] == "tasks" and mine["metadata"]["origin_ref"] == origin
    # Taken in, it is not taken again.
    assert ok(cli(lab, "run", "intake", where="there"))["would_claim"] is None
    error = refused(cli(lab, "run", "intake", "--key", origin, "--apply", where="there"), 6)
    assert f"taken in already, as {linked}" in error["message"]
    # Answered and handed back, it returns to the project that raised it.
    back = ok(cli(lab, "set", origin, "--clear", "assignee", where="there"))
    assert back["handed_back"] is True and back["task"]["status"] == "todo"


@needs_store
def test_a_worker_without_the_grant_never_reaches_the_task(lab):
    save(lab, "local", LOCAL.replace("[chore]", "[change]"), "there")
    save(lab, "intake", intake(lab["ids"]["here"]), "there")
    origin = ok(handed(lab))["created"]
    assert ok(cli(lab, "run", "local", where="there"))["would_claim"] is None
    error = refused(cli(lab, "run", "local", "--key", origin, where="there"), 4)
    assert error["code"] == "policy"
    # Both take `change`, one of its own project and one under the grant, so
    # no task is selected by both and neither is refused as an overlap.
    doctor = ok(cli(lab, "doctor", where="there"))
    rows = {row["worker"]: row for row in doctor["workers"]}
    assert rows["intake"]["ok"] is True and rows["local"]["ok"] is True
