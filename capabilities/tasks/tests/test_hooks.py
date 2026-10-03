#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""A worker's hooks: the project's own commands `run` starts around a turn.

What a worker file declares is checked with no store. What `before` decides
about a claim and what `after` is told about a settled raise are driven against
a real store, with the harness replaced by one that finishes the task the way a
worker would, and with each hook a small script that records what it was given
and answers as the test tells it. The service is driven once through the CLI.
The store-backed checks read TASKS_TEST_DSN and skip when it is unset; every run
works in a schema of its own and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from callva import harness_runner
from callva.harness_runner import FailureKind, Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()
FAKE_CLAUDE = Path(__file__).resolve().parent / "fakes" / "claude"

PROFILE = """harness = "claude"
model = "claude-opus-5"
timeout_seconds = 600
permission_mode = "bypassPermissions"
"""

# A hook that records its stdin, its argv and its environment, then answers by
# the file the test wrote for the task it was asked about: `exit` and `print`,
# or `sleep` first. A task the test wrote nothing for is let through.
HOOK = """
import json, os, sys, time
from pathlib import Path
here = Path(sys.argv[1])
task = json.loads(sys.stdin.read())
seen = {"argv": sys.argv[2:], "cwd": os.getcwd(), "task": task,
        "env": {k: v for k, v in os.environ.items() if k.startswith("TASKS_")}}
with (here / "seen.jsonl").open("a") as out:
    out.write(json.dumps(seen) + "\\n")
answer = here / f"{task.get('unique_key')}.json"
told = json.loads(answer.read_text()) if answer.is_file() else {}
time.sleep(told.get("sleep", 0))
if told.get("print"):
    print("checking")
    print(told["print"])
sys.exit(told.get("exit", 0))
"""


def write_worker(project: Path, name: str, front: str, body: str = "the body.") -> Path:
    path = project / "capabilities" / "tasks" / "workers" / f"{name}.md"
    path.write_text(f"---\n{textwrap.dedent(front).strip()}\n---\n\n{body}\n")
    return path


def hooked(project: Path, before: bool = True, after: bool = False,
           name: str = "alpha", kind: str = "alpha") -> Path:
    lines = [f"takes: [{kind}]", "profile: plain", "hooks:"]
    if before:
        lines.append(f"  before: {sys.executable} hooks/hook.py hooks/before --pre")
    if after:
        lines.append(f"  after: {sys.executable} hooks/hook.py hooks/after --post")
    return write_worker(project, name, "\n".join(lines))


def tell(project: Path, which: str, key: str, **answer) -> None:
    (project / "hooks" / which / f"{key}.json").write_text(json.dumps(answer))


def seen(project: Path, which: str) -> list[dict]:
    path = project / "hooks" / which / "seen.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def project(tmp_path, monkeypatch):
    envelope = tmp_path / "capabilities" / "tasks"
    (envelope / "workers").mkdir(parents=True)
    (envelope / "profiles").mkdir()
    (envelope / "profiles" / "plain.toml").write_text(PROFILE)
    write_worker(tmp_path, "alpha", "takes: [alpha]\nprofile: plain")
    write_worker(tmp_path, "default", "enabled: false")
    for which in ("before", "after"):
        (tmp_path / "hooks" / which).mkdir(parents=True)
    (tmp_path / "hooks" / "hook.py").write_text(HOOK)
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setattr(mod, "_STATE_HOME", tmp_path / "state")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine"))
    return tmp_path


def problems_of(name: str) -> list[str]:
    _rows, broken = mod._workers_report()
    return [one for one in broken if one.startswith(f"{name}:")]


# --- What is declared --------------------------------------------------------

def test_hooks_are_read_and_shown_by_doctor(project):
    hooked(project, before=True, after=True)
    worker = mod._worker("alpha")
    assert set(worker["hooks"]) == {"before", "after"}
    assert worker["hooks"]["before"].endswith("hooks/hook.py hooks/before --pre")
    [row] = [r for r in mod._workers_report()[0] if r["worker"] == "alpha"]
    assert row["ok"] and row["hooks"] == worker["hooks"]


