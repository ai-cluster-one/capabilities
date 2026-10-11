"""The CLI against a fake homeserver: registration, the write gate, the rooms
verbs and the shape of what they send. No network."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from _cli import load_cli

HS = "https://matrix.example.org"
SERVER = "example.org"
TOKEN = "syt_fake_access_token_for_tests"


class FakeHomeserver:
    """Just enough of the client-server API to drive the verbs."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.users: set[str] = set()
        self.encrypted: set[str] = set()
        self.messages: dict[str, list[dict]] = {}
        self.txns: dict[str, str] = {}
        self.created: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content and \
            request.headers.get("content-type", "").startswith("application/json") else None
        self.requests.append((request.method, path, body))
        auth = request.headers.get("authorization")
        if path == "/_matrix/client/v3/register":
            name = (body or {}).get("username")
            if "auth" not in (body or {}):
                if f"@{name}:{SERVER}" in self.users:
                    return httpx.Response(400, json={"errcode": "M_USER_IN_USE",
                                                     "error": "User ID is not available."})
                return httpx.Response(401, json={"session": "s1", "flows": [
                    {"stages": ["m.login.registration_token"]}]})
            if body["auth"].get("token") != "reg-token":
                return httpx.Response(401, json={"errcode": "M_FORBIDDEN", "error": "bad token"})
            self.users.add(f"@{name}:{SERVER}")
            return httpx.Response(200, json={"user_id": f"@{name}:{SERVER}",
                                             "access_token": TOKEN, "device_id": "DEV1"})
        if auth != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN", "error": "unknown"})
        if path == "/_matrix/client/v3/account/whoami":
            return httpx.Response(200, json={"user_id": f"@alpha:{SERVER}", "device_id": "DEV1"})
        if path == "/_matrix/client/v3/createRoom":
            self.created.append(body)
            return httpx.Response(200, json={"room_id": "!new"})
        if path.endswith("/state/m.room.encryption/"):
            room = path.split("/rooms/")[1].split("/")[0]
            if room in self.encrypted:
                return httpx.Response(200, json={"algorithm": "m.megolm.v1.aes-sha2"})
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no state"})
        if "/send/m.room.message/" in path:
            room = path.split("/rooms/")[1].split("/")[0]
            txn = path.rsplit("/", 1)[1]
            event = self.txns.setdefault(txn, f"$ev{len(self.txns) + 1}")
            self.messages.setdefault(room, []).append({"event_id": event, "content": body})
            return httpx.Response(200, json={"event_id": event})
        if path == "/_matrix/media/v3/upload":
            return httpx.Response(200, json={"content_uri": f"mxc://{SERVER}/abc"})
        if path.endswith("/messages"):
            chunk = [{"type": "m.room.message", "event_id": f"$m{i}", "sender": f"@b:{SERVER}",
                      "origin_server_ts": 1_700_000_000_000 + i,
                      "content": {"msgtype": "m.text", "body": f"message {i}"}}
                     for i in (3, 2, 1)]
            return httpx.Response(200, json={"chunk": chunk, "end": "t1"})
        if path.startswith("/_matrix/client/v3/join/") or path.endswith("/leave") \
                or path.endswith("/invite"):
            return httpx.Response(200, json={"room_id": "!joined"})
        if "/account_data/agentrooms.marked" in path:
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED", "error": path})


