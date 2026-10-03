#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""Machine mode: one process serving every project that joined it.

The opt-in list, the machine settings and `join`'s refusals are checked with
no store. The machine process itself runs through the CLI, as a supervisor
runs it, over three fixture projects written into a temp directory - an
orchard identified by a UUID, a kiln whose folder is the pottery-shed, and a
mill - each with a worker of its own; the orchard and the mill share one
schema and store, and the kiln has a schema and a store of its own, reached
through a secret held in the user tier. Turns are real `tasks run` children on
the stand-in harness. The store-backed checks read TASKS_TEST_DSN and skip
when it is unset; every run works in schemas of its own and drops them.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_service as base  # noqa: E402

mod = base.mod
needs_store = base.needs_store
DSN = base.DSN
poll_for = base.poll_for

ORCHARD_ID = str(uuid.uuid4())

PROJECTS_TEXT = """# Projects served by the machine tasks service. Written by `tasks service join|leave`; do not edit by hand.
version = 1

[projects.{orchard}]
root = "/srv/fields/orchard"
slug = "orchard"
joined_at = 2026-10-02T18:00:00Z
joined_by = "ops-person"

[projects.prj_k1ln00000001]
root = "/srv/fields/pottery-shed"
slug = "kiln"
joined_at = 2026-10-02T18:05:00Z
joined_by = "ops-person"
"""


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A config home and a state home of the test's own, read by the module."""
    monkeypatch.setattr(mod, "_CONFIG_HOME", tmp_path / "config")
    monkeypatch.setattr(mod, "_STATE_HOME", tmp_path / "state")
    monkeypatch.setattr(mod, "CREDENTIALS_ENV", tmp_path / "config" / "tasks" / "credentials.env")
    return tmp_path


# --- What is declared --------------------------------------------------------

def test_the_manifest_declares_the_machine_service():
    machine = mod.SERVICE["machine"]
    assert machine == {
        "schema": "capabilities.service.machine.v1",
        "command": ["tasks", "service", "run", "--machine"],
        "doctor": ["tasks", "service", "doctor", "--machine"],
        "projects": "$XDG_CONFIG_HOME/tasks/service/projects.toml",
        "config": "$XDG_CONFIG_HOME/tasks/service/config.toml",
        "state": "$XDG_STATE_HOME/capabilities/machine/tasks",
    }
    assert {"run", "status", "doctor", "join", "leave"} <= set(mod.SERVICE["verbs"])
    assert set(mod.SERVICE["verbs"]) == set(mod._SERVICE_VERBS)


def test_the_list_is_written_in_its_format_and_read_back(homes):
    listed = {ORCHARD_ID: {"root": "/srv/fields/orchard", "slug": "orchard",
                           "joined_at": "2026-10-02T18:00:00Z", "joined_by": "ops-person"},
              "prj_k1ln00000001": {"root": "/srv/fields/pottery-shed", "slug": "kiln",
                                   "joined_at": "2026-10-02T18:05:00Z",
                                   "joined_by": "ops-person"}}
    mod._write_machine_projects(listed)
    path = homes / "config" / "tasks" / "service" / "projects.toml"
    assert path == mod._machine_projects_path()
    text = path.read_text()
    expected = PROJECTS_TEXT.format(orchard=ORCHARD_ID)
    # Sorted by id: the UUID and the prj_ id fall where they fall.
    blocks = sorted(expected.split("\n\n")[1:])
    assert sorted(text.split("\n\n")[1:]) == blocks
    assert text.startswith(expected.split("\n\n")[0] + "\n\n")
    assert mod._read_machine_projects() == listed
    assert not list(path.parent.glob(".projects.toml.*"))


@pytest.mark.parametrize("text, said", [
    ("version = 1\n[projects.x\n", "could not be read"),
    ("version = 2\n", "`version` is 1"),
    ("version = 1\nextra = 1\n", "'extra'"),
    ("version = 1\n[projects.p]\nroot = \"relative\"\nslug = \"p\"\n", "absolute path"),
    ("version = 1\n[projects.p]\nroot = \"/a\"\nslug = \"p\"\nweight = 2\n", "'weight'"),
])
def test_a_list_that_does_not_read_is_refused_saying_why(homes, text, said):
    path = mod._machine_projects_path()
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(mod.Refusal) as caught:
        mod._read_machine_projects()
    assert caught.value.code == "projects_invalid" and said in caught.value.message


def test_no_list_is_an_empty_list(homes):
    assert mod._read_machine_projects() == {}


def test_the_machine_settings_template_and_defaults(homes):
    template = _cli.CAPABILITY_DIR / "service" / "templates" / "machine.toml"
    assert mod._machine_template() == template
    # No file: the defaults, with no machine cap.
    assert mod._read_machine_settings() == {"version": 1, "max_parallel": None,
                                            "shutdown_grace_seconds": 60}
    absent = mod._machine_fingerprint()
    path = mod._machine_settings_path()
    assert path == homes / "config" / "tasks" / "service" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(template.read_text())
    assert mod._read_machine_settings() == {"version": 1, "max_parallel": 8,
                                            "shutdown_grace_seconds": 60}
    assert mod._machine_fingerprint() != absent
    for text, said in (("version = 1\npoll_seconds = 5\n", "'poll_seconds'"),
                       ("version = 1\nmax_parallel = 0\n", "`max_parallel`"),
                       ("version = 1\nshutdown_grace_seconds = -1\n", "`shutdown_grace_seconds`"),
                       ("max_parallel = 2\n", "`version` is 1")):
        path.write_text(text)
        with pytest.raises(mod.Refusal) as caught:
            mod._read_machine_settings()
        assert caught.value.code == "service_invalid" and said in caught.value.message