def test_a_worker_without_hooks_declares_none(project):
    worker = mod._worker("alpha")
    assert worker["hooks"] == {}
    [row] = [r for r in mod._workers_report()[0] if r["worker"] == "alpha"]
    assert "hooks" not in row


@pytest.mark.parametrize("hooks, said", [
    ("{before: a, during: b}", "its `hooks` names 'during', which nothing reads"),
    ("[a, b]", "its `hooks` is a mapping of before and after"),
    ("{before: ''}", "its `hooks.before` is one command line"),
    ("{after: [a, b]}", "its `hooks.after` is one command line"),
    ("{before: 'a \"b'}", "its `hooks.before` does not split into a command"),
])
def test_hooks_that_cannot_be_read_are_refused(project, hooks, said):
    write_worker(project, "alpha", f"takes: [alpha]\nprofile: plain\nhooks: {hooks}")
    assert any(said in one for one in problems_of("alpha")), problems_of("alpha")


def test_the_fingerprint_moves_with_a_hook(project):
    before = mod._service_fingerprint()
    hooked(project)
    moved = mod._service_fingerprint()
    assert moved != before
    write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\nhooks:\n"
                 "  before: other-command")
    assert mod._service_fingerprint() not in (before, moved)


# --- Against a real store ----------------------------------------------------

HERE = "prj_hooks"
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
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


@pytest.fixture
def turns(store, monkeypatch, capsys):
    """The harness replaced by a worker that finishes the task it holds."""
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
            mod.cmd_activity(entry, [key, "done"])
            mod.cmd_set(entry, [key, "--status", "complete"])
            capsys.readouterr()
            monkeypatch.delenv("TASKS_EXECUTION")
            return Result(ok=True, harness="claude", answer="done", session_id=session.id,
                          model="m", cost_usd=0.0, duration_ms=1, num_turns=1)

    monkeypatch.setattr(mod, "_harness_runner", Worker)
    return ran


def add(entry, capsys, key: str, kind: str = "alpha") -> str:
    mod.cmd_add(entry, ["--type", kind, "--title", f"a {kind}", "--key", key,
                        "--status", "todo"])
    return json.loads(capsys.readouterr().out)["created"]


def answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def shown(entry, capsys, key: str) -> dict:
    mod.cmd_show(entry, [key])
    return answer(capsys)


def raises_of(entry, capsys, key: str) -> list[dict]:
    mod.cmd_runs(entry, [key])
    return answer(capsys)["executions"]


def changes_of(entry, capsys, key: str) -> list[dict]:
    mod.cmd_history(entry, [key])
    return answer(capsys)["changes"]


@needs_store
def test_go_claims_as_before_and_the_hook_is_given_the_task(project, store, turns,
                                                           capsys):
    entry, _schema, _conn = store
    hooked(project)
    tid = add(entry, capsys, "t-go")
    task = shown(entry, capsys, "t-go")["task"]
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-go" and report["passed_over"] == []
    assert turns == ["t-go"]
    [asked] = seen(project, "before")
    assert asked["task"] == task
    assert asked["argv"] == ["--pre"] and Path(asked["cwd"]) == project.resolve()
    assert {k: asked["env"][k] for k in ("TASKS_TASK_ID", "TASKS_TASK_KEY",
                                         "TASKS_TASK_TYPE", "TASKS_TASK_ASSIGNEE",
                                         "TASKS_WORKER", "TASKS_PROJECT_ROOT")} == {
        "TASKS_TASK_ID": tid, "TASKS_TASK_KEY": "t-go", "TASKS_TASK_TYPE": "alpha",
        "TASKS_TASK_ASSIGNEE": "", "TASKS_WORKER": "alpha",
        "TASKS_PROJECT_ROOT": str(project)}
    [raised] = raises_of(entry, capsys, "t-go")
    assert raised["status"] == "ok" and raised["attempt"] == 1