@pytest.fixture()
def homeserver(monkeypatch):
    fake = FakeHomeserver()
    real = httpx.Client

    class Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    return fake


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    caps = root / "capabilities"
    caps.mkdir()
    (caps / "settings.json").write_text(json.dumps({"capabilities": {"agentrooms": {"enabled": True}}}))
    (caps / "project.json").write_text(json.dumps({"id": "prj_test", "slug": "project"}))
    home = tmp_path / "home"
    for key, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                     ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(key, str(home / sub))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    for key in list(os.environ):
        if key.startswith(("AGENTKIT_DB_", "AGENTROOMS_", "CAPABILITIES_PROJECT_",
                           "CAPABILITIES_READ_ONLY", "CAPABILITIES_AUTH_CONTEXT")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(root)
    return root


@pytest.fixture()
def cli(project):
    module = load_cli()
    module._RECORDS = None
    return module


def run(cli, capsys, *argv) -> tuple[int, dict | None, dict | None]:
    sys_argv = sys.argv
    sys.argv = ["agentrooms", *argv]
    cli._RECORDS = None
    code = 0
    try:
        cli.main()
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    finally:
        sys.argv = sys_argv
    out, err = capsys.readouterr()
    parse = lambda text: json.loads(text) if text.strip().startswith(("{", "[")) else None  # noqa: E731
    return code, parse(out), parse(err.strip().splitlines()[-1]) if err.strip() else None


def connect(project: Path, allow_write: bool) -> None:
    conns = project / "capabilities" / "agentrooms"
    conns.mkdir(parents=True, exist_ok=True)
    entry = {"homeserver": HS, "user_id": f"@alpha:{SERVER}", "secret_env": "AGENTROOMS_ALPHA_TOKEN"}
    if allow_write:
        entry["allow_write"] = True
    (conns / "connections.json").write_text(json.dumps({"connections": {"alpha": entry}}))
    (project / ".env.local").write_text(f"AGENTROOMS_ALPHA_TOKEN={TOKEN}\n")


def test_register_writes_connection_and_token(cli, project, homeserver, capsys, monkeypatch):
    monkeypatch.setenv("AGENTROOMS_REGISTRATION_TOKEN", "reg-token")
    code, out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 0, err
    assert out["user_id"] == f"@alpha:{SERVER}" and out["secret_env"] == "AGENTROOMS_ALPHA_TOKEN"
    assert TOKEN not in json.dumps(out)
    env_file = project / ".env.local"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert f"AGENTROOMS_ALPHA_TOKEN='{TOKEN}'" in env_file.read_text()
    entry = json.loads((project / "capabilities" / "agentrooms" / "connections.json").read_text())
    assert entry["connections"]["alpha"] == {"homeserver": HS, "user_id": f"@alpha:{SERVER}",
                                             "secret_env": "AGENTROOMS_ALPHA_TOKEN",
                                             "device_id": "DEV1"}
    auth = [b for m, p, b in homeserver.requests if b and "auth" in b][0]["auth"]
    assert auth == {"type": "m.login.registration_token", "token": "reg-token", "session": "s1"}
    # A second registration of the same connection id is refused before the network.
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 6 and err["error"]["code"] == "connection_exists"
    # A taken name is the homeserver's refusal.
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS, "--as", "other")
    assert code == 6 and err["error"]["code"] == "user_in_use"


def test_register_needs_the_registration_token(cli, project, homeserver, capsys):
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 2 and err["error"]["code"] == "registration_token_missing"
    assert not homeserver.requests


def test_register_refuses_a_bad_registration_token(cli, project, homeserver, capsys, monkeypatch):
    monkeypatch.setenv("AGENTROOMS_REGISTRATION_TOKEN", "wrong")
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 2 and err["error"]["code"] == "registration_token_rejected"
    assert not (project / ".env.local").exists()


def test_writes_refused_without_a_grant(cli, project, homeserver, capsys):
    connect(project, allow_write=False)
    for argv in (["post", "!room", "hi"], ["open", "x", "--invite", f"@b:{SERVER}"],
                 ["invite", "!room", f"@b:{SERVER}"], ["join", "!room"], ["leave", "!room"],
                 ["mark", "!room"], ["unmark", "!room"]):
        code, _out, err = run(cli, capsys, *argv)
        assert code == 4 and err["error"]["code"] == "read_only", argv
    assert not homeserver.requests
    code, out, _err = run(cli, capsys, "read", "!room")
    assert code == 0 and out["count"] == 3


def test_read_only_switch_closes_a_granted_connection(cli, project, homeserver, capsys, monkeypatch):
    connect(project, allow_write=True)
    monkeypatch.setenv("CAPABILITIES_READ_ONLY", "1")
    code, _out, err = run(cli, capsys, "post", "!room", "hi")
    assert code == 4 and err["error"]["code"] == "read_only_switch"


