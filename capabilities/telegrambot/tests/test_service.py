"""The service end to end on a real PostgreSQL and a fake Bot API: the CLI's host
admits a project, the engine polls each bot, a message becomes a turn whose
answer is sent, the offset moves only past what was handled, one process
serves two projects in machine mode, and a turn's job is run and delivered.
The harness runner is a fake.

Needs TELEGRAMBOT_TEST_DSN; skipped without it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

from _agents import register_agents, register_nodes
from _cli import CAPABILITY_DIR, load_cli, load_service

DSN = os.environ.get("TELEGRAMBOT_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TELEGRAMBOT_TEST_DSN is not set")


class FakeTelegram:
    """The Bot API of several bots, by token."""

    def __init__(self):
        self.queues: dict[str, list] = {}
        self.offsets: dict[str, list] = {}
        self.sent: list[dict] = []
        self.lock = threading.Lock()

    def add_bot(self, token):
        self.queues[token] = []
        self.offsets[token] = []

    def push(self, token, update):
        with self.lock:
            self.queues[token].append(update)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        token, method = path.split("/")[1][3:], path.rsplit("/", 1)[1]
        body = json.loads(request.content) if request.content else {}
        if token not in self.queues:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        bot_id = int(token.split(":")[0])
        ok = lambda result: httpx.Response(200, json={"ok": True, "result": result})  # noqa
        if method == "getMe":
            return ok({"id": bot_id, "is_bot": True, "first_name": f"Bot{bot_id}",
                       "username": f"bot{bot_id}", "can_read_all_group_messages": False})
        if method == "getWebhookInfo":
            return ok({"url": ""})
        if method in ("setMyCommands", "sendChatAction", "setMessageReaction"):
            return ok(True)
        if method == "getUpdates":
            offset = body.get("offset")
            with self.lock:
                self.offsets[token].append(offset)
                queue = self.queues[token]
                if offset is not None:
                    queue[:] = [u for u in queue if u["update_id"] >= offset]
                return ok(list(queue))
        if method == "sendMessage":
            with self.lock:
                self.sent.append({"token": token, **body})
                return ok({"message_id": 5000 + len(self.sent)})
        return httpx.Response(404, json={"ok": False, "description": "Not Found: " + method})


@pytest.fixture()
def telegram(monkeypatch):
    fake = FakeTelegram()
    real = httpx.Client

    class Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    return fake


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for key, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                     ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(key, str(home / sub))
    monkeypatch.setenv("HOME", str(home))
    for key in list(os.environ):
        if key.startswith(("AGENTKIT_DB_", "TELEGRAMBOT_", "CAPABILITIES_PROJECT_",
                           "CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR", "AGENT_ENCRYPTION")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_NODE_ID", "0123456789ab")
    return home


def make_project(tmp_path, name, token, settings, schema):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    caps = root / "capabilities"
    (caps / "telegrambot" / "service").mkdir(parents=True)
    (caps / "settings.json").write_text(json.dumps({"capabilities": {"telegrambot": {"enabled": True}}}))
    (caps / "project.json").write_text(json.dumps({"slug": name}))
    (caps / "telegrambot" / "connections.json").write_text(json.dumps({"connections": {
        "bot": {"secret_env": "TELEGRAMBOT_BOT_BOT_TOKEN", "allow_write": True}}}))
    (caps / "telegrambot" / "service" / "settings.json").write_text(json.dumps(settings))
    project_id = f"prj_{uuid.uuid4().hex[:12]}"
    (root / ".env.local").write_text(
        f"CAPABILITIES_PROJECT_ID={project_id}\nTELEGRAMBOT_BOT_BOT_TOKEN={token}\n"
        f"AGENTKIT_DB_URL={DSN}\nAGENTKIT_DB_SCHEMA={schema}\n")
    migrate_schema(schema, project_id)
    return root, project_id


def _setting(schema):
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(DSN)
    return db.Setting(schema=schema, level="machine", sources=("t",),
                      host=info.get("host"), port=int(info.get("port") or 5432),
                      database=info.get("dbname"), user=info.get("user"),
                      password=info.get("password"), sslmode="disable")


def migrate_schema(schema, project_id):
    """Bring a project's tables up before its service starts, as `telegrambot
    migrate` does where the payload is installed. The shared database library
    judges a migration step by a snapshot of the whole database, so a step that
    runs while another schema is being created is refused (naming_law) and the
    service retries its admission later; migrating first keeps serving free of
    DDL and these cases free of that timing."""
    store = load_service("store")
    opened = store.Store(_setting(schema)).open()
    # The project's agent and the node it runs on, which the manager registers
    # before its service runs.
    from capabilities_contract.capability.store import node_id
    register_agents(opened, project_id)
    register_nodes(opened, node_id())
    opened.close()


def drop_schema(schema):
    from capabilities_contract import db
    setting = _setting(schema)
    with db.connect(application_name="t", setting=setting) as conn:
        conn.autocommit = True
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


class Result:
    def __init__(self, answer):
        self.ok, self.answer, self.session_id, self.failure = True, answer, "sess-1", None


class Runner:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, prompt, profile, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
        found = self.answer(prompt, {**kwargs, "cwd": cwd}) if callable(self.answer) \
            else self.answer
        if kwargs.get("on_start"):
            kwargs["on_start"](type("Started", (), {"pid": None, "session_id": "sess-1",
                                                    "harness": "claude"})())
        return Result(found)


def serve(cli, host, mode, state):
    engine = cli._engine_for(host, mode, state)
    engine.fast = True
    engine.echo = False
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    return engine, thread


def wait(condition, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def logs(cli) -> str:
    out = []
    for path in sorted(Path(os.environ["XDG_STATE_HOME"]).rglob("daemon.log")):
        out.append(f"== {path}\n{path.read_text()}")
    return "\n".join(out)


def settle(condition, cli, what, seconds=20.0):
    if not wait(condition, seconds):
        pytest.fail(f"{what} did not happen within {seconds:g}s\n{logs(cli)}", pytrace=False)


def message(update_id, chat, sender, text):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": int(time.time()), "text": text,
        "from": {"id": sender, "is_bot": False, "first_name": "Ann"},
        "chat": {"id": chat, "type": "private"}}}


def host_with(cli, runner, mode, entry=None):
    class Host(cli._Host):
        def run_override(self):
            return runner
    return Host(mode, entry)


def test_one_project_is_served_and_the_offset_follows_what_was_handled(tmp_path, home,
                                                                       telegram, monkeypatch):
    schema = f"tgb_svc_{uuid.uuid4().hex[:8]}"
    token = "111:aaa"
    telegram.add_bot(token)
    root, project_id = make_project(tmp_path, "alpha", token,
                                    {"allowed_users": {"42": {"name": "Ann",
                                                              "role": "supervisor"}},
                                     "defaults": {"debounce": 0}}, schema)
    monkeypatch.chdir(root)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    cli = load_cli()
    runner = Runner("=== REPLY ===\nhi Ann")
    entry = {"id": project_id, "root": str(root), "slug": "alpha"}
    engine, thread = serve(cli, host_with(cli, runner, "project", entry), "project",
                           cli._project_state_dir("alpha"))
    try:
        telegram.push(token, message(10, 42, 42, "hello"))
        telegram.push(token, message(11, 43, 43, "a stranger"))
        settle(lambda: telegram.sent, cli, 'lambda: telegram.sent')
        assert telegram.sent[0]["chat_id"] == 42 and telegram.sent[0]["text"] == "hi Ann"
        settle(lambda: 12 in telegram.offsets[token], cli, 'lambda: 12 in telegram.offsets[token]')
        assert len(runner.calls) == 1
        assert engine.status()["projects"][0]["state"] == "served"
        # The project's lock keeps a second process (either mode) off it.
        other = cli._engine_for(host_with(cli, runner, "project", entry), "project",
                                tmp_path / "elsewhere")
        second = type(engine.slots[project_id])(other, project_id, entry)
        assert second.take_lock() is False
        # /reload takes up changed settings, and says so in the chat.
        before = engine.slots[project_id].plan["fingerprint"]
        settings = root / "capabilities" / "telegrambot" / "service" / "settings.json"
        settings.write_text(json.dumps({"allowed_users": {"42": {"name": "Ann",
                                                                 "role": "supervisor"}},
                                        "defaults": {"debounce": 0, "tail_size": 12}}))
        telegram.push(token, message(12, 42, 42, "/reload"))
        settle(lambda: any(s["text"] == "ok, settings reloaded" for s in telegram.sent), cli, 'lambda: any(s["text"] == "ok, settings reloaded" for s in telegram.sent)')
        assert engine.slots[project_id].plan["fingerprint"] != before
    finally:
        engine.stopping.set()
        engine.wake.set()
        thread.join(30)
        drop_schema(schema)


def test_one_machine_process_serves_two_projects(tmp_path, home, telegram, monkeypatch):
    schema_a, schema_b = (f"tgb_m_{uuid.uuid4().hex[:8]}" for _ in range(2))
    telegram.add_bot("222:bbb")
    telegram.add_bot("333:ccc")
    root_a, id_a = make_project(tmp_path, "one", "222:bbb",
                                {"direct_messages": {"mode": "anyone"},
                                 "defaults": {"debounce": 0}}, schema_a)
    root_b, id_b = make_project(tmp_path, "two", "333:ccc",
                                {"direct_messages": {"mode": "anyone"},
                                 "defaults": {"debounce": 0}}, schema_b)
    cli = load_cli()
    listed = cli._machine_projects_path()
    listed.parent.mkdir(parents=True)
    listed.write_text(json.dumps({"version": 1, "projects": {
        id_a: {"root": str(root_a), "slug": "one", "joined_at": "now", "joined_by": "test"},
        id_b: {"root": str(root_b), "slug": "two", "joined_at": "now", "joined_by": "test"}}}))
    runner = Runner(lambda prompt, kw: "=== REPLY ===\nfrom " + kw["cwd"].rsplit("/", 1)[1])
    monkeypatch.chdir(tmp_path)
    engine, thread = serve(cli, host_with(cli, runner, "machine"), "machine",
                           cli._machine_state_dir())
    try:
        telegram.push("222:bbb", message(1, 50, 50, "to one"))
        telegram.push("333:ccc", message(1, 60, 60, "to two"))
        settle(lambda: len(telegram.sent) == 2, cli, "both bots answering")
        by_token = {s["token"]: s["text"] for s in telegram.sent}
        assert by_token == {"222:bbb": "from one", "333:ccc": "from two"}, logs(cli)
        rows = {p["project"]: p["state"] for p in engine.status()["projects"]}
        assert rows == {"one": "served", "two": "served"}
        # A project that leaves the list is released.
        listed.write_text(json.dumps({"version": 1, "projects": {
            id_a: {"root": str(root_a), "slug": "one", "joined_at": "now",
                   "joined_by": "test"}}}))
        settle(lambda: [p["project"] for p in engine.status()["projects"]] == ["one"], cli, 'lambda: [p["project"] for p in engine.status()["projects"]] == ["one"]')
    finally:
        engine.stopping.set()
        engine.wake.set()
        thread.join(30)
        drop_schema(schema_a)
        drop_schema(schema_b)


def test_a_turn_hands_work_to_a_job_that_is_delivered(tmp_path, home, telegram, monkeypatch):
    schema = f"tgb_job_{uuid.uuid4().hex[:8]}"
    token = "444:ddd"
    telegram.add_bot(token)
    root, project_id = make_project(tmp_path, "gamma", token, {
        "allowed_users": {"42": {"name": "Ann", "role": "supervisor"}},
        "defaults": {"debounce": 0, "delegation": "allowed"}}, schema)
    monkeypatch.chdir(root)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    cli = load_cli()
    cli_path = str(CAPABILITY_DIR / "bin" / "telegrambot")

    def answer(prompt, kwargs):
        if "Kind: registered job" in prompt:
            assert "AGENT RULES" in prompt          # the agent's prompt reaches the job
            return "=== REPLY ===\nthe report is ready"
        env = {**os.environ, **kwargs["extra_env"]}
        env.pop("TELEGRAMBOT_REAL_CLI", None)
        # The turn registers and submits through the real CLI, scoped by its env.
        python = [sys.executable, "-c", "import runpy,sys; sys.argv=['telegrambot']+sys.argv[1:];"
                  f" runpy.run_path({cli_path!r}, run_name='__main__')"]
        found = subprocess.run(python + ["jobs", "register", "make the report",
                                         "--chat", "1", "--requested-by", "1"],
                               env=env, capture_output=True, text=True, cwd=str(root))
        assert found.returncode == 0, found.stderr
        job = json.loads(found.stdout)
        assert job["chat_id"] == 42 and job["origin_message_id"] == 7   # the turn's, not ours
        done = subprocess.run(python + ["jobs", "submit", job["id"][:8],
                                        "--confirm-active-jobs-checked"],
                              env=env, capture_output=True, text=True, cwd=str(root))
        assert done.returncode == 0, done.stderr
        return "=== REPLY ===\none second"

    runner = Runner(answer)
    entry = {"id": project_id, "root": str(root), "slug": "gamma"}
    host = host_with(cli, runner, "project", entry)
    engine, thread = serve(cli, host, "project", cli._project_state_dir("gamma"))
    try:
        store = None
        settle(lambda: engine.slots.get(project_id) and engine.slots[project_id].store, cli, 'lambda: engine.slots.get(project_id) and engine.slots[project_id].store')
        store = engine.slots[project_id].store
        store.save_prompt(project_id, "bot", "AGENT RULES", "test")
        telegram.push(token, message(7, 42, 42, "please make the report"))
        settle(lambda: any(s["text"] == "the report is ready" for s in telegram.sent), cli,
               "the job's result being delivered", 40)
        texts = [s["text"] for s in telegram.sent]
        assert texts[0] == "one second"
        delivered = next(s for s in telegram.sent if s["text"] == "the report is ready")
        assert delivered["reply_parameters"]["message_id"] == 7
    finally:
        engine.stopping.set()
        engine.wake.set()
        thread.join(30)
        drop_schema(schema)


def run_cli(cli, capsys, *argv, stdin=None):
    import io
    saved, saved_in = sys.argv, sys.stdin
    sys.argv = ["telegrambot", *argv]
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    cli._RECORDS = None
    code = 0
    try:
        cli.main()
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    finally:
        sys.argv, sys.stdin = saved, saved_in
    out, err = capsys.readouterr()
    parse = lambda text: json.loads(text) if text.strip().startswith(("{", "[")) else None  # noqa
    return code, parse(out), parse(err.strip().splitlines()[-1]) if err.strip() else None


def test_profiles_and_the_service_verbs(tmp_path, home, telegram, monkeypatch, capsys):
    schema = f"tgb_cli_{uuid.uuid4().hex[:8]}"
    telegram.add_bot("555:eee")
    root, _project_id = make_project(tmp_path, "delta", "555:eee", {
        "allowed_groups": {"-1001": {"profile": "telegrambot-codex"}}}, schema)
    monkeypatch.chdir(root)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    cli = load_cli()
    try:
        code, out, err = run_cli(cli, capsys, "profiles", "list")
        assert code == 0, err
        rows = {r["name"]: r for r in out["profiles"]}
        assert rows["telegrambot-codex"]["fits_turn"] is True
        assert rows["telegrambot-codex"]["positions"] == ["allowed_groups.-1001.profile"]
        assert rows["telegrambot-gate-claude"]["fits_turn"] is False
        code, out, err = run_cli(cli, capsys, "profiles", "save", "telegrambot-fast", "-",
                                 stdin='harness = "claude"\nmodel = "haiku"\n'
                                       'permission_mode = "bypassPermissions"\n')
        assert code == 0 and out["created"] is True, err
        code, out, _ = run_cli(cli, capsys, "profiles", "show", "telegrambot-fast")
        assert out["layer"] == "project" and out["knobs"]["model"] == "haiku"
        code, out, err = run_cli(cli, capsys, "service", "harness")
        assert code == 0 and "codex" in out["harness"], err
        code, out, _ = run_cli(cli, capsys, "service", "status")
        assert out["running"] is False and out["mode"] == "project"
        code, out, _ = run_cli(cli, capsys, "service", "doctor")
        assert code == 7 and out["problems"] == ["the service is not running"], out
        assert out["bots"]["bot"]["bot"] == "bot555"
        assert out["bots"]["bot"]["history"].startswith("off")
        code, _out, err = run_cli(cli, capsys, "settings", "set", "defaults.tail_size", "500")
        assert code == 6 and err["error"]["code"] == "settings_invalid"
    finally:
        drop_schema(schema)