@needs_store
def test_defer_sets_the_pickup_and_the_next_task_is_taken(project, store, turns, capsys):
    import datetime
    entry, _schema, _conn = store
    hooked(project)
    add(entry, capsys, "t-later")
    add(entry, capsys, "t-now")
    moment = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(hours=3)).replace(microsecond=0)
    tell(project, "before", "t-later", exit=75, print=moment.isoformat())

    mod.cmd_run(entry, ["alpha"])
    dry = answer(capsys)
    assert dry["would_claim"] == "t-now" and dry["applied"] is False
    [held] = dry["passed_over"]
    assert (held["task"], held["verdict"]) == ("t-later", "defer")
    assert "pickup_set" not in held
    assert shown(entry, capsys, "t-later")["task"]["pickup_at"] is None

    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-now" and turns == ["t-now"]
    [held] = report["passed_over"]
    assert held["verdict"] == "defer" and held["pickup_set"] is True
    assert held["said"] == moment.isoformat()
    later = shown(entry, capsys, "t-later")
    assert later["task"]["status"] == "todo" and later["activities"] == []
    assert (datetime.datetime.fromisoformat(later["task"]["pickup_at"]) == moment)
    [change] = changes_of(entry, capsys, "t-later")
    assert (change["field"], change["actor"]) == ("pickup", "hook:alpha")
    assert raises_of(entry, capsys, "t-later") == []


@needs_store
def test_defer_to_a_bare_date(project, store, turns, capsys):
    entry, _schema, _conn = store
    hooked(project)
    add(entry, capsys, "t-day")
    tell(project, "before", "t-day", exit=75, print="2099-01-02")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None
    assert report["passed_over"][0]["until"].startswith("2099-01-02T00:00:00")
    assert shown(entry, capsys, "t-day")["task"]["pickup_at"] is not None


@needs_store
@pytest.mark.parametrize("told, why", [
    ({"exit": 75}, "exited 75 without a moment"),
    ({"exit": 75, "print": "tomorrow"}, "exited 75 without a moment"),
    ({"exit": 75, "print": "2099-01-02T10:00:00"}, "exited 75 without a moment"),
    ({"exit": 3, "print": "busy"}, "exited 3"),
    ({"sleep": 5}, "timed out after 1s"),
])
def test_skip_writes_nothing_and_the_next_task_is_taken(project, store, turns, capsys,
                                                       monkeypatch, told, why):
    entry, _schema, _conn = store
    monkeypatch.setitem(mod._HOOK_TIMEOUTS, "before", 1)
    hooked(project)
    add(entry, capsys, "t-skip")
    add(entry, capsys, "t-next")
    tell(project, "before", "t-skip", **told)
    before = shown(entry, capsys, "t-skip")["task"]
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-next"
    [held] = report["passed_over"]
    assert held["task"] == "t-skip" and held["verdict"] == "skip"
    assert held["why"].startswith(why)
    assert shown(entry, capsys, "t-skip")["task"] == before
    assert changes_of(entry, capsys, "t-skip") == []
    assert raises_of(entry, capsys, "t-skip") == []


@needs_store
def test_a_hook_that_cannot_start_skips_every_task_and_nothing_is_claimed(
        project, store, turns, capsys):
    entry, _schema, _conn = store
    write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\nhooks:\n"
                 "  before: ./no-such-hook --flag")
    add(entry, capsys, "t-one")
    add(entry, capsys, "t-two")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None and turns == []
    assert [h["task"] for h in report["passed_over"]] == ["t-one", "t-two"]
    assert all(h["verdict"] == "skip" and h["why"].startswith("could not start")
               for h in report["passed_over"])
    for key in ("t-one", "t-two"):
        assert shown(entry, capsys, key)["task"]["status"] == "todo"
        assert raises_of(entry, capsys, key) == []