def test_post_content_thread_reply_data_mentions(cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    code, out, err = run(cli, capsys, "post", "!room", f"hello @b:{SERVER}", "--thread", "$root",
                         "--reply-to", "$parent", "--data", '{"kind": "status"}',
                         "--txn-id", "fixed-1")
    assert code == 0, err
    content = homeserver.messages["!room"][-1]["content"]
    assert content["body"] == f"hello @b:{SERVER}"
    assert content["m.mentions"] == {"user_ids": [f"@b:{SERVER}"]}
    assert content["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$root",
                                       "is_falling_back": False,
                                       "m.in_reply_to": {"event_id": "$parent"}}
    assert content["agentrooms.data"] == {"kind": "status"}
    first = out["event_id"]
    code, out, _err = run(cli, capsys, "post", "!room", f"hello @b:{SERVER}", "--txn-id", "fixed-1")
    assert out["event_id"] == first and out["txn_id"] == "fixed-1"


def test_post_file_and_refusals(cli, project, homeserver, capsys, tmp_path):
    connect(project, allow_write=True)
    file = tmp_path / "photo.png"
    file.write_bytes(b"\x89PNG fake")
    code, out, err = run(cli, capsys, "post", "!room", "caption", "--file", str(file))
    assert code == 0, err
    content = homeserver.messages["!room"][-1]["content"]
    assert content["msgtype"] == "m.image" and content["url"].startswith("mxc://")
    assert content["body"] == "caption" and content["filename"] == "photo.png"
    code, _out, err = run(cli, capsys, "post", "!room", "x", "--data", "[1]")
    assert code == 6 and err["error"]["code"] == "bad_data"
    code, _out, err = run(cli, capsys, "post", "!room", "x", "--thread", "not-an-event")
    assert code == 6 and err["error"]["code"] == "bad_event"
    homeserver.encrypted.add("!secret")
    code, _out, err = run(cli, capsys, "post", "!secret", "x")
    assert code == 6 and err["error"]["code"] == "encrypted_room"
    code, _out, err = run(cli, capsys, "read", "!secret")
    assert code == 6 and err["error"]["code"] == "encrypted_room"


def test_open_room_version_unfederated_rights_fixed(cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    code, out, err = run(cli, capsys, "open", "lab", "--invite", f"@b:{SERVER}", "--topic", "t")
    assert code == 0, err
    body = homeserver.created[-1]
    assert body["room_version"] == "12"
    assert body["creation_content"] == {"m.federate": False}
    assert body["power_level_content_override"] == {"invite": 50, "state_default": 50}
    assert "users" not in body["power_level_content_override"]
    assert body["invite"] == [f"@b:{SERVER}"]
    assert "initial_state" not in body
    code, _out, err = run(cli, capsys, "open", "lab", "--invite", "@b:other.org")
    assert code == 6 and err["error"]["code"] == "federation_refused"
    code, _out, err = run(cli, capsys, "invite", "!room", "@b:other.org")
    assert code == 6 and err["error"]["code"] == "federation_refused"
    code, _out, err = run(cli, capsys, "join", "#lab:other.org")
    assert code == 6 and err["error"]["code"] == "federation_refused"


def test_read_is_chronological_and_says_when_more_exists(cli, project, homeserver, capsys):
    connect(project, allow_write=False)
    code, out, _err = run(cli, capsys, "read", "!room", "--limit", "3")
    assert [m["body"] for m in out["messages"]] == ["message 1", "message 2", "message 3"]
    assert out["more"] is True and out["count"] == 3
    code, _out, err = run(cli, capsys, "read", "!room", "--limit", "500")
    assert code == 6


def test_connections_masks_the_token_and_doctor_checks_identity(cli, project, homeserver, capsys):
    connect(project, allow_write=False)
    code, out, _err = run(cli, capsys, "connections")
    keys = {k["key"]: k for k in out["connections"]["alpha"]["keys"]}
    assert keys["AGENTROOMS_ALPHA_TOKEN"]["value"].startswith("…")
    assert TOKEN not in json.dumps(out)
    code, out, _err = run(cli, capsys, "doctor")
    assert code == 0 and out["connections"]["alpha"]["user_id"] == f"@alpha:{SERVER}"


def test_an_earlier_agents_map_is_read_for_its_move(cli, project):
    service = project / "capabilities" / "agentrooms" / "service"
    service.mkdir(parents=True)
    assert cli._legacy_agents(project) is None
    (service / "settings.json").write_text(json.dumps({"version": 1, "agents": {
        "a": {}, "b": {"profile": "agentrooms-codex", "turn_timeout": 60, "history": 5}}}))
    assert cli._legacy_agents(project) == {
        "a": {"profile": "agentrooms-claude", "turn_timeout": 900, "history": 30},
        "b": {"profile": "agentrooms-codex", "turn_timeout": 60, "history": 5}}
    (service / "settings.json").write_text("{nope")
    with pytest.raises(cli._Refusal):
        cli._legacy_agents(project)


def test_a_project_service_takes_no_settings_file(cli, project, capsys):
    code, _out, err = run(cli, capsys, "service", "init")
    assert code == 6 and "workers bind" in err["error"]["hint"]


def test_service_run_refuses_without_a_store(cli, project, capsys):
    connect(project, allow_write=True)
    code, _out, err = run(cli, capsys, "service", "run")
    assert code == 6 and err["error"]["code"] == "store_not_configured"
    code, _out, err = run(cli, capsys, "service", "run", "--machine")
    assert code == 6 and err["error"]["code"] == "store_not_configured"


def test_service_activation_needs_an_explicit_project_enable(cli, project, capsys):
    (project / "capabilities" / "settings.json").write_text(json.dumps({"capabilities": {}}))
    global_gate = Path(os.environ["XDG_CONFIG_HOME"]) / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True, exist_ok=True)
    global_gate.write_text(json.dumps({"capabilities": {"agentrooms": {"enabled": True}}}))
    for argv in (["service", "run"], ["service", "join"], ["service", "init"]):
        code, _out, err = run(cli, capsys, *argv)
        assert code == 4 and err["error"]["code"] == "project_enable_required", argv


def test_join_and_leave_write_the_machine_list(cli, project, capsys):
    connect(project, allow_write=True)
    code, out, err = run(cli, capsys, "service", "join")
    assert code == 0 and out["joined"] is True, err
    listed = json.loads(cli._machine_projects_path().read_text())
    assert listed["projects"]["prj_test"]["slug"] == "project"
    code, _out, err = run(cli, capsys, "service", "run")
    assert code == 4 and err["error"]["code"] == "joined_to_machine_service"
    code, out, _err = run(cli, capsys, "service", "leave")
    assert out["left"] is True
    assert json.loads(cli._machine_projects_path().read_text())["projects"] == {}


def test_inventory_is_local_and_changes_nothing(cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    before = sorted(p.as_posix() for p in project.rglob("*"))
    code, out, _err = run(cli, capsys, "inventory")
    assert code == 0 and out["metrics"][0] == {"label": "agents", "value": 1}
    assert out["service"]["state"] == "stopped"
    assert sorted(p.as_posix() for p in project.rglob("*")) == before
    assert not homeserver.requests


def test_the_room_frame_is_the_capabilitys_and_the_role_follows_it(cli, project, tmp_path):
    from test_engine import FakeHost, FakeMatrix, FakeRunner, ME, engine, make_engine, message

    service = project / "capabilities" / "agentrooms" / "service"
    service.mkdir(parents=True)
    (service / "room.md").write_text("Project rules for $user_id.\n")
    template = cli._Host("project").template({"root": str(project)})
    matrix = FakeMatrix([message("$1", "@b:example.org", f"{ME} hi")])
    runner = FakeRunner(["answer"])
    host = FakeHost(matrix, runner, template=template)
    eng, slot = make_engine(tmp_path, matrix, runner, host=host,
                            workers={"w": ({"accepts_from": ["*"]}, "Keep the lab tidy.")})
    slot.store.add("!r", "$1")
    eng.turn(engine.Turn(slot, "alpha", "!r"))
    sent = runner.calls[0]["profile"].append_system_prompt
    assert sent.startswith("# You are an agent in a shared room") and f"You are {ME}," in sent
    assert sent.endswith("## Your role\n\nKeep the lab tidy.") and "Project rules" not in sent
    assert "shared room" not in runner.calls[0]["prompt"]


# ── workers in the store ─────────────────────────────────────────────────────

DSN = os.environ.get("AGENTROOMS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="AGENTROOMS_TEST_DSN is not set")


@pytest.fixture()
def stored(project, monkeypatch):
    import uuid as _uuid
    schema = f"ar_cli_{_uuid.uuid4().hex[:8]}"
    url = DSN if "sslmode=" in DSN else DSN + ("&" if "?" in DSN else "?") + "sslmode=disable"
    monkeypatch.setenv("AGENTKIT_DB_URL", url)
    monkeypatch.setenv("AGENTKIT_DB_SCHEMA", schema)
    yield schema
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


ROLE = """---
description: Answers the lab.
accepts_from: ["@b:example.org"]
limits:
  turns_per_room: 10
---
Keep the lab tidy.
"""


@needs_store
def test_workers_are_saved_as_versions_and_bound_on_the_connection(cli, project, stored, capsys,
                                                                    tmp_path):
    connect(project, allow_write=True)
    doc = tmp_path / "lab.md"
    doc.write_text(ROLE)
    code, out, err = run(cli, capsys, "workers", "save", "lab", str(doc))
    assert code == 0 and out["created"] is True, err
    first = out["version"]
    code, out, _err = run(cli, capsys, "workers", "save", "lab", str(doc))
    assert out["created"] is False and out["version"] == first
    doc.write_text(ROLE.replace("10", "12"))
    code, _out, err = run(cli, capsys, "workers", "save", "lab", str(doc), "--base", "nope")
    assert code == 6 and err["error"]["code"] == "conflict"
    code, out, _err = run(cli, capsys, "workers", "save", "lab", str(doc), "--base", first)
    assert code == 0 and out["created"] is True
    code, out, _err = run(cli, capsys, "workers", "show", "lab")
    assert out["spec"]["limits"]["turns_per_room"] == 12 and out["body"] == "Keep the lab tidy."
    assert out["text"].startswith("---\n")
    code, out, _err = run(cli, capsys, "workers", "history", "lab")
    assert [v["active"] for v in out["versions"]] == [False, True]
    doc.write_text("---\naccepts_from: []\n---\n")
    code, _out, err = run(cli, capsys, "workers", "save", "bad", str(doc))
    assert code == 6 and err["error"]["code"] == "worker_invalid"
    code, _out, err = run(cli, capsys, "workers", "save", "default", str(doc))
    assert code == 6
    code, _out, err = run(cli, capsys, "workers", "bind", "ghost")
    assert code == 3
    code, out, err = run(cli, capsys, "workers", "bind", "lab")
    assert code == 0 and out["worker"] == "lab", err
    code, out, _err = run(cli, capsys, "workers", "bind", "lab", "--room", "!lab:example.org")
    assert out["rooms"] == {"!lab:example.org": "lab"}
    code, out, _err = run(cli, capsys, "workers", "supervisor", "@boss:example.org")
    assert out["supervisor"] == "@boss:example.org"
    entry = json.loads((project / "capabilities" / "agentrooms" / "connections.json").read_text())
    alpha = entry["connections"]["alpha"]
    assert alpha["worker"] == "lab" and alpha["supervisor"] == "@boss:example.org"
    assert alpha["allow_write"] is True and alpha["secret_env"] == "AGENTROOMS_ALPHA_TOKEN"
    code, out, _err = run(cli, capsys, "workers", "list")
    names = {w["worker"]: w for w in out["workers"]}
    assert names["lab"]["source"] == "store" and names["default"]["source"] == "shipped"
    assert out["bindings"]["alpha"]["rooms"] == {"!lab:example.org": "lab"}
    code, out, _err = run(cli, capsys, "workers", "unbind", "--room", "!lab:example.org")
    assert out["rooms"] == {}
    os.environ["AGENTROOMS_TURN"] = "t1"
    try:
        code, _out, err = run(cli, capsys, "workers", "unbind")
    finally:
        del os.environ["AGENTROOMS_TURN"]
    assert code == 4 and err["error"]["code"] == "policy"
    code, out, _err = run(cli, capsys, "workers", "remove", "lab")
    assert code == 0 and out["removed"]
    code, _out, _err = run(cli, capsys, "workers", "show", "lab")
    assert code == 3


@needs_store
def test_an_agents_map_moves_into_a_bound_worker_that_admits_everyone(cli, project, stored, capsys):
    connect(project, allow_write=True)
    service = project / "capabilities" / "agentrooms" / "service"
    service.mkdir(parents=True)
    settings = service / "settings.json"
    settings.write_text(json.dumps({"version": 1, "agents": {
        "alpha": {"profile": "agentrooms-claude", "turn_timeout": 600, "history": 20},
        "gone": {}}}))
    code, out, _err = run(cli, capsys, "workers", "import")
    assert out["moved"] == {"alpha": "alpha"} and out["dropped"] == ["gone"]
    assert settings.is_file() and out["applied"] is False
    code, out, err = run(cli, capsys, "workers", "import", "--apply")
    assert code == 0 and out["removed"] == str(settings), err
    assert not settings.exists()
    code, out, _err = run(cli, capsys, "workers", "show", "alpha")
    assert out["spec"]["accepts_from"] == ["*"] and out["spec"]["history"] == 20
    assert out["spec"]["limits"] == {"turn_timeout": 600}
    entry = json.loads((project / "capabilities" / "agentrooms" / "connections.json").read_text())
    assert entry["connections"]["alpha"]["worker"] == "alpha"