def test_init_machine_writes_the_template_and_keeps_an_edited_file(homes, capsys):
    target = mod._machine_settings_path()
    mod.cmd_service_init_machine([])
    first = json.loads(capsys.readouterr().out)
    template = mod._machine_template().read_text()
    assert first["written"] == [str(target)] and target.read_text() == template
    target.write_text("version = 1\nmax_parallel = 3\n")
    mod.cmd_service_init_machine([])
    assert json.loads(capsys.readouterr().out)["skipped"] == [str(target)]
    mod.cmd_service_init_machine(["--force"])
    capsys.readouterr()
    assert target.read_text() == template


# --- join and leave ----------------------------------------------------------

def write_project(root: Path, *, project_id: str | None, slug: str, worker: str, kind: str,
                  entry: dict, connection: str = "farm") -> Path:
    envelope = root / "capabilities"
    tasks = envelope / "tasks"
    (tasks / "workers").mkdir(parents=True)
    (tasks / "profiles").mkdir()
    (tasks / "profiles" / "plain.toml").write_text(
        base.PROFILE + f'cli_path = "{base.FAKE_CLAUDE}"\n')
    identity = {"schema": "capabilities.project.v1", "slug": slug}
    if project_id:
        identity["id"] = project_id
    (envelope / "project.json").write_text(json.dumps(identity))
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (tasks / "connections.json").write_text(json.dumps(
        {"default": connection, "connections": {connection: entry}}))
    base.write_worker(root, worker, f"takes: [{kind}]\nprofile: plain")
    base.write_worker(root, "default", "enabled: false")
    base.write_settings(root, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 2\n"
                              "retry_delay_seconds = 900\n")
    return root


def entry_for(schema: str, secret_env: str = "TASKS_TEST_PASSWORD", **over) -> dict:
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN or "postgresql://nobody@127.0.0.1:5432/none")
    entry = {"db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
             "db_user": info.get("user"), "db_name": info.get("dbname"),
             "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
             "secret_env": secret_env, "allow_write": True}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not None}


@pytest.fixture
def joining(homes, monkeypatch):
    """Projects resolved where they stand, the way the service tests resolve
    them, with no machine-level configuration but the test's own."""
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    for leaked in ("TASKS_EXECUTION", "CAPABILITIES_READ_ONLY", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_PROJECT_ENVELOPE",
                   "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CLAUDE_PROJECT_DIR",
                   "CAPABILITIES_STORE_MODE"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setenv("TASKS_ACTOR", "ops-person")
    return homes


def join(root: Path, capsys) -> tuple[int, dict]:
    """`tasks service join` in the project at `root`: its exit code and answer
    or error."""
    code = 0
    with mod.ProjectScope(root, dict(os.environ)):
        try:
            mod.cmd_service_join(None, [])
        except SystemExit as stopped:
            code = stopped.code
    said = capsys.readouterr()
    return code, (json.loads(said.out) if code == 0 else json.loads(said.err)["error"])


def test_join_refuses_in_order_and_answers_with_the_project(joining, capsys):
    root = joining / "pottery-shed"
    write_project(root, project_id=None, slug="kiln", worker="potter", kind="clay",
                  entry=entry_for("tasks_kiln", db_port=None))
    (root / ".env").write_text("TASKS_TEST_PASSWORD=from-the-project\n")
    identity = root / "capabilities" / "project.json"
    found = json.loads(identity.read_text())

    # Every refusal holds at once; each is reported in its turn as the one
    # before it is put right.
    code, error = join(root, capsys)
    assert (code, error["code"]) == (6, "no_project_identity")
    assert "capabilities init" in error["hint"]
    identity.write_text(json.dumps({**found, "id": "prj_k1ln00000001", "store": "db"}))
    code, error = join(root, capsys)
    assert (code, error["code"]) == (6, "database_mode_unsupported")
    identity.write_text(json.dumps({**found, "id": "prj_k1ln00000001"}))
    code, error = join(root, capsys)
    assert (code, error["code"]) == (6, "project_secret_set")
    assert "TASKS_TEST_PASSWORD" in error["message"] and "from-the-project" not in error["message"]
    (root / ".env").write_text("OTHER=1\n")
    code, error = join(root, capsys)
    assert (code, error["code"]) == (6, "connection_incomplete")
    assert "leaves db_port to the environment" in error["message"]
    connections = root / "capabilities" / "tasks" / "connections.json"
    connections.write_text(json.dumps({"default": "farm",
                                       "connections": {"farm": entry_for("tasks_kiln")}}))
    # Another project already joined under the same slug.
    other = joining / "kiln-copy"
    write_project(other, project_id="prj_k1ln00000002", slug="kiln", worker="potter",
                  kind="clay", entry=entry_for("tasks_kiln"))
    code, answer = join(other, capsys)
    assert code == 0 and answer["joined"] is True
    code, error = join(root, capsys)
    assert (code, error["code"]) == (6, "slug_taken")
    assert "prj_k1ln00000002" in error["message"]
    listed = mod._read_machine_projects()
    del listed["prj_k1ln00000002"]
    mod._write_machine_projects(listed)

    code, answer = join(root, capsys)
    assert code == 0
    assert answer == {"joined": True,
                      "project": {"id": "prj_k1ln00000001", "slug": "kiln",
                                  "root": str(root.resolve())},
                      "list": str(mod._machine_projects_path())}
    [(project, entry)] = mod._read_machine_projects().items()
    assert project == "prj_k1ln00000001"
    assert (entry["root"], entry["slug"], entry["joined_by"]) == (
        str(root.resolve()), "kiln", "ops-person")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["joined_at"])
    text = mod._machine_projects_path().read_text()
    assert f'[projects.prj_k1ln00000001]\nroot = "{root.resolve()}"\nslug = "kiln"\n' \
           f'joined_at = {entry["joined_at"]}\njoined_by = "ops-person"\n' in text
    # Joined already: an answer, not a refusal, and the list is unchanged.
    code, again = join(root, capsys)
    assert code == 0 and again["joined"] is False and again["reason"]
    assert again["project"]["id"] == "prj_k1ln00000001"
    assert mod._machine_projects_path().read_text() == text