@needs_store
def test_key_is_refused_with_the_hooks_reason(project, store, turns, capsys):
    entry, _schema, _conn = store
    hooked(project)
    add(entry, capsys, "t-key")
    tell(project, "before", "t-key", exit=2, print="not today")
    mod.cmd_run(entry, ["alpha", "--key", "t-key"])
    dry = answer(capsys)
    assert dry["would_claim"] is None
    assert dry["passed_over"][0]["said"] == "not today"
    with pytest.raises(SystemExit) as stopped:
        mod.cmd_run(entry, ["alpha", "--key", "t-key", "--apply"])
    assert stopped.value.code == 6
    err = json.loads(capsys.readouterr().err)
    assert "t-key was not claimed" in json.dumps(err) and "not today" in json.dumps(err)
    assert raises_of(entry, capsys, "t-key") == [] and turns == []
    tell(project, "before", "t-key", exit=0)
    mod.cmd_run(entry, ["alpha", "--key", "t-key", "--apply"])
    assert answer(capsys)["claimed"] == "t-key"


@needs_store
def test_key_asks_the_hook_about_a_task_whose_wait_ended(project, store, turns, capsys):
    """A waiting task whose pickup has passed becomes claimable only when the
    sweep returns it to todo; its hook is asked about it all the same."""
    entry, schema, conn = store
    hooked(project)
    add(entry, capsys, "t-waited")
    conn.execute(f"""update {schema}.tasks set status = 'waiting', assignee = 'someone',
                            pickup_at = now() - interval '1 minute'
                      where unique_key = 't-waited'""")
    tell(project, "before", "t-waited", exit=3, print="not yet")
    with pytest.raises(SystemExit) as stopped:
        mod.cmd_run(entry, ["alpha", "--key", "t-waited", "--apply"])
    assert stopped.value.code == 6
    assert "not yet" in capsys.readouterr().err
    [asked] = seen(project, "before")
    assert asked["task"]["unique_key"] == "t-waited" and asked["task"]["status"] == "todo"
    assert raises_of(entry, capsys, "t-waited") == [] and turns == []


@needs_store
def test_key_asks_the_hook_about_a_task_whose_lease_lapsed(project, store, turns, capsys):
    """An in-progress task whose raise's lease has lapsed becomes claimable only
    when the sweep frees it; its hook is asked about it all the same, and no
    raise is opened beside the one the sweep closed."""
    entry, schema, conn = store
    hooked(project)
    tid = add(entry, capsys, "t-lapsed")
    conn.execute(f"update {schema}.tasks set status = 'in_progress' "
                 "where unique_key = 't-lapsed'")
    conn.execute(f"""insert into {schema}.task_executions
                       (task_id, attempt, worker, status, lease_until)
                     values ('{tid}', 1, 'alpha', 'running', now() - interval '1 minute')""")
    tell(project, "before", "t-lapsed", exit=3, print="not yet")
    with pytest.raises(SystemExit) as stopped:
        mod.cmd_run(entry, ["alpha", "--key", "t-lapsed", "--apply"])
    assert stopped.value.code == 6
    assert "not yet" in capsys.readouterr().err
    [asked] = seen(project, "before")
    assert asked["task"]["unique_key"] == "t-lapsed"
    [swept] = raises_of(entry, capsys, "t-lapsed")
    assert swept["status"] == "abandoned" and turns == []


@needs_store
def test_after_is_told_the_outcome_and_its_failure_changes_nothing(project, store, turns,
                                                                   capsys):
    entry, _schema, _conn = store
    hooked(project, before=False, after=True)
    add(entry, capsys, "t-after")
    tell(project, "after", "t-after", exit=4, print="could not notify")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-after" and "passed_over" not in report
    assert report["after_hook"] == {"exit": 4, "why": "exited 4", "said": "could not notify"}
    [told] = seen(project, "after")
    assert told["task"]["status"] == "complete"
    assert told["env"]["TASKS_EXECUTION"] == report["execution"]
    assert (told["env"]["TASKS_OUTCOME"], told["env"]["TASKS_LANDED"]) == ("ok", "complete")
    assert told["env"]["TASKS_TASK_KEY"] == "t-after"
    [raised] = raises_of(entry, capsys, "t-after")
    assert raised["status"] == "ok"
    assert raised["metrics"]["after_hook"] == {"why": "exited 4", "said": "could not notify"}