def leave(root: Path | None, capsys, *args: str) -> tuple[int, dict]:
    code = 0
    scope = (mod.ProjectScope(root, dict(os.environ)) if root is not None
             else __import__("contextlib").nullcontext())
    with scope:
        try:
            mod.cmd_service_leave(list(args))
        except SystemExit as stopped:
            code = stopped.code
    said = capsys.readouterr()
    return code, (json.loads(said.out) if code == 0 else json.loads(said.err)["error"])


def test_leave_in_the_project_and_from_anywhere(joining, capsys):
    roots = {}
    for name, project, slug in (("orchard", ORCHARD_ID, "orchard"),
                                ("pottery-shed", "prj_k1ln00000001", "kiln"),
                                ("mill", "prj_m1ll00000001", "mill")):
        roots[slug] = write_project(joining / name, project_id=project, slug=slug,
                                    worker="w", kind=slug, entry=entry_for("tasks_x"))
        assert join(roots[slug], capsys)[1]["joined"] is True
    # In the project.
    code, answer = leave(roots["orchard"], capsys)
    assert code == 0 and answer["left"] is True
    assert answer["project"] == {"id": ORCHARD_ID, "slug": "orchard",
                                 "root": str(roots["orchard"].resolve())}
    assert set(mod._read_machine_projects()) == {"prj_k1ln00000001", "prj_m1ll00000001"}
    assert leave(roots["orchard"], capsys)[1]["left"] is False
    # From anywhere, by slug, the folder gone.
    roots["kiln"].rename(joining / "gone")
    code, answer = leave(None, capsys, "--machine", "--project", "kiln")
    assert code == 0 and answer["left"] is True and answer["project"]["id"] == "prj_k1ln00000001"
    # By id.
    code, answer = leave(None, capsys, "--machine", "--project", "prj_m1ll00000001")
    assert code == 0 and answer["left"] is True
    assert mod._read_machine_projects() == {}
    code, answer = leave(None, capsys, "--machine", "--project", "kiln")
    assert code == 0 and answer["left"] is False
    code, error = leave(None, capsys, "--machine")
    assert (code, error["code"]) == (6, "input")


# --- The machine process, in this process ------------------------------------

@pytest.fixture
def plot(joining, monkeypatch):
    """Two projects for a machine dispatcher run in this test's own process,
    standing in its machine state root."""
    if not DSN:
        pytest.skip("TASKS_TEST_DSN is unset")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    password = conninfo_to_dict(DSN).get("password") or ""
    monkeypatch.setenv("TASKS_TEST_PASSWORD", password)
    (joining / "config" / "tasks").mkdir(parents=True)
    mod.CREDENTIALS_ENV.write_text(f"TASKS_TEST_PASSWORD_KILN={password}\n")
    suffix = secrets.token_hex(3)
    schemas = [f"tasks_test_orchard_{suffix}", f"tasks_test_kiln_{suffix}"]
    roots = {
        "orchard": write_project(joining / "orchard", project_id=ORCHARD_ID, slug="orchard",
                                 worker="picker", kind="fruit", entry=entry_for(schemas[0])),
        "kiln": write_project(joining / "pottery-shed", project_id="prj_k1ln00000001",
                              slug="kiln", worker="potter", kind="clay",
                              entry=entry_for(schemas[1], "TASKS_TEST_PASSWORD_KILN")),
    }
    for name, root in roots.items():
        (root / ".env").write_text(f"{name.upper()}_ONLY=1\n")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for schema in schemas:
            conn.execute(mod._schema_ddl(schema))
    state = mod._machine_state_dir()
    state.mkdir(parents=True)
    monkeypatch.chdir(state)
    found = {"roots": roots, "schemas": schemas, "dispatcher": None}
    try:
        yield found
    finally:
        dispatcher = found["dispatcher"]
        if dispatcher is not None:
            dispatcher.stopping = True
            for slot in dispatcher.slots:
                for turn in list(slot.turns.values()):
                    if turn.process is not None:
                        turn.process.kill()
                        turn.process.wait()
                slot.turns.clear()
            dispatcher.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            for schema in schemas:
                conn.execute(f"drop schema if exists {schema} cascade")


def test_the_machine_process_resolves_no_secret_inside_a_scope_and_keeps_no_project_env(
        plot, capsys, monkeypatch):
    for root in plot["roots"].values():
        assert join(root, capsys)[1]["joined"] is True
    resolve = mod._resolve_env_key
    resolved = []
    tracking = {"on": True}

    def resolving(key):
        if tracking["on"] and key.startswith("TASKS_TEST_PASSWORD"):
            resolved.append((key, mod.ProjectScope.current is not None))
        return resolve(key)

    monkeypatch.setattr(mod, "_resolve_env_key", resolving)
    spawned = []
    service = mod._service_module()

    class Recording(service.subprocess.Popen):
        def __init__(self, *args, **kwargs):
            spawned.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(service.subprocess, "Popen", Recording)
    dispatcher = service.MachineDispatcher(mod._MachineHost(), tick=0.1)
    plot["dispatcher"] = dispatcher
    start = dict(dispatcher.environment)
    dispatcher.open()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (
            len(dispatcher.slots) == 2 and all(s.state() == "served" for s in dispatcher.slots)):
        dispatcher.step()
    assert sorted(slot.slug for slot in dispatcher.slots) == ["kiln", "orchard"]
    assert all(slot.state() == "served" for slot in dispatcher.slots)
    # Two distinct stores, one listener and one question connection each.
    assert len(dispatcher.stores()) == 2
    assert [store.connections_opened for store in dispatcher.stores()] == [2, 2]
    # A turn, so the start of one is seen too.
    slot = next(slot for slot in dispatcher.slots if slot.slug == "kiln")
    slot.host.turn_command = lambda worker: [sys.executable, "-c", "pass"]
    tracking["on"] = False  # the test's own write, not the machine process's
    with slot.scope():
        mod.cmd_add(slot.host.entry, ["--type", "clay", "--title", "a pot", "--status", "todo"])
    tracking["on"] = True
    capsys.readouterr()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not spawned:
        dispatcher.step()
    assert spawned and spawned[0]["close_fds"] is True
    assert Path(spawned[0]["cwd"]).resolve() == plot["roots"]["kiln"].resolve()
    assert "--connection" not in slot.host.turn_command("potter")
    # The secrets were resolved, and never inside a scope; the environment
    # the process and its turns start from holds nothing of either project.
    assert {key for key, _ in resolved} == {"TASKS_TEST_PASSWORD", "TASKS_TEST_PASSWORD_KILN"}
    assert not [key for key, scoped in resolved if scoped]
    assert dispatcher.environment == start
    assert not {"ORCHARD_ONLY", "KILN_ONLY", "CLAUDE_PROJECT_DIR"} & set(dispatcher.environment)
    assert not {"ORCHARD_ONLY", "KILN_ONLY"} & set(os.environ)