@needs_store
def test_after_runs_on_a_park_and_a_quiet_success_leaves_the_metrics_alone(
        project, store, turns, capsys):
    entry, _schema, _conn = store
    write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\nroutines: [absent]\n"
                 f"hooks:\n  after: {sys.executable} hooks/hook.py hooks/after")
    add(entry, capsys, "t-park")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["parked"] is True and report["after_hook"] == {"exit": 0}
    [told] = seen(project, "after")
    assert told["env"]["TASKS_OUTCOME"] == "handback"
    assert told["env"]["TASKS_LANDED"] == told["task"]["status"] == "draft"
    [raised] = raises_of(entry, capsys, "t-park")
    assert "after_hook" not in raised["metrics"]


# --- The service, through the CLI --------------------------------------------

@pytest.fixture()
def lab(tmp_path):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    tasks = envelope / "tasks"
    (tasks / "workers").mkdir(parents=True)
    (tasks / "profiles").mkdir()
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1",
        "id": "prj_" + uuid.uuid4().hex[:12], "slug": "lab-" + secrets.token_hex(3)}))
    (tasks / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {"local": {
            "db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
            "db_user": info.get("user"), "db_name": info.get("dbname"),
            "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
            "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True}}}))
    (tasks / "profiles" / "plain.toml").write_text(PROFILE + f'cli_path = "{FAKE_CLAUDE}"\n')
    for which in ("before", "after"):
        (project / "hooks" / which).mkdir(parents=True)
    (project / "hooks" / "hook.py").write_text(HOOK)
    hooked(project)
    write_worker(project, "default", "enabled: false")
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
        "TASKS_TEST_PASSWORD": info.get("password") or "",
        "FAKE_ENGINE_TASKS": str(_cli.CLI_PATH),
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_STORE_URL",
                   "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    lab = {"project": project, "env": env}
    assert tasks_cli(lab, "migrate", "--apply").returncode == 0
    try:
        yield lab
    finally:
        tasks_cli(lab, "service", "stop", "--end-turns", "--timeout", "30", "--force")
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def tasks_cli(lab, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=lab["env"],
                          text=True, capture_output=True, timeout=180)


def answer_of(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def poll_for(check, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.2)
    raise AssertionError(f"not reached within {seconds}s")


@needs_store
def test_the_service_logs_what_a_hook_held_back_and_does_not_spin(lab):
    project = lab["project"]
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    settings = project / "capabilities" / "tasks" / "service" / "config.toml"
    settings.write_text(settings.read_text().replace("poll_seconds = 60", "poll_seconds = 3600")
                        .replace("shutdown_grace_seconds = 60", "shutdown_grace_seconds = 5"))
    assert answer_of(tasks_cli(lab, "service", "doctor"))["ok"]
    tell(project, "before", "t-held", exit=3, print="the resource is busy")
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "held", "--key", "t-held",
                        "--status", "todo"))
    assert answer_of(tasks_cli(lab, "service", "start"))["running"]

    def logged():
        lines = answer_of(tasks_cli(lab, "service", "logs"))["lines"]
        return lines if any("passed over t-held" in line for line in lines) else None

    lines = poll_for(logged, 60)
    [ended] = [line for line in lines if "passed over t-held" in line]
    assert "claimed nothing" in ended and "skip (exited 3)" in ended
    assert "the resource is busy" in ended
    time.sleep(3)
    lines = answer_of(tasks_cli(lab, "service", "logs"))["lines"]
    assert sum(" started: worker alpha" in line for line in lines) == 1
    status = answer_of(tasks_cli(lab, "service", "status"))
    assert status["lanes"][0]["held_until_poll"] is True
    assert len(seen(project, "before")) == 1
    shown = answer_of(tasks_cli(lab, "show", "t-held"))
    assert shown["task"]["status"] == "todo" and shown["task"]["pickup_at"] is None
    assert answer_of(tasks_cli(lab, "runs", "t-held"))["executions"] == []