def test_a_machine_connection_is_never_opened_standing_in_a_project(plot, capsys, monkeypatch):
    root = plot["roots"]["orchard"]
    host = mod._service_host(root, None, dict(os.environ), machine=True)
    monkeypatch.chdir(root)
    with pytest.raises(mod.Refusal) as caught:
        host.open_query(5)
    assert caught.value.code == "machine_in_a_project"
    with host.scope(dict(os.environ)), pytest.raises(mod.Refusal):
        host.listen()


# --- The machine process, through the CLI -------------------------------------

class Farm:
    """Three projects and the homes a machine process and their CLIs share."""

    def __init__(self, tmp: Path):
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(DSN)
        self.tmp = tmp
        self.record = tmp / "engine.jsonl"
        suffix = secrets.token_hex(3)
        self.schemas = {"a": f"tasks_test_farm_a_{suffix}", "b": f"tasks_test_farm_b_{suffix}"}
        self.ids = {"orchard": str(uuid.uuid4()), "kiln": "prj_" + uuid.uuid4().hex[:12],
                    "mill": "prj_" + uuid.uuid4().hex[:12]}
        self.workers = {"orchard": ("picker", "fruit"), "kiln": ("potter", "clay"),
                        "mill": ("miller", "grain")}
        folders = {"orchard": "orchard", "kiln": "pottery-shed", "mill": "mill"}
        stores = {"orchard": ("a", "TASKS_TEST_PASSWORD"),
                  "kiln": ("b", "TASKS_TEST_PASSWORD_KILN"),
                  "mill": ("a", "TASKS_TEST_PASSWORD")}
        self.roots = {}
        for name in ("orchard", "kiln", "mill"):
            worker, kind = self.workers[name]
            schema, secret = stores[name]
            root = write_project(tmp / folders[name], project_id=self.ids[name], slug=name,
                                 worker=worker, kind=kind,
                                 entry=entry_for(self.schemas[schema], secret))
            (root / ".git").mkdir()
            (root / ".env").write_text(f"{name.upper()}_ONLY=1\n")
            self.roots[name] = root.resolve()
        config = tmp / "config"
        (config / "tasks").mkdir(parents=True)
        (config / "tasks" / "credentials.env").write_text(
            f"TASKS_TEST_PASSWORD_KILN={info.get('password') or ''}\n")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("CAPABILITIES_", "TASKS_", "CLAUDE_", "FAKE_"))}
        self.env.update({
            "HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(config),
            "XDG_STATE_HOME": str(tmp / "state"), "XDG_CACHE_HOME": str(tmp / "cache"),
            "CAPABILITIES_HOME": str(tmp / "registry"),
            "TASKS_TEST_PASSWORD": info.get("password") or "",
            "TASKS_ACTOR": "ops-person",
            "FAKE_ENGINE_TASKS": str(_cli.CLI_PATH), "FAKE_ENGINE_RECORD": str(self.record),
            "FAKE_ENGINE_SLEEP": "1",
        })
        self.machine_state = tmp / "state" / "capabilities" / "machine" / "tasks"

    def state(self, name: str) -> Path:
        return self.tmp / "state" / "capabilities" / "projects" / name / "tasks"

    def cli(self, name: str | None, *args: str, **env) -> subprocess.CompletedProcess:
        """A tasks command in the project `name`, or outside every project."""
        run_env = {**self.env, **env}
        if name is not None:
            root = self.roots[name]
            run_env.update(CLAUDE_PROJECT_DIR=str(root),
                           CAPABILITIES_PROJECT_ENVELOPE=str(root / "capabilities"),
                           CAPABILITIES_PROJECT_ENVELOPE_ROOT=str(root))
        return subprocess.run([str(_cli.CLI_PATH), *args],
                              cwd=self.roots[name] if name else self.tmp, env=run_env,
                              text=True, capture_output=True, timeout=180)

    def ok(self, name, *args, **env) -> dict:
        return base.answer_of(self.cli(name, *args, **env))

    def refused(self, name, *args, **env) -> tuple[int, dict]:
        done = self.cli(name, *args, **env)
        assert done.returncode != 0, done.stdout
        return done.returncode, json.loads(done.stderr.strip().splitlines()[-1])["error"]

    def join(self, *names: str) -> None:
        for name in names:
            assert self.ok(name, "service", "join")["joined"] is True

    def machine(self) -> dict:
        return self.ok(None, "service", "status", "--machine")

    def entry(self, name: str) -> dict:
        return next(row for row in self.machine()["projects"] if row["project"] == name)

    def until(self, name: str, state: str, seconds: float = 30) -> dict:
        return poll_for(lambda: (lambda row: row if row.get("state") == state else None)(
            self.entry(name)), seconds)

    def start(self, **env) -> int:
        started = self.ok(None, "service", "start", "--machine", **env)
        assert started["running"] is True
        return started["pid"]

    def add(self, name: str, key: str, kind: str | None = None) -> None:
        self.ok(name, "add", "--type", kind or self.workers[name][1], "--title", key,
                "--key", key, "--status", "todo")

    def status_of(self, name: str, key: str) -> str:
        return self.ok(name, "show", key)["task"]["status"]

    def complete(self, name: str, key: str, seconds: float = 60) -> None:
        poll_for(lambda: self.status_of(name, key) == "complete", seconds)

    def log(self) -> list[str]:
        path = self.machine_state / "daemon.log"
        return path.read_text().splitlines() if path.is_file() else []

    def turns_seen(self) -> list[dict]:
        if not self.record.is_file():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line]


@pytest.fixture
def farm(tmp_path):
    import psycopg

    if not DSN:
        pytest.skip("TASKS_TEST_DSN is unset")
    found = Farm(tmp_path)
    with psycopg.connect(DSN, autocommit=True) as conn:
        for schema in found.schemas.values():
            conn.execute(mod._schema_ddl(schema))
    try:
        yield found
    finally:
        found.cli(None, "service", "stop", "--machine", "--end-turns", "--timeout", "20",
                  "--force")
        for name in found.roots:
            if found.roots[name].is_dir():
                found.cli(name, "service", "stop", "--end-turns", "--timeout", "20", "--force")
        with psycopg.connect(DSN, autocommit=True) as conn:
            for schema in found.schemas.values():
                conn.execute(f"drop schema if exists {schema} cascade")


def started_in(farm: Farm) -> list[str]:
    """The project of every turn the machine log says started, in order."""
    return [match.group(1) for line in farm.log()
            for match in [re.search(r" tasks service \[([a-z]+)\]: turn \w+ started:", line)]
            if match]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@needs_store
def test_the_gate_asks_join_for_an_explicit_enable_and_the_read_only_switch_refuses(farm):
    """`join` is a project act: enabled globally is not opting a project in, and
    under the read-only switch neither `join` nor `leave` writes the list."""
    envelope = farm.roots["mill"] / "capabilities"
    (envelope / "settings.json").write_text(json.dumps({"capabilities": {}}))
    global_gate = farm.tmp / "config" / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True, exist_ok=True)
    global_gate.write_text(json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    code, error = farm.refused("mill", "service", "join")
    assert (code, error["code"]) == (4, "project_enable_required")
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    code, error = farm.refused("mill", "service", "join", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")
    assert not (farm.tmp / "config" / "tasks" / "service" / "projects.toml").exists()
    farm.join("mill")
    for args in (("mill", "service", "leave"),
                 (None, "service", "leave", "--machine", "--project", "mill")):
        code, error = farm.refused(*args, CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
    # Outside every project, with tasks enabled nowhere, the machine verbs pass.
    global_gate.write_text(json.dumps({"capabilities": {}}))
    assert farm.machine()["projects_file"]["joined"] == 1
    left = farm.ok(None, "service", "leave", "--machine", "--project", "mill")
    assert left["left"] is True


@needs_store
def test_one_machine_process_serves_three_projects_each_as_itself(farm):
    farm.join("orchard", "kiln", "mill")
    pid = farm.start()
    for name in ("orchard", "kiln", "mill"):
        farm.until(name, "served")
    status = farm.machine()
    assert (status["mode"], status["running"], status["pid"], status["current"]) == (
        "machine", True, pid, True)
    assert status["projects_file"]["joined"] == 3
    assert sorted(row["project"] for row in status["projects"]) == ["kiln", "mill", "orchard"]
    by_name = {row["project"]: row for row in status["projects"]}
    assert by_name["orchard"]["project_id"] == farm.ids["orchard"]
    uuid.UUID(by_name["orchard"]["project_id"])
    assert by_name["kiln"]["root"] == str(farm.roots["kiln"]) and \
        farm.roots["kiln"].name == "pottery-shed"
    assert all(row["present"] and row["enabled_explicitly"] and row["current"]
               for row in status["projects"])
    # Two distinct stores: one listener and one question connection each.
    assert sorted((store["listening"], store["connections_opened"])
                  for store in status["stores"]) == [(True, 2), (True, 2)]
    assert sorted(sorted(store["projects"]) for store in status["stores"]) == [
        ["kiln"], ["mill", "orchard"]]

    for name in ("orchard", "kiln", "mill"):
        farm.add(name, f"t-{name}")
    # A type only another project's worker takes: nobody here takes it.
    farm.add("orchard", "t-orchard-clay", kind="clay")
    for name in ("orchard", "kiln", "mill"):
        farm.complete(name, f"t-{name}")
    time.sleep(2)
    assert farm.status_of("orchard", "t-orchard-clay") == "todo"
    seen = farm.turns_seen()
    assert len(seen) == 3
    for name in ("orchard", "kiln", "mill"):
        worker = farm.workers[name][0]
        [turn] = [turn for turn in seen if f"the worker `{worker}`" in turn["prompt"]]
        assert Path(turn["cwd"]).resolve() == farm.roots[name]
        # The turn read its own project's env files, and nothing of another's:
        # the environment every turn starts from holds no project's.
        own = {f"{other.upper()}_ONLY" for other in ("orchard", "kiln", "mill")} & set(turn["env"])
        assert own == {f"{name.upper()}_ONLY"}
        assert "CLAUDE_PROJECT_DIR" not in turn["env"] or \
            Path(turn["env"]["CLAUDE_PROJECT_DIR"]).resolve() == farm.roots[name]
    # Still the same connections, after turns started and ended.
    status = farm.machine()
    assert sorted(store["connections_opened"] for store in status["stores"]) == [2, 2]
    # The log: one line per thing, each naming its project, and each project's
    # lines in its own daemon.log too.
    lines = farm.log()
    assert all(re.search(r" tasks service \[(machine|orchard|kiln|mill)\]: ", line)
               for line in lines), lines
    assert any(" tasks service [machine]: started" in line for line in lines)
    for name in ("orchard", "kiln", "mill"):
        own = (farm.state(name) / "daemon.log").read_text().splitlines()
        assert own and all(f" tasks service [{name}]: " in line for line in own)
        assert any("turn " in line and " started: " in line for line in own)
        published = json.loads((farm.state(name) / "daemon.json").read_text())
        assert (published["mode"], published["pid"]) == ("machine", pid)
        assert (farm.state(name) / "daemon.pid").read_text().strip() == str(pid)
    filtered = farm.ok(None, "service", "logs", "--machine", "--project", "kiln",
                       "--tail", "500")["lines"]
    assert filtered and all(" tasks service [kiln]: " in line for line in filtered)
    assert len(filtered) < len(lines)

    # A notification for one project's schema wakes that slot alone.
    before = {row["project"]: row["last_wake"] for row in farm.machine()["projects"]}
    farm.add("mill", "t-mill-2")
    farm.complete("mill", "t-mill-2")
    after = {row["project"]: row["last_wake"] for row in farm.machine()["projects"]}
    assert after["orchard"] == before["orchard"] and after["kiln"] == before["kiln"]
    assert after["mill"] != before["mill"]
    assert any("notify" in line or "claimed t-mill-2" in line
               for line in farm.ok("mill", "service", "logs", "--tail", "50")["lines"])


@needs_store
def test_the_machine_cap_deals_turns_round_the_projects_in_turn(farm):
    settings = farm.tmp / "config" / "tasks" / "service" / "config.toml"
    assert farm.ok(None, "service", "init", "--machine")["written"] == [str(settings)]
    settings.write_text("version = 1\nmax_parallel = 1\nshutdown_grace_seconds = 5\n")
    farm.join("orchard", "kiln", "mill")
    for name in ("orchard", "kiln", "mill"):
        farm.add(name, f"t-{name}-1")
        farm.add(name, f"t-{name}-2")
    farm.start(FAKE_ENGINE_SLEEP="2")
    binding = poll_for(lambda: (lambda s: s if s["cap"]["binding"] else None)(farm.machine()), 60)
    assert binding["cap"]["max_parallel"] == 1 and binding["cap"]["running"] == 1
    assert any(row.get("waiting_for_cap") for row in binding["projects"])
    for name in ("orchard", "kiln", "mill"):
        for n in (1, 2):
            farm.complete(name, f"t-{name}-{n}", 180)
    order = started_in(farm)
    assert len(order) == 6, order
    assert sorted(order[:3]) == ["kiln", "mill", "orchard"] and order[3:] == order[:3], order
    # Never two at once: every start after the first comes after the end before it.
    events = [("start" if " started: " in line else "end", line) for line in farm.log()
              if re.search(r"\]: turn \w+ (started|ended): ", line)]
    running = peak = 0
    for kind, _line in events:
        running += 1 if kind == "start" else -1
        peak = max(peak, running)
    assert peak == 1, events

    # No machine cap: only each project's own limits.
    settings.write_text("version = 1\nshutdown_grace_seconds = 5\n")
    reloaded = farm.ok(None, "service", "reload", "--machine")
    assert reloaded["reloaded"] is True and reloaded["changed"] is True
    assert farm.machine()["cap"]["max_parallel"] is None
    for name in ("orchard", "kiln", "mill"):
        farm.add(name, f"t-{name}-3")
    together = poll_for(lambda: (lambda s: s if s["cap"]["running"] == 3 else None)(
        farm.machine()), 30)
    assert together["cap"]["binding"] is False


@needs_store
def test_a_project_that_does_not_load_is_error_and_the_others_are_served(farm):
    farm.join("orchard", "kiln", "mill")
    miller = farm.roots["mill"] / "capabilities" / "tasks" / "workers" / "miller.md"
    good = miller.read_text()
    miller.write_text("---\ntakes: [grain\nprofile: plain\n---\n\nbroken.\n")
    for name in ("orchard", "kiln", "mill"):
        farm.add(name, f"t-{name}")
    farm.start()
    broken = farm.until("mill", "error")
    assert broken["reason"].startswith("worker_invalid") and "miller" in broken["reason"]
    farm.complete("orchard", "t-orchard")
    farm.complete("kiln", "t-kiln")
    assert farm.status_of("mill", "t-mill") == "todo"
    # The machine process is healthy; the broken project's own doctor is not.
    assert farm.ok(None, "service", "doctor", "--machine")["ok"] is True
    code, _error = farm.cli("mill", "service", "doctor").returncode, None
    assert code == 6
    doctor = json.loads(farm.cli("mill", "service", "doctor").stdout)
    assert any("does not serve this project: error" in p for p in doctor["problems"])
    assert farm.ok("orchard", "service", "doctor")["ok"] is True
    # Repaired and reloaded from the project: served, and its work is taken.
    miller.write_text(good)
    assert farm.ok("mill", "service", "reload")["reloaded"] is True
    farm.until("mill", "served")
    farm.complete("mill", "t-mill")

    # One project paused alone.
    farm.ok("orchard", "service", "pause", "--reason", "holding the orchard")
    farm.until("orchard", "paused")
    farm.add("orchard", "t-orchard-held")
    farm.add("kiln", "t-kiln-2")
    farm.complete("kiln", "t-kiln-2")
    assert farm.status_of("orchard", "t-orchard-held") == "todo"
    assert {row["project"]: row["state"] for row in farm.machine()["projects"]} == {
        "orchard": "paused", "kiln": "served", "mill": "served"}
    farm.ok("orchard", "service", "resume")
    farm.complete("orchard", "t-orchard-held")


@needs_store
def test_joining_and_leaving_are_followed_without_a_reload(farm):
    for name in ("orchard", "mill"):
        base.write_settings(farm.roots[name], "version = 1\npoll_seconds = 2\n"
                                              "retry_delay_seconds = 900\n")
    farm.join("orchard", "kiln")
    pid = farm.start()
    farm.until("orchard", "served")
    farm.until("kiln", "served")
    # Joined while it runs: served from a pass, with no reload.
    farm.join("mill")
    farm.until("mill", "served", 15)
    farm.add("mill", "t-mill")
    farm.complete("mill", "t-mill")
    assert farm.machine()["pid"] == pid

    # Leaving lets the project go at once: its lock, pid and status.
    assert farm.ok("kiln", "service", "leave")["left"] is True
    poll_for(lambda: "kiln" not in [row["project"] for row in farm.machine()["projects"]]
             and not (farm.state("kiln") / "daemon.pid").exists(), 15)
    with (farm.state("kiln") / "daemon.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert any("[kiln]: left the machine service's opt-in list" in line for line in farm.log())
    assert farm.ok("kiln", "service", "status").get("mode") is None

    # `capabilities disable tasks --project` writes this: the list keeps the
    # project, and the machine service reports it refused.
    settings = farm.roots["mill"] / "capabilities" / "settings.json"
    settings.write_text(json.dumps({"capabilities": {"tasks": {"enabled": False}}}))
    refused = farm.until("mill", "refused")
    assert "project_enable_required" in refused["reason"]
    assert refused["enabled_explicitly"] is False and refused["present"] is True
    listed = farm.tmp / "config" / "tasks" / "service" / "projects.toml"
    assert farm.ids["mill"] in listed.read_text()
    settings.write_text(json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    farm.until("mill", "served")

    # A folder renamed away is an error; back, it is served again.
    moved = farm.tmp / "orchard-moved"
    farm.roots["orchard"].rename(moved)
    gone = farm.until("orchard", "error")
    assert "is gone" in gone["reason"] and gone["present"] is False
    moved.rename(farm.roots["orchard"])
    farm.until("orchard", "served")
    # Leaving from anywhere, by slug, with the folder gone.
    farm.roots["orchard"].rename(moved)
    left = farm.ok(None, "service", "leave", "--machine", "--project", "orchard")
    assert left["left"] is True and left["project"]["id"] == farm.ids["orchard"]
    poll_for(lambda: [row["project"] for row in farm.machine()["projects"]] == ["mill"], 15)
    moved.rename(farm.roots["orchard"])


def working(farm: Farm, name: str, key: str, *, machine: bool = True) -> dict:
    """The turn row once a turn holds `key`."""
    def found():
        rows = (farm.entry(name).get("turns") if machine
                else farm.ok(name, "service", "status").get("turns")) or []
        return next((row for row in rows if row["task"] == key and row["phase"] == "working"),
                    None)
    return poll_for(found, 60)


@needs_store
def test_a_project_mode_daemon_keeps_its_project_until_it_exits(farm):
    held = {"FAKE_ENGINE_SLEEP": "15"}
    farm.ok("orchard", "service", "start", **held)
    farm.add("orchard", "t-long")
    row = working(farm, "orchard", "t-long", machine=False)
    project_pid = farm.ok("orchard", "service", "status")["pid"]
    farm.join("orchard", "kiln")
    farm.start(**held)
    refused = farm.until("orchard", "refused")
    assert refused["reason"] == (f"a project-mode daemon, pid {project_pid}, serves "
                                 "this project; stop it there")
    farm.until("kiln", "served")
    # Joined: start and run are refused before they try the lock.
    for verb in ("start", "run"):
        code, error = farm.refused("orchard", "service", verb)
        assert (code, error["code"]) == (4, "joined_to_machine_service")
        assert "tasks service leave" in error["hint"]
    # The project-mode daemon is stopped where it runs; its turn is left.
    assert farm.ok("orchard", "service", "stop")["stopped"] is True
    assert alive(row["pid"])
    farm.until("orchard", "served", 15)
    [adopted] = farm.entry("orchard")["turns"]
    assert (adopted["id"], adopted["pid"], adopted["adopted"], adopted["task"]) == (
        row["id"], row["pid"], True, "t-long")
    assert any(f"[orchard]: turn {row['id']} adopted" in line for line in farm.log())
    # Now the machine process holds it: a stop here is refused.
    code, error = farm.refused("orchard", "service", "stop")
    assert (code, error["code"]) == (4, "joined_to_machine_service")
    assert "tasks service pause" in error["hint"] and "stop --machine" in error["hint"]
    farm.complete("orchard", "t-long", 90)


@needs_store
def test_turns_outlive_the_machine_process_and_move_between_modes(farm):
    settings = farm.tmp / "config" / "tasks" / "service" / "config.toml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("version = 1\nshutdown_grace_seconds = 2\n")
    farm.join("orchard", "kiln")
    env = {**farm.env, "FAKE_ENGINE_SLEEP": "15"}
    out = (farm.tmp / "machine-run.log").open("w")
    # As a supervisor runs it: the executable under its `uv run --script` shebang.
    wrapper = subprocess.Popen([str(_cli.CLI_PATH), "service", "run", "--machine"],
                               cwd=farm.tmp, env=env, stdin=subprocess.DEVNULL, stdout=out,
                               stderr=out, start_new_session=True)
    try:
        machine = poll_for(lambda: (lambda s: s if s["running"] else None)(farm.machine()), 60)
        assert machine["pid"] != wrapper.pid
        said = subprocess.run(["ps", "-o", "command=", "-p", str(wrapper.pid)],
                              capture_output=True, text=True).stdout
        assert said.startswith("uv run --script"), said
        farm.until("orchard", "served")
        farm.add("orchard", "t-kept")
        row = working(farm, "orchard", "t-kept")
        os.kill(wrapper.pid, signal.SIGTERM)
        poll_for(lambda: not alive(machine["pid"]), 10)
        assert wrapper.wait(timeout=10) == 0
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
        out.close()
    assert alive(row["pid"]) and farm.status_of("orchard", "t-kept") == "in_progress"
    assert farm.machine()["running"] is False
    # The next machine process adopts it.
    farm.start(FAKE_ENGINE_SLEEP="15")
    farm.until("orchard", "served")
    [adopted] = farm.entry("orchard")["turns"]
    assert (adopted["id"], adopted["pid"], adopted["adopted"]) == (row["id"], row["pid"], True)
    farm.complete("orchard", "t-kept", 90)

    # `stop --machine --end-turns` ends them after the machine's grace.
    farm.add("kiln", "t-ended")
    ended = working(farm, "kiln", "t-ended")
    stopped = farm.ok(None, "service", "stop", "--machine", "--end-turns")
    assert stopped["stopped"] is True and stopped["end_turns"]["by"] == "ops-person"
    assert 2 <= stopped["waited_seconds"] < 20
    assert not alive(ended["pid"])
    [raised] = farm.ok("kiln", "runs", "t-ended")["executions"]
    assert raised["status"] == "abandoned" and farm.status_of("kiln", "t-ended") == "todo"
    assert any(f"[kiln]: turn {ended['id']} cut off after 2s" in line for line in farm.log())
    assert not (farm.machine_state / "end-turns.json").exists()

    # Leaving with a turn running, the project's own daemon adopts it.
    farm.start(FAKE_ENGINE_SLEEP="15")
    farm.until("kiln", "served")
    farm.add("orchard", "t-moved")
    moved = working(farm, "orchard", "t-moved")
    assert farm.ok("orchard", "service", "leave")["left"] is True
    poll_for(lambda: not (farm.state("orchard") / "daemon.pid").exists(), 15)
    assert alive(moved["pid"])
    farm.ok("orchard", "service", "start")
    [taken] = farm.ok("orchard", "service", "status")["turns"]
    assert (taken["id"], taken["pid"], taken["adopted"]) == (moved["id"], moved["pid"], True)
    farm.complete("orchard", "t-moved", 90)


@needs_store
def test_machine_status_and_doctor_with_and_without_a_process(farm):
    farm.join("orchard", "kiln", "mill")
    (farm.roots["mill"] / "capabilities" / "tasks" / "workers" / "miller.md").write_text(
        "---\ntakes: grain: [\n---\n")
    status = farm.machine()
    assert (status["running"], status["pid"], status["mode"]) == (False, None, "machine")
    assert status["projects_file"] == {"path": str(farm.tmp / "config" / "tasks" / "service"
                                                    / "projects.toml"), "joined": 3}
    assert [(row["project"], row["present"], row["enabled_explicitly"], row["state"])
            for row in status["projects"]] == [("kiln", True, True, None),
                                               ("mill", True, True, None),
                                               ("orchard", True, True, None)]
    code, _error = farm.cli(None, "service", "doctor", "--machine").returncode, None
    assert code == 6
    pid = farm.start()
    farm.until("orchard", "served")
    farm.until("kiln", "served")
    farm.until("mill", "error")
    status = farm.machine()
    for key in ("mode", "running", "pid", "started_at", "fingerprint", "current",
                "projects_file", "cap", "stores", "projects"):
        assert key in status, key
    assert (status["running"], status["pid"], status["current"]) == (True, pid, True)
    assert status["cap"] == {"max_parallel": None, "running": 0, "binding": False}
    served = farm.entry("orchard")
    for key in ("project", "project_id", "root", "joined_at", "present", "enabled_explicitly",
                "state", "reason", "fingerprint", "current", "reload_error", "lanes", "pause",
                "turns", "next_wake"):
        assert key in served, key
    assert served["lanes"] == [{"project": "orchard", "worker": "picker", "max_parallel": 1,
                                "running": 0, "held_until_poll": False}]
    # Healthy with one project in error.
    doctor = farm.ok(None, "service", "doctor", "--machine")
    assert doctor["ok"] is True
    assert {row["project"]: row["state"] for row in doctor["projects"]}["mill"] == "error"
    # The project's own status speaks of the machine process.
    own = farm.ok("orchard", "service", "status")
    assert (own["mode"], own["running"], own["pid"], own["state"]) == ("machine", True, pid,
                                                                       "served")
    assert own["machine"]["running"] is True and own["machine"]["state"] == "served"
    assert farm.ok("orchard", "service", "doctor")["ok"] is True
    # Stale machine settings fail it until a reload.
    settings = farm.tmp / "config" / "tasks" / "service" / "config.toml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("version = 1\nmax_parallel = 4\n")
    stale = farm.cli(None, "service", "doctor", "--machine")
    assert stale.returncode == 6 and "changed since" in " ".join(json.loads(stale.stdout)["problems"])
    assert farm.ok(None, "service", "reload", "--machine")["reloaded"] is True
    assert farm.ok(None, "service", "doctor", "--machine")["ok"] is True
    assert farm.machine()["cap"]["max_parallel"] == 4
    settings.write_text("version = 1\nbogus = 1\n")
    code, error = farm.refused(None, "service", "reload", "--machine")
    assert (code, error["code"]) == (6, "service_invalid")
    settings.write_text("version = 1\nmax_parallel = 4\n")
    # Not running: non-zero.
    farm.ok(None, "service", "stop", "--machine")
    assert farm.cli(None, "service", "doctor", "--machine").returncode == 6
    assert farm.machine()["running"] is False
