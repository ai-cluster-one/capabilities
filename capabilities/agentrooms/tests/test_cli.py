"""The CLI against a fake homeserver: registration, the write gate, the rooms
verbs and the shape of what they send. No network."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from urllib.parse import unquote

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
        self.passwords: dict[str, str] = {}
        self.syncs: list = []
        self.state: dict[str, list[dict]] = {}
        self.sync_params: list[dict] = []
        self.registration_flows = [["m.login.registration_token"]]
        self.grants: dict[str, dict] = {}
        self.refuse_grant_before_join = False
        self.joined: set[str] = set()
        self.account: dict[str, dict] = {}
        self.refuse_kick: set[str] = set()
        self.aliases: dict[str, str] = {}
        self.claimed: set[str] = set()
        self.served: dict[str, dict] = {}
        self.profiles: dict[str, dict] = {}

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
                if not self.registration_flows:
                    self.users.add(f"@{name}:{SERVER}")
                    self.passwords[f"@{name}:{SERVER}"] = body.get("password")
                    return httpx.Response(200, json={"user_id": f"@{name}:{SERVER}",
                                                     "access_token": TOKEN, "device_id": "DEV1"})
                return httpx.Response(401, json={"session": "s1", "flows": [
                    {"stages": stages} for stages in self.registration_flows]})
            if [body["auth"].get("type")] not in self.registration_flows:
                return httpx.Response(401, json={"errcode": "M_FORBIDDEN", "error": "no such flow"})
            if body["auth"]["type"] == "m.login.registration_token" and \
                    body["auth"].get("token") != "reg-token":
                return httpx.Response(401, json={"errcode": "M_FORBIDDEN", "error": "bad token"})
            self.users.add(f"@{name}:{SERVER}")
            self.passwords[f"@{name}:{SERVER}"] = body.get("password")
            return httpx.Response(200, json={"user_id": f"@{name}:{SERVER}",
                                             "access_token": TOKEN, "device_id": "DEV1"})
        if path == "/_matrix/client/v3/login":
            user = ((body or {}).get("identifier") or {}).get("user")
            if body.get("type") != "m.login.password" or \
                    self.passwords.get(user) != body.get("password"):
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN",
                                                 "error": "Invalid username or password"})
            return httpx.Response(200, json={"user_id": user, "access_token": TOKEN,
                                             "device_id": "DEV2"})
        if auth != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN", "error": "unknown"})
        if path == "/_matrix/client/v3/account/whoami":
            return httpx.Response(200, json={"user_id": f"@alpha:{SERVER}", "device_id": "DEV1"})
        if "/account_data/agentrooms.served" in path:
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            if request.method == "PUT":
                self.served[room] = body
                return httpx.Response(200, json={})
            if room in self.served:
                return httpx.Response(200, json=self.served[room])
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no data"})
        if path.startswith("/_matrix/client/v3/profile/"):
            parts = [unquote(part) for part in path[len("/_matrix/client/v3/profile/"):].split("/")]
            user = parts[0]
            profile = self.profiles.setdefault(user, {"displayname": user[1:].split(":")[0]})
            if len(parts) == 1:
                return httpx.Response(200, json=profile)
            if request.method != "GET" and user != f"@alpha:{SERVER}":
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN",
                                                 "error": "Cannot set another user's profile"})
            if request.method == "PUT":
                profile[parts[1]] = body[parts[1]]
            elif request.method == "DELETE":
                if parts[1] not in profile:
                    return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "unset"})
                profile.pop(parts[1])
            elif parts[1] not in profile:
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "unset"})
            else:
                return httpx.Response(200, json={parts[1]: profile[parts[1]]})
            return httpx.Response(200, json={})
        if "/state/m.room.member/" in path:
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            user = unquote(path.rsplit("/", 1)[1])
            found = [e for e in self.state.get(room, []) if e.get("type") == "m.room.member"
                     and e.get("state_key") == user]
            if found:
                return httpx.Response(200, json=found[-1].get("content") or {})
            if user == f"@alpha:{SERVER}" and room in self.joined:
                return httpx.Response(200, json={"membership": "join"})
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no member"})
        if path == "/_matrix/client/v3/sync":
            self.sync_params.append(dict(request.url.params))
            answer = self.syncs.pop(0)
            if callable(answer):
                answer = answer()
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(200, json=answer)
        if path.endswith("/kick"):
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            if body["user_id"] in self.refuse_kick:
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN", "error": "no"})
            for event in self.state.get(room, []):
                if event.get("type") == "m.room.member" and event.get("state_key") == body["user_id"]:
                    event["content"] = {"membership": "leave"}
            return httpx.Response(200, json={})
        if "/account_data/" in path and "/rooms/" not in path:
            kind = unquote(path.rsplit("/", 1)[1])
            if request.method == "PUT":
                self.account[kind] = body
                return httpx.Response(200, json={})
            if kind in self.account:
                return httpx.Response(200, json=self.account[kind])
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no data"})
        if path.endswith("/state") and "/rooms/" in path:
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            if room in self.aliases.values() and room not in self.joined:
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN", "error": "not in room"})
            return httpx.Response(200, json=self.state.get(room, []))
        if path.startswith("/_matrix/client/v3/directory/room/"):
            alias = unquote(path.rsplit("/", 1)[1])
            if alias in self.aliases:
                return httpx.Response(200, json={"room_id": self.aliases[alias]})
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no alias"})
        if "/state/agentrooms.card/" in path:
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            key = unquote(path.rsplit("/", 1)[1])
            if room not in self.joined:
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN", "error": "not in room"})
            events = self.state.setdefault(room, [])
            found = [e for e in events if e["type"] == "agentrooms.card" and e["state_key"] == key]
            if request.method == "PUT":
                # The homeserver's own rule: a state key that is a user id is that
                # user's alone to write.
                if key.startswith("@") and key != f"@alpha:{SERVER}":
                    return httpx.Response(403, json={"errcode": "M_FORBIDDEN",
                                                     "error": "state key is another user's"})
                for event in found:
                    events.remove(event)
                events.append({"type": "agentrooms.card", "state_key": key,
                               "sender": f"@alpha:{SERVER}", "content": body})
                return httpx.Response(200, json={"event_id": "$card"})
            if found:
                return httpx.Response(200, json=found[-1]["content"])
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no state"})
        if path == "/_matrix/client/v3/createRoom":
            self.created.append(body)
            if body.get("room_alias_name"):
                alias = f"#{body['room_alias_name']}:{SERVER}"
                if alias in self.aliases or alias in self.claimed:
                    return httpx.Response(400, json={"errcode": "M_ROOM_IN_USE",
                                                     "error": "Room alias already taken"})
                self.aliases[alias] = "!new"
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
        if path.startswith("/_matrix/client/v3/join/"):
            room = unquote(path.rsplit("/", 1)[1])
            self.joined.add(room)
            return httpx.Response(200, json={"room_id": room})
        if path.endswith("/leave") or path.endswith("/invite"):
            if path.endswith("/leave"):
                room = unquote(path.split("/rooms/")[1].split("/")[0])
                self.joined.discard(room)
                for event in self.state.get(room, []) if room in self.aliases.values() else []:
                    if event.get("type") == "m.room.member" and \
                            event.get("state_key") == f"@alpha:{SERVER}":
                        event["content"] = {**event["content"], "membership": "leave"}
            return httpx.Response(200, json={"room_id": "!joined"})
        if path.startswith("/_matrix/client/v1/media/download/"):
            return httpx.Response(200, content=b"\x89PNG avatar",
                                  headers={"content-type": "image/png"})
        if "/account_data/agentrooms.marked" in path:
            return httpx.Response(200, json={})
        if "/account_data/agentrooms.grant" in path:
            room = unquote(path.split("/rooms/")[1].split("/")[0])
            if request.method == "PUT":
                if self.refuse_grant_before_join and room not in self.joined:
                    return httpx.Response(403, json={"errcode": "M_FORBIDDEN",
                                                     "error": "not in room"})
                self.grants[room] = body
                return httpx.Response(200, json={})
            if room in self.grants:
                return httpx.Response(200, json=self.grants[room])
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "no data"})
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
    (caps / "project.json").write_text(json.dumps({"slug": "project"}))
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
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID", "prj_test")
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


def homeserver_profile(cli, user=f"@alpha:{SERVER}") -> dict:
    """The profile the fake homeserver holds for `user`, read back through it."""
    import httpx as _httpx
    with _httpx.Client(base_url=HS, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        return client.get(f"/_matrix/client/v3/profile/{user}").json()


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
    # The account has a password, which a homeserver needs to make it no guest; it is
    # sent with both requests and kept nowhere.
    password = homeserver.passwords[f"@alpha:{SERVER}"]
    assert password and len(password) >= 32
    assert [b["password"] for _m, p, b in homeserver.requests if p.endswith("/register")] == \
        [password, password]
    assert password not in json.dumps(out) + json.dumps(err) + env_file.read_text()
    # The homeserver has no directory yet: the agent is registered, and not visible.
    assert out["visible"] is False and "no directory" in out["visible_unset"]
    # A second registration of the same connection id is refused before the network.
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 6 and err["error"]["code"] == "connection_exists"
    # A taken name is the homeserver's refusal.
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS, "--as", "other")
    assert code == 6 and err["error"]["code"] == "user_in_use"


def test_a_registered_agent_is_visible_and_names_its_supervisor(cli, project, homeserver, capsys,
                                                                 monkeypatch):
    monkeypatch.setenv("AGENTROOMS_REGISTRATION_TOKEN", "reg-token")
    homeserver.aliases[DIRECTORY] = "!dir"
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS,
                          "--supervisor", "not-a-user")
    assert code == 6 and err["error"]["code"] == "bad_user"
    code, out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS,
                         "--supervisor", f"@boss:{SERVER}")
    assert code == 0 and out["visible"] is True and "!dir" in homeserver.joined, err
    assert out["supervisor"] == f"@boss:{SERVER}"
    entry = json.loads((project / "capabilities" / "agentrooms" / "connections.json").read_text())
    assert entry["connections"]["alpha"]["supervisor"] == f"@boss:{SERVER}"
    assert homeserver_profile(cli)["agentrooms.supervisor"] == f"@boss:{SERVER}"
    code, out, _err = run(cli, capsys, "connections")
    assert out["connections"]["alpha"]["supervisor"] == f"@boss:{SERVER}"


def test_register_needs_the_registration_token(cli, project, homeserver, capsys):
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 2 and err["error"]["code"] == "registration_token_missing"
    # Only the handshake reached the homeserver, which creates no account.
    assert [sorted(b) for _m, _p, b in homeserver.requests] == [["password", "username"]]
    assert not homeserver.users and not (project / ".env.local").exists()


def test_register_on_an_open_homeserver_needs_no_token(cli, project, homeserver, capsys):
    homeserver.registration_flows = [["m.login.registration_token"], ["m.login.dummy"]]
    code, out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 0, err
    assert out["user_id"] == f"@alpha:{SERVER}"
    auth = [b for m, p, b in homeserver.requests if b and "auth" in b][0]["auth"]
    assert auth == {"type": "m.login.dummy", "session": "s1"}
    assert homeserver.passwords[f"@alpha:{SERVER}"]
    assert f"AGENTROOMS_ALPHA_TOKEN='{TOKEN}'" in (project / ".env.local").read_text()


def test_register_on_a_homeserver_that_registers_at_once(cli, project, homeserver, capsys):
    homeserver.registration_flows = []
    code, out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 0, err
    assert out["user_id"] == f"@alpha:{SERVER}"
    assert not [b for m, p, b in homeserver.requests if b and "auth" in b]
    assert homeserver.passwords[f"@alpha:{SERVER}"]


def test_register_refuses_a_homeserver_offering_no_usable_flow(cli, project, homeserver, capsys):
    homeserver.registration_flows = [["m.login.recaptcha", "m.login.email.identity"]]
    code, _out, err = run(cli, capsys, "register", "alpha", "--homeserver", HS)
    assert code == 6 and err["error"]["code"] == "registration_unsupported"


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
def test_workers_are_saved_as_versions_and_bound_in_the_projects_settings(cli, project, stored,
                                                                          capsys, tmp_path,
                                                                          homeserver):
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
    assert out["supervisor"] == "@boss:example.org" and out["published"] is True
    code, _out, err = run(cli, capsys, "workers", "supervisor", "@boss:elsewhere.org")
    assert code == 6 and err["error"]["code"] == "bad_user"
    # The supervisor is the connection's, published in the agent's profile.
    assert homeserver_profile(cli)["agentrooms.supervisor"] == "@boss:example.org"
    settings = json.loads((project / "capabilities" / "agentrooms" / "service"
                           / "settings.json").read_text())
    assert settings["binding.alpha"] == {"worker": "lab", "rooms": {"!lab:example.org": "lab"}}
    entry = json.loads((project / "capabilities" / "agentrooms" / "connections.json").read_text())
    alpha = entry["connections"]["alpha"]
    assert "worker" not in alpha and alpha["supervisor"] == "@boss:example.org"
    assert alpha["allow_write"] is True and alpha["secret_env"] == "AGENTROOMS_ALPHA_TOKEN"
    # The rooms the agent opens start off, the rooms it accepts on, until set.
    code, out, _err = run(cli, capsys, "workers", "defaults")
    assert (out["opened"], out["accepted"]) == (False, True)
    code, out, err = run(cli, capsys, "workers", "defaults", "--opened", "on", "--accepted", "off")
    assert code == 0 and (out["opened"], out["accepted"]) == (True, False), err
    code, out, _err = run(cli, capsys, "workers", "list")
    names = {w["worker"]: w for w in out["workers"]}
    assert names["lab"]["source"] == "store" and names["default"]["source"] == "shipped"
    assert names["default"]["answers"] is True and "answers" not in names["lab"]
    assert out["bindings"]["alpha"]["rooms"] == {"!lab:example.org": "lab"}
    assert out["bindings"]["alpha"]["supervisor"] == "@boss:example.org"
    assert (out["bindings"]["alpha"]["opened"], out["bindings"]["alpha"]["accepted"]) == \
        (True, False)
    code, _out, err = run(cli, capsys, "settings", "set", "binding.alpha.opened", "yes")
    assert code == 6
    # The shipped default reads as a whole worker; a project's `default` replaces it.
    code, out, _err = run(cli, capsys, "workers", "show", "default")
    assert out["source"] == "shipped" and out["spec"]["accepts_from"] == ["*"]
    assert out["spec"]["limits"]["turn_timeout"] == 900 and out["spec"]["enabled"] is True
    doc.write_text("---\nenabled: false\n---\n")
    code, out, err = run(cli, capsys, "workers", "save", "default", str(doc))
    assert code == 0 and out["created"] is True, err
    code, out, _err = run(cli, capsys, "workers", "list")
    defaults = [w for w in out["workers"] if w["worker"] == "default"]
    assert [(w["source"], w["answers"]) for w in defaults] == [("store", False)]
    code, _out, err = run(cli, capsys, "workers", "bind", "default")
    assert code == 6
    code, out, _err = run(cli, capsys, "workers", "remove", "default")
    assert code == 0 and out["removed"]
    code, out, _err = run(cli, capsys, "workers", "show", "default")
    assert out["source"] == "shipped"
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
    assert code == 0 and "removed" not in out, err
    assert json.loads(settings.read_text()) == {"version": 1, "binding.alpha": {"worker": "alpha"}}
    code, out, _err = run(cli, capsys, "workers", "show", "alpha")
    assert out["spec"]["accepts_from"] == ["*"] and out["spec"]["history"] == 20
    assert out["spec"]["limits"] == {"turn_timeout": 600}


@needs_store
def test_a_binding_an_earlier_release_left_on_the_connection_holds_and_moves_at_the_next_write(
        cli, project, stored, capsys, tmp_path):
    connect(project, allow_write=True)
    path = project / "capabilities" / "agentrooms" / "connections.json"
    entry = json.loads(path.read_text())
    entry["connections"]["alpha"].update(worker="lab", supervisor="@boss:example.org")
    path.write_text(json.dumps(entry))
    doc = tmp_path / "lab.md"
    doc.write_text(ROLE)
    run(cli, capsys, "workers", "save", "lab", str(doc))
    code, out, _err = run(cli, capsys, "workers", "list")
    assert out["bindings"]["alpha"]["worker"] == "lab"
    assert out["bindings"]["alpha"]["supervisor"] == "@boss:example.org"
    code, out, err = run(cli, capsys, "workers", "bind", "lab", "--room", "!lab:example.org")
    assert code == 0 and out["worker"] == "lab", err
    # The worker moves off the connection; the supervisor is the connection's own.
    alpha = json.loads(path.read_text())["connections"]["alpha"]
    assert "worker" not in alpha and alpha["supervisor"] == "@boss:example.org"
    assert alpha["allow_write"] is True
    settings = json.loads((project / "capabilities" / "agentrooms" / "service"
                           / "settings.json").read_text())
    assert settings["binding.alpha"] == {"worker": "lab", "rooms": {"!lab:example.org": "lab"}}


@needs_store
def test_a_connection_held_in_the_store_is_bound_in_this_projects_settings(cli, project, stored,
                                                                           capsys, tmp_path,
                                                                           homeserver):
    identity = project / "capabilities" / "project.json"
    identity.write_text(json.dumps({"slug": "project", "connections": "store"}))
    (project / ".env.local").write_text(f"AGENTROOMS_ALPHA_TOKEN={TOKEN}\n")
    records = cli._records()
    records.set("agentrooms", "connection", "alpha", {
        "homeserver": HS, "user_id": f"@alpha:{SERVER}", "secret_env": "AGENTROOMS_ALPHA_TOKEN"})
    records.set("agentrooms", "grant", "alpha", {"enabled": True, "allow_write": True})
    doc = tmp_path / "lab.md"
    doc.write_text(ROLE)
    code, _out, err = run(cli, capsys, "workers", "save", "lab", str(doc))
    assert code == 0, err
    code, out, err = run(cli, capsys, "--connection", "alpha", "workers", "bind", "lab")
    assert code == 0 and out["worker"] == "lab", err
    # An earlier release kept the supervisor in this project's binding setting:
    # it holds, and moves onto the connection at the next binding write.
    records.set("agentrooms", "setting", "binding.alpha",
                {"worker": "lab", "supervisor": "@old:example.org"}, scope="project")
    code, out, _err = run(cli, capsys, "workers", "supervisor")
    assert out["supervisor"] == "@old:example.org"
    code, out, err = run(cli, capsys, "workers", "bind", "lab")
    assert code == 0, err
    cli._RECORDS = None
    spec = cli._records().resolve("agentrooms", "connection")["alpha"]["value"]
    assert "worker" not in spec and spec["supervisor"] == "@old:example.org"
    code, out, _err = run(cli, capsys, "workers", "supervisor", "@boss:example.org")
    assert out["supervisor"] == "@boss:example.org"
    settings = json.loads((project / "capabilities" / "agentrooms" / "service"
                           / "settings.json").read_text())
    assert settings["binding.alpha"] == {"worker": "lab"}
    cli._RECORDS = None
    spec = cli._records().resolve("agentrooms", "connection")["alpha"]["value"]
    assert "worker" not in spec and spec["supervisor"] == "@boss:example.org"
    code, out, _err = run(cli, capsys, "workers", "list")
    assert out["bindings"]["alpha"]["worker"] == "lab"
    code, out, _err = run(cli, capsys, "workers", "unbind")
    assert code == 0 and out["worker"] is None
    code, out, _err = run(cli, capsys, "workers", "list")
    assert out["bindings"]["alpha"]["worker"] is None
    assert out["bindings"]["alpha"]["supervisor"] == "@boss:example.org"


# ── a person's connection, the stream, owners, sessions ──────────────────────

OWNER = f"@owner:{SERVER}"


def test_login_signs_in_an_existing_account_and_keeps_no_password(cli, project, homeserver,
                                                                  capsys, monkeypatch):
    import io
    homeserver.passwords[OWNER] = "pw with spaces"
    monkeypatch.setattr(sys, "stdin", io.StringIO("wrong\n"))
    code, _out, err = run(cli, capsys, "login", OWNER, "--homeserver", HS)
    assert code == 2 and err["error"]["code"] == "login_rejected"
    assert not (project / ".env.local").exists()
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw with spaces\n"))
    code, out, err = run(cli, capsys, "login", OWNER, "--homeserver", HS)
    assert code == 0, err
    assert out["connection"] == "owner" and out["scope"] == "project" and out["user_id"] == OWNER
    assert out["usable_here"] is True and out["allow_write"] is False and "next" in out
    login = [b for m, p, b in homeserver.requests if p.endswith("/login")][-1]
    assert login["identifier"] == {"type": "m.id.user", "user": OWNER}
    for text in (json.dumps(out), (project / ".env.local").read_text(),
                 (project / "capabilities" / "agentrooms" / "connections.json").read_text()):
        assert "pw with spaces" not in text and TOKEN not in json.dumps(out)
    assert f"AGENTROOMS_OWNER_TOKEN='{TOKEN}'" in (project / ".env.local").read_text()
    # Signing in again to the same account replaces its token; another account is refused.
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw with spaces"))
    code, _out, err = run(cli, capsys, "login", OWNER, "--homeserver", HS)
    assert code == 0, err
    monkeypatch.setattr(sys, "stdin", io.StringIO("x"))
    code, _out, err = run(cli, capsys, "login", f"@other:{SERVER}", "--as", "owner",
                          "--homeserver", HS)
    assert code == 6 and err["error"]["code"] == "connection_exists"
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, _out, err = run(cli, capsys, "login", f"@other:{SERVER}", "--homeserver", HS)
    assert code == 6 and err["error"]["code"] == "password_missing"


@pytest.fixture()
def nowhere(project, tmp_path, monkeypatch):
    """Outside any project, on a machine whose global policy enables agentrooms."""
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(away)
    caps = Path(os.environ["XDG_CONFIG_HOME"]) / "capabilities"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "settings.json").write_text(json.dumps({"capabilities": {"agentrooms": {"enabled": True}}}))
    return away


def test_a_global_login_is_the_machines_read_only_outside_a_project(cli, nowhere, homeserver,
                                                                    capsys, monkeypatch):
    import io
    homeserver.passwords[OWNER] = "pw"
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw"))
    code, out, err = run(cli, capsys, "login", OWNER, "--homeserver", HS, "--global", "--default")
    assert code == 0, err
    assert out["scope"] == "global" and out["usable_here"] is True and out["allow_write"] is False
    assert "watch" in out["machine_reads"]
    credentials = Path(out["credentials"])
    assert credentials == cli.CREDENTIALS_ENV and stat.S_IMODE(credentials.stat().st_mode) == 0o600
    homeserver.syncs.append({"next_batch": "b0", "rooms": {"join": {}}})
    code, out, err = run(cli, capsys, "rooms")
    assert code == 0 and out["user_id"] == OWNER, err
    # A write is no machine read: outside a project it gets no connection at all.
    code, _out, err = run(cli, capsys, "post", "!room", "hi")
    assert code == 4 and err["error"]["code"] == "connection_not_granted"
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw"))
    code, _out, err = run(cli, capsys, "login", OWNER, "--homeserver", HS)
    assert code == 6 and err["error"]["code"] == "no_project"


def _create(sender, version="12", **extra):
    return {"type": "m.room.create", "state_key": "", "sender": sender,
            "content": {"room_version": version, **extra}}


def _levels(users):
    return {"type": "m.room.power_levels", "state_key": "", "sender": OWNER,
            "content": {"users": users}}


def test_rooms_name_each_rooms_owners_and_what_that_rests_on(cli, project, homeserver, capsys):
    connect(project, allow_write=False)
    me = f"@alpha:{SERVER}"
    homeserver.syncs.append({"next_batch": "b1", "rooms": {"join": {
        "!new": {"state": {"events": [_create(OWNER, additional_creators=[me]),
                                      _levels({})]},
                 "summary": {"m.joined_member_count": 2}},
        "!old": {"state": {"events": [_create(me, version="10"),
                                      _levels({me: 50, OWNER: 100, f"@c:{SERVER}": 100})]},
                 "summary": {"m.joined_member_count": 3}},
        "!bare": {"state": {"events": [_create(me, version="9")]},
                  "summary": {"m.joined_member_count": 1}}}}})
    code, out, err = run(cli, capsys, "rooms")
    assert code == 0, err
    rows = {r["room_id"]: r for r in out["joined"]}
    assert (rows["!new"]["owners"], rows["!new"]["owner_basis"]) == ([OWNER, me], "creator")
    assert (rows["!old"]["owners"], rows["!old"]["owner_basis"]) == \
        ([f"@c:{SERVER}", OWNER], "power_level")
    assert (rows["!bare"]["owners"], rows["!bare"]["owner_basis"]) == ([me], "creator")
    assert rows["!old"]["members"] == 3


def _message(event_id, body, sender=OWNER):
    return {"type": "m.room.message", "event_id": event_id, "sender": sender,
            "origin_server_ts": 1_700_000_000_000, "content": {"msgtype": "m.text", "body": body}}


def test_watch_prints_one_line_per_change_from_now_until_stopped(cli, project, homeserver,
                                                                 capsys, monkeypatch):
    connect(project, allow_write=False)
    me = f"@alpha:{SERVER}"
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    homeserver.state["!lab"] = [_create(OWNER), {"type": "m.room.name", "state_key": "",
                                                 "content": {"name": "Lab"}},
                                {"type": "m.room.member", "state_key": me,
                                 "content": {"membership": "join"}},
                                {"type": "m.room.member", "state_key": OWNER,
                                 "content": {"membership": "join"}}]
    join = {"type": "m.room.member", "state_key": me, "sender": me, "event_id": "$j",
            "content": {"membership": "join"}}
    homeserver.syncs += [
        {"next_batch": "b0", "rooms": {"join": {"!old": {}}}},
        {"next_batch": "b1", "rooms": {
            "invite": {"!inv": {"invite_state": {"events": [
                {"type": "m.room.member", "state_key": me, "sender": OWNER,
                 "content": {"membership": "invite"}}]}}},
            "join": {
                "!old": {"timeline": {"events": [_message("$1", "hello")]},
                         "ephemeral": {"events": [{"type": "m.receipt", "content": {
                             "$1": {"m.read": {OWNER: {"ts": 1_700_000_000_000}}}}}]},
                         "account_data": {"events": [{"type": "agentrooms.marked",
                                                      "content": {"marked": True}}]}},
                "!lab": {"timeline": {"limited": True, "events": [
                    _message("$before", "said before the join"), join,
                    _message("$2", "welcome")]}}}}},
        httpx.ConnectError("down"),
        {"next_batch": "b2", "rooms": {"join": {"!old": {"timeline": {"limited": True,
                                                                     "events": []}}}}},
        cli._WatchStopped(),
    ]
    code, lines, err = run_stream(cli, capsys, "watch")
    assert code == 0, err
    assert [line["event"] for line in lines] == [
        "ready", "invite", "message", "receipt", "marked", "membership", "message", "room",
        "lost", "resumed", "gap"]
    ready, invite, hello, receipt, marked, joined, welcome, room, lost, _resumed, gap = lines
    assert ready == {"event": "ready", "connection": "alpha", "user_id": me, "rooms": None,
                     "held": False}
    assert invite["room_id"] == "!inv" and invite["invite"]["inviter"] == OWNER
    assert hello["room_id"] == "!old" and hello["message"]["body"] == "hello"
    assert receipt == {"event": "receipt", "room_id": "!old", "user_id": OWNER, "event_id": "$1",
                       "ts": "2023-11-14T22:13:20Z"}
    assert marked == {"event": "marked", "room_id": "!old", "marked": True}
    # A room just joined prints nothing from before the join, and then its row.
    assert joined["membership"] == "join" and joined["user_id"] == me
    assert welcome["message"]["event_id"] == "$2"
    assert room["room"]["name"] == "Lab" and room["room"]["members"] == 2
    assert room["room"]["grant"] == {"everyone": True, "users": [], "recorded": False}
    assert room["room"]["held"] is None
    assert (room["room"]["owners"], room["room"]["owner_basis"]) == ([OWNER], "creator")
    assert lost["error"] and gap == {"event": "gap", "room_id": "!old"}
    # The first sync only takes the position; every later one goes on from the last.
    assert "since" not in homeserver.sync_params[0]
    assert [p.get("since") for p in homeserver.sync_params[1:]] == ["b0", "b1", "b1", "b2"]


def run_stream(cli, capsys, *argv) -> tuple[int, list[dict], dict | None]:
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
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    return code, lines, json.loads(err.strip().splitlines()[-1]) if err.strip() else None


def test_watch_refuses_an_encrypted_room_before_following(cli, project, homeserver, capsys):
    connect(project, allow_write=False)
    homeserver.encrypted.add("!secret")
    code, lines, err = run_stream(cli, capsys, "watch", "--room", "!secret")
    assert code == 6 and err["error"]["code"] == "encrypted_room" and lines == []
    assert not homeserver.sync_params


@needs_store
def test_sessions_and_the_turn_behind_an_answer(cli, project, stored, capsys):
    connect(project, allow_write=False)
    code, out, err = run(cli, capsys, "sessions")
    assert code == 0 and out == {"connection": "alpha", "user_id": f"@alpha:{SERVER}",
                                 "sessions": []}, err
    code, out, err = run(cli, capsys, "turn", "$a")
    assert code == 0 and out is None and err is None
    engine = cli._engine()
    store = engine.Store(engine.resolve_store(project)).open()
    try:
        store.save_session("prj_test", "alpha", "!r", harness="claude", session_id="s1",
                           node="n1", seen_ts=1)
        store.start_turn("t1", "prj_test", "alpha", "!r", "lab", None, ["@b:x"], ["mention"])
        store.end_turn("t1", "answered", event_id="$a", harness="claude", session_id="s1",
                       node="n1")
    finally:
        store.close()
    code, out, _err = run(cli, capsys, "sessions", "--room", "!r")
    assert [(r["room_id"], r["harness"], r["session_id"], r["turns"]) for r in out["sessions"]] \
        == [("!r", "claude", "s1", 1)]
    code, out, _err = run(cli, capsys, "turn", "$a")
    assert {k: out[k] for k in ("event_id", "connection", "turn", "room_id", "worker", "harness",
                                "session_id", "node", "outcome")} == {
        "event_id": "$a", "connection": "alpha", "turn": "t1", "room_id": "!r", "worker": "lab",
        "harness": "claude", "session_id": "s1", "node": "n1", "outcome": "answered"}
    code, _out, err = run(cli, capsys, "turn", "nope")
    assert code == 6 and err["error"]["code"] == "bad_event"


# ── the operator's consent: invites wait, a grant per room, held wakes ───────


def _invited(room, me, inviter=OWNER, reason=None, members=()):
    mine = {"membership": "invite", **({"reason": reason} if reason else {})}
    events = [{"type": "m.room.name", "state_key": "", "content": {"name": "Lab"}},
              {"type": "m.room.member", "state_key": me, "sender": inviter, "content": mine}]
    events += [{"type": "m.room.member", "state_key": who, "sender": who,
                "content": {"membership": "join"}} for who in members]
    return {"next_batch": "i1", "rooms": {"invite": {room: {"invite_state": {"events": events}}}}}


def _paths(homeserver, method, part):
    return [p for m, p, _b in homeserver.requests if m == method and part in p]


def test_an_invite_waits_for_the_operator_and_accepting_grants_the_inviter(
        cli, project, homeserver, capsys, monkeypatch):
    connect(project, allow_write=True)
    me = f"@alpha:{SERVER}"
    homeserver.syncs.append(_invited("!inv", me, reason="Help us sort the lab?",
                                     members=[OWNER, f"@c:{SERVER}"]))
    code, out, err = run(cli, capsys, "rooms")
    assert code == 0, err
    assert out["invites"] == [{"room_id": "!inv", "name": "Lab", "inviter": OWNER,
                               "reason": "Help us sort the lab?",
                               "members": [f"@c:{SERVER}", OWNER], "encrypted": False}]
    assert out["held_unread"]  # no store here: held wakes are not read, and it says so
    # Inside a turn the agent neither accepts nor declines for itself.
    monkeypatch.setenv("AGENTROOMS_TURN", "t1")
    for verb in ("join", "leave"):
        homeserver.syncs.append(_invited("!inv", me))
        code, _out, err = run(cli, capsys, verb, "!inv")
        assert code == 4 and err["error"]["code"] == "policy" and "operator" in err["error"]["message"]
    assert not _paths(homeserver, "POST", "/join/") and not _paths(homeserver, "POST", "/leave")
    assert homeserver.grants == {}
    monkeypatch.delenv("AGENTROOMS_TURN")
    # The operator accepts: the grant - the inviter alone - is written before the join.
    homeserver.syncs.append(_invited("!inv", me))
    code, out, err = run(cli, capsys, "join", "!inv")
    assert code == 0, err
    assert out == {"room_id": "!inv", "joined": True,
                   "grant": {"everyone": False, "users": [OWNER], "recorded": True},
                   "served": True}
    order = [(m, p) for m, p, _b in homeserver.requests
             if "agentrooms.grant" in p or "agentrooms.served" in p or "/join/" in p]
    # The grant and the switch - a room accepted starts served, as the agent's
    # `accepted` default says - are written before the join, so the service's
    # first sync of the room reads both.
    assert [(m, p.rsplit("/", 1)[1]) for m, p in order] == [
        ("PUT", "agentrooms.grant"), ("PUT", "agentrooms.served"), ("POST", "%21inv")] or \
        [m for m, _p in order] == ["PUT", "PUT", "POST"]
    assert homeserver.served["!inv"] == {"served": True}
    # A homeserver that refuses account data for a room not joined gets it after.
    homeserver.grants.clear()
    homeserver.refuse_grant_before_join = True
    homeserver.syncs.append(_invited("!two", me))
    code, out, err = run(cli, capsys, "join", "!two")
    assert code == 0 and homeserver.grants["!two"] == {"everyone": False, "users": [OWNER]}, err
    # A room joined without an invite keeps whatever grant it has; none admits everyone.
    homeserver.syncs.append({"next_batch": "n"})
    code, out, err = run(cli, capsys, "join", "!public")
    assert code == 0 and out["grant"] == {"everyone": True, "users": [], "recorded": False}, err
    # An agent whose accepted rooms start off: the switch says so, and a room it
    # is in already keeps the switch it has.
    code, out, err = run(cli, capsys, "workers", "defaults", "--accepted", "off")
    assert code == 0 and out["accepted"] is False, err
    homeserver.syncs.append(_invited("!three", me))
    code, out, err = run(cli, capsys, "join", "!three")
    assert code == 0 and out["served"] is False, err
    assert homeserver.served["!three"] == {"served": False}
    homeserver.syncs.append({"next_batch": "n"})
    code, out, err = run(cli, capsys, "join", "!inv")
    assert code == 0 and out["served"] is True and homeserver.served["!inv"] == {"served": True}
    # Outside a turn, declining is the operator's leave.
    monkeypatch.setenv("AGENTROOMS_TURN", "")
    code, out, err = run(cli, capsys, "leave", "!other")
    assert code == 0 and out["left"] is True, err


def test_the_grant_verbs_and_inviting_as_consent(cli, project, homeserver, capsys, monkeypatch):
    connect(project, allow_write=True)
    b, c = f"@b:{SERVER}", f"@c:{SERVER}"
    code, out, err = run(cli, capsys, "grant", "show", "!r")
    assert out == {"room_id": "!r", "everyone": True, "users": [], "recorded": False}, err
    # Inviting into a room with no grant writes none: it admits everyone already.
    code, out, err = run(cli, capsys, "invite", "!r", c)
    assert code == 0 and "!r" not in homeserver.grants and out["grant"]["recorded"] is False
    code, out, err = run(cli, capsys, "grant", "everyone", "!r", "off")
    assert out == {"room_id": "!r", "everyone": False, "users": [], "recorded": True}, err
    code, out, err = run(cli, capsys, "grant", "add", "!r", b)
    assert out["users"] == [b] and homeserver.grants["!r"] == {"everyone": False, "users": [b]}
    code, out, err = run(cli, capsys, "invite", "!r", c, "--message", "Join us for the review")
    assert code == 0 and out["grant"]["users"] == [b, c], err
    sent = [body for m, p, body in homeserver.requests if p.endswith("/invite")][-1]
    assert sent == {"user_id": c, "reason": "Join us for the review"}
    code, out, err = run(cli, capsys, "grant", "withdraw", "!r", b)
    assert out["users"] == [c] and out["everyone"] is False
    code, out, err = run(cli, capsys, "grant", "everyone", "!r", "on")
    assert out == {"room_id": "!r", "everyone": True, "users": [c], "recorded": True}
    code, out, err = run(cli, capsys, "grant", "add", "!r", "@b:other.org")
    assert code == 6 and err["error"]["code"] == "federation_refused"
    # A room the agent opens admits the invited alone.
    code, out, err = run(cli, capsys, "open", "lab", "--invite", b)
    assert code == 0 and out["grant"] == {"everyone": False, "users": [b], "recorded": True}, err
    assert homeserver.grants["!new"] == {"everyone": False, "users": [b]}
    # Inside a turn no grant moves and nothing held is answered.
    monkeypatch.setenv("AGENTROOMS_TURN", "t1")
    for argv in (["grant", "add", "!r", b], ["grant", "everyone", "!r", "off"],
                 ["held", "answer", "!r"], ["held", "dismiss", "!r"]):
        code, _out, err = run(cli, capsys, *argv)
        assert code == 4 and err["error"]["code"] == "policy", argv
    code, out, err = run(cli, capsys, "grant", "show", "!r")
    assert code == 0 and out["users"] == [c]
    monkeypatch.delenv("AGENTROOMS_TURN")
    # A grant change is a write: refused on a connection without the write grant.
    connect(project, allow_write=False)
    code, _out, err = run(cli, capsys, "grant", "add", "!r", b)
    assert code == 4 and err["error"]["code"] == "read_only"
    code, out, err = run(cli, capsys, "grant", "show", "!r")
    assert code == 0 and out["everyone"] is True


def test_watch_prints_grant_changes(cli, project, homeserver, capsys):
    connect(project, allow_write=False)
    homeserver.syncs += [
        {"next_batch": "b0", "rooms": {"join": {"!old": {}}}},
        {"next_batch": "b1", "rooms": {"join": {"!old": {"account_data": {"events": [
            {"type": "agentrooms.grant", "content": {"everyone": False,
                                                     "users": [OWNER]}}]}}}}},
        cli._WatchStopped(),
    ]
    code, lines, err = run_stream(cli, capsys, "watch")
    assert code == 0, err
    assert lines[1:] == [{"event": "grant", "room_id": "!old", "grant": {
        "everyone": False, "users": [OWNER], "recorded": True}}]


@needs_store
def test_held_wakes_are_listed_answered_and_dismissed(cli, project, stored, homeserver, capsys):
    connect(project, allow_write=False)
    engine = cli._engine()
    store = engine.Store(engine.resolve_store(project)).open()
    stranger, other = f"@s:{SERVER}", f"@o:{SERVER}"
    try:
        store.acquire("prj_test", "alpha", "h1", "n1", 1)
        store.save_sync("prj_test", "alpha", f"@alpha:{SERVER}", "h1", "b1", [
            {"room_id": "!r", "key": key, "kind": "mention", "event_id": key, "sender": who,
             "held": True} for key, who in (("$1", stranger), ("$2", other), ("$3", stranger))])
    finally:
        store.close()
    homeserver.syncs.append({"next_batch": "b1", "rooms": {"join": {
        "!r": {"state": {"events": [_create(OWNER)]}, "summary": {"m.joined_member_count": 3}},
        "!quiet": {"state": {"events": [_create(OWNER)]},
                   "summary": {"m.joined_member_count": 3}}}}})
    code, out, err = run(cli, capsys, "rooms")
    assert code == 0 and "held_unread" not in out, err
    rows = {r["room_id"]: r for r in out["joined"]}
    assert rows["!r"]["held"] == {"count": 3, "senders": [other, stranger]}
    assert rows["!quiet"]["held"] == {"count": 0, "senders": []}
    code, out, err = run(cli, capsys, "held", "list", "--room", "!r")
    assert code == 0 and [(h["event_id"], h["sender"]) for h in out["held"]] == [
        ("$1", stranger), ("$2", other), ("$3", stranger)], err
    code, out, err = run(cli, capsys, "held", "answer", "!r", "--from", stranger)
    assert code == 0 and [w["event_id"] for w in out["answered"]] == ["$1", "$3"], err
    code, out, err = run(cli, capsys, "held", "dismiss", "!r")
    assert code == 0 and [w["event_id"] for w in out["dismissed"]] == ["$2"], err
    code, out, err = run(cli, capsys, "held", "list")
    assert out["held"] == []
    code, _out, err = run(cli, capsys, "held", "answer", "!r", "--from", "nobody")
    assert code == 6 and err["error"]["code"] == "bad_user"


@needs_store
def test_watch_follows_the_held_wakes_in_the_store(cli, project, stored, homeserver, capsys):
    import time as _time
    connect(project, allow_write=False)
    engine = cli._engine()
    stranger = f"@s:{SERVER}"

    def hold_one():
        store = engine.Store(engine.resolve_store(project)).open()
        try:
            store.acquire("prj_test", "alpha", "h1", "n1", 1)
            store.save_sync("prj_test", "alpha", f"@alpha:{SERVER}", "h1", "b1", [
                {"room_id": "!r", "key": "$1", "kind": "mention", "event_id": "$1",
                 "sender": stranger, "held": True}])
            _time.sleep(1.0)
            store.dismiss_held("prj_test", "alpha", "!r")
            _time.sleep(1.0)
        finally:
            store.close()
        return {"next_batch": "b2"}

    homeserver.syncs += [{"next_batch": "b0"}, hold_one, cli._WatchStopped()]
    code, lines, err = run_stream(cli, capsys, "watch")
    assert code == 0, err
    assert lines[0]["held"] is True
    assert lines[1:] == [
        {"event": "held", "room_id": "!r", "held": {"count": 1, "senders": [stranger]}},
        {"event": "held", "room_id": "!r", "held": {"count": 0, "senders": []}}]


@needs_store
def test_watch_no_held_follows_matrix_holding_no_store_connection(cli, project, stored,
                                                                 homeserver, capsys):
    # The connection is resolved from the store, as in a project whose
    # connections moved there; following then holds nothing of the store.
    import psycopg
    identity = project / "capabilities" / "project.json"
    identity.write_text(json.dumps({"slug": "project", "connections": "store"}))
    (project / ".env.local").write_text(f"AGENTROOMS_ALPHA_TOKEN={TOKEN}\n")
    records = cli._records()
    records.set("agentrooms", "connection", "alpha", {
        "homeserver": HS, "user_id": f"@alpha:{SERVER}", "secret_env": "AGENTROOMS_ALPHA_TOKEN"})
    records.set("agentrooms", "grant", "alpha", {"enabled": True, "allow_write": False})
    held_open = []

    def backends(named):
        with psycopg.connect(DSN, autocommit=True) as conn:
            return {pid for (pid,) in conn.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name LIKE %s "
                "AND pid <> pg_backend_pid()", (named,))}

    # An earlier watch in this test process may still be listening.
    listening = backends("agentrooms-watch")

    def count_backends():
        held_open.append(len(backends("agentrooms%") - listening))
        return {"next_batch": "b1"}

    homeserver.syncs += [{"next_batch": "b0"}, count_backends, cli._WatchStopped()]
    code, lines, err = run_stream(cli, capsys, "--connection", "alpha", "watch", "--no-held")
    assert code == 0, err
    assert lines == [{"event": "ready", "connection": "alpha", "user_id": f"@alpha:{SERVER}",
                      "rooms": None, "held": False}]
    assert held_open == [0]


def _member(who, membership):
    return {"type": "m.room.member", "state_key": who, "sender": who,
            "content": {"membership": membership}}


def _owned(homeserver, room, me, others=(), invited=(), creator=None):
    homeserver.state[room] = [_create(creator or me), {"type": "m.room.name", "state_key": "",
                                                        "content": {"name": "Lab"}},
                              _member(me, "join"), *[_member(u, "join") for u in others],
                              *[_member(u, "invite") for u in invited]]


def test_an_owner_removes_a_member_and_cancels_an_invite(cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    me, b, c = f"@alpha:{SERVER}", f"@b:{SERVER}", f"@c:{SERVER}"
    _owned(homeserver, "!lab", me, others=[b], invited=[c])
    code, out, err = run(cli, capsys, "remove", "!lab", b, "--reason", "off topic")
    assert code == 0 and out == {"room_id": "!lab", "removed": b, "was": "join"}, err
    code, out, err = run(cli, capsys, "remove", "!lab", c)
    assert code == 0 and out == {"room_id": "!lab", "removed": c, "was": "invite"}, err
    kicks = [body for m, p, body in homeserver.requests if p.endswith("/kick")]
    assert kicks == [{"user_id": b, "reason": "off topic"}, {"user_id": c}]
    # Repeating it is harmless: WHO is already out, and nothing is sent.
    code, out, err = run(cli, capsys, "remove", "!lab", b)
    assert code == 0 and out["was"] is None and len(_paths(homeserver, "POST", "/kick")) == 2
    code, _out, err = run(cli, capsys, "remove", "!lab", me)
    assert code == 6 and err["error"]["code"] == "bad_user"
    # In a room another agent owns, refused before anything is sent, the owners named.
    _owned(homeserver, "!theirs", me, others=[OWNER, b], creator=OWNER)
    code, _out, err = run(cli, capsys, "remove", "!theirs", b)
    assert code == 6 and err["error"]["code"] == "not_owner" and OWNER in err["error"]["message"]
    code, _out, err = run(cli, capsys, "remove", "!nowhere", b)
    assert code == 6 and err["error"]["code"] == "not_member"
    assert len(_paths(homeserver, "POST", "/kick")) == 2
    # A write: refused on a connection without the write grant.
    connect(project, allow_write=False)
    code, _out, err = run(cli, capsys, "remove", "!lab", b)
    assert code == 4 and err["error"]["code"] == "read_only"


def test_an_owner_cannot_simply_leave_and_archives_instead(cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    me, b, c = f"@alpha:{SERVER}", f"@b:{SERVER}", f"@c:{SERVER}"
    _owned(homeserver, "!lab", me, others=[b], invited=[c])
    code, _out, err = run(cli, capsys, "leave", "!lab")
    assert code == 6 and err["error"]["code"] == "owner_cannot_leave"
    assert "archive" in err["error"]["hint"] and not _paths(homeserver, "POST", "/leave")
    # A room it does not own, or one it owns alone, it leaves as before.
    _owned(homeserver, "!theirs", me, others=[OWNER], creator=OWNER)
    _owned(homeserver, "!alone", me)
    for room in ("!theirs", "!alone"):
        code, out, err = run(cli, capsys, "leave", room)
        assert code == 0 and out["left"] is True, err
    # Archive: everyone else out, the record written, then the agent leaves.
    code, out, err = run(cli, capsys, "archive", "!lab")
    assert code == 0, err
    assert out["removed"] == [{"user_id": b, "was": "join"}, {"user_id": c, "was": "invite"}]
    assert out["name"] == "Lab" and out["archived_at"] and out["left"] is True
    assert out["already_archived"] is False
    record = homeserver.account["agentrooms.archived"]["rooms"]["!lab"]
    assert record == {"name": "Lab", "archived_at": out["archived_at"]}
    acts = [(m, p.rsplit("/", 1)[1]) for m, p, _b in homeserver.requests
            if m in ("POST", "PUT") and ("/rooms/!lab" in p or "agentrooms.archived" in p)]
    assert acts[-4:] == [("POST", "kick"), ("POST", "kick"), ("PUT", "agentrooms.archived"),
                         ("POST", "leave")]
    # Once left, repeating it answers the record and sends nothing.
    homeserver.state["!lab"][2] = _member(me, "leave")
    code, again, err = run(cli, capsys, "archive", "!lab")
    assert code == 0 and again["already_archived"] is True and again["removed"] == [], err
    assert again["archived_at"] == out["archived_at"]
    # rooms lists it on request; read reads its history.
    homeserver.syncs.append({"next_batch": "n"})
    code, rooms, err = run(cli, capsys, "rooms", "--archived")
    assert code == 0 and rooms["archived"] == [{"room_id": "!lab", "name": "Lab",
                                                "archived_at": out["archived_at"]}], err
    homeserver.syncs.append({"next_batch": "n"})
    code, rooms, err = run(cli, capsys, "rooms")
    assert code == 0 and "archived" not in rooms
    code, read, err = run(cli, capsys, "read", "!lab")
    assert code == 0 and read["count"] == 3, err
    # Not an owner: refused.
    code, _out, err = run(cli, capsys, "archive", "!theirs")
    assert code == 6 and err["error"]["code"] == "not_owner"


def test_an_archive_stopped_part_way_names_what_was_done_and_finishes_when_repeated(
        cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    me, b, c = f"@alpha:{SERVER}", f"@b:{SERVER}", f"@c:{SERVER}"
    _owned(homeserver, "!lab", me, others=[b, c])
    homeserver.refuse_kick.add(c)
    code, _out, err = run(cli, capsys, "archive", "!lab")
    assert code == 6 and err["error"]["code"] == "archive_incomplete"
    assert b in err["error"]["message"] and c in err["error"]["message"]
    assert "agentrooms.archived" not in homeserver.account
    assert not _paths(homeserver, "POST", "/leave")
    homeserver.refuse_kick.clear()
    code, out, err = run(cli, capsys, "archive", "!lab")
    assert code == 0 and out["removed"] == [{"user_id": c, "was": "join"}], err


def test_an_operator_blocks_a_sender(cli, project, homeserver, capsys, monkeypatch):
    connect(project, allow_write=True)
    me, b = f"@alpha:{SERVER}", f"@b:{SERVER}"
    homeserver.account["m.ignored_user_list"] = {"ignored_users": {f"@old:{SERVER}": {}}}
    code, out, err = run(cli, capsys, "block", "add", b)
    assert code == 0 and out == {"user_id": me, "blocked": [b, f"@old:{SERVER}"]}, err
    assert homeserver.account["m.ignored_user_list"] == {
        "ignored_users": {f"@old:{SERVER}": {}, b: {}}}
    puts = len(_paths(homeserver, "PUT", "m.ignored_user_list"))
    code, out, err = run(cli, capsys, "block", "add", b)
    assert code == 0 and len(_paths(homeserver, "PUT", "m.ignored_user_list")) == puts
    # A blocked inviter's invite leaves the list; another's stays.
    homeserver.syncs.append(_invited("!inv", me, inviter=b))
    code, rooms, err = run(cli, capsys, "rooms")
    assert code == 0 and rooms["invites"] == [], err
    code, out, err = run(cli, capsys, "block", "remove", b)
    assert code == 0 and out["blocked"] == [f"@old:{SERVER}"], err
    homeserver.syncs.append(_invited("!inv", me, inviter=b))
    code, rooms, err = run(cli, capsys, "rooms")
    assert [r["inviter"] for r in rooms["invites"]] == [b]
    code, out, err = run(cli, capsys, "block", "list")
    assert code == 0 and out == {"user_id": me, "blocked": [f"@old:{SERVER}"]}
    code, _out, err = run(cli, capsys, "block", "add", me)
    assert code == 6 and err["error"]["code"] == "bad_user"
    code, _out, err = run(cli, capsys, "block", "add", "@b:other.org")
    assert code == 6 and err["error"]["code"] == "federation_refused"
    # The operator's, not the agent's: refused inside a turn; a write otherwise.
    monkeypatch.setenv("AGENTROOMS_TURN", "t1")
    for verb in ("add", "remove"):
        code, _out, err = run(cli, capsys, "block", verb, b)
        assert code == 4 and err["error"]["code"] == "policy"
    code, out, err = run(cli, capsys, "block", "list")
    assert code == 0
    monkeypatch.delenv("AGENTROOMS_TURN")
    connect(project, allow_write=False)
    code, _out, err = run(cli, capsys, "block", "add", b)
    assert code == 4 and err["error"]["code"] == "read_only"
    code, out, err = run(cli, capsys, "block", "list")
    assert code == 0 and out["blocked"] == [f"@old:{SERVER}"]


def test_watch_prints_block_and_archive_changes_and_no_blocked_invite(cli, project, homeserver,
                                                                     capsys):
    connect(project, allow_write=False)
    me, b = f"@alpha:{SERVER}", f"@b:{SERVER}"
    old = {"!old": {"name": "Old", "archived_at": "2026-10-01T00:00:00Z"}}
    homeserver.syncs += [
        {"next_batch": "b0", "account_data": {"events": [
            {"type": "agentrooms.archived", "content": {"rooms": old}}]}},
        {"next_batch": "b1", "account_data": {"events": [
            {"type": "m.ignored_user_list", "content": {"ignored_users": {b: {}}}}]},
         "rooms": {"invite": {
             **_invited("!fromb", me, inviter=b)["rooms"]["invite"],
             **_invited("!fromowner", me)["rooms"]["invite"]}}},
        {"next_batch": "b2", "account_data": {"events": [
            {"type": "agentrooms.archived", "content": {"rooms": {
                **old, "!lab": {"name": "Lab", "archived_at": "2026-10-13T00:00:00Z"}}}}]},
         "rooms": {"leave": {"!lab": {"timeline": {"events": [
             {**_member(me, "leave"), "event_id": "$l"}]}}}}},
        cli._WatchStopped(),
    ]
    code, lines, err = run_stream(cli, capsys, "watch")
    assert code == 0, err
    assert [line["event"] for line in lines] == ["ready", "invite", "blocked", "membership",
                                                 "archived"]
    assert lines[1]["room_id"] == "!fromowner"
    assert lines[2] == {"event": "blocked", "blocked": [b]}
    assert lines[4] == {"event": "archived", "room_id": "!lab",
                        "archived": {"name": "Lab", "archived_at": "2026-10-13T00:00:00Z"}}
    # The first sync takes what the account data holds; both types are followed.
    for params in homeserver.sync_params[:2]:
        assert json.loads(params["filter"])["account_data"] == {
            "types": ["agentrooms.archived", "m.ignored_user_list"]}


DIRECTORY = f"#agentrooms-directory:{SERVER}"


def test_the_directory_is_created_once_and_lists_every_member_with_its_card(
        cli, project, homeserver, capsys):
    connect(project, allow_write=True)
    code, _, err = run(cli, capsys, "directory", "list")
    assert code == 3 and err["error"]["code"] == "no_directory"
    code, out, _ = run(cli, capsys, "directory", "create")
    assert code == 0 and out == {"room_id": "!new", "alias": DIRECTORY, "created": True}
    body = homeserver.created[-1]
    assert body["room_alias_name"] == "agentrooms-directory" and body["preset"] == "public_chat"
    assert body["room_version"] == "12" and body["visibility"] == "private"
    assert body["creation_content"] == {"m.federate": False, "type": "agentrooms.directory"}
    assert body["power_level_content_override"] == {
        "events_default": 50, "invite": 50, "state_default": 50,
        "events": {"agentrooms.card": 0}}
    made = len(homeserver.created)
    code, _, err = run(cli, capsys, "directory", "create")
    assert code == 6 and err["error"]["code"] == "directory_exists" and "!new" in err["error"]["message"]
    assert len(homeserver.created) == made
    # Created between the look-up and the create: the homeserver's refusal says so.
    homeserver.aliases.clear()
    homeserver.claimed.add(DIRECTORY)
    code, _, err = run(cli, capsys, "directory", "create")
    assert code == 6 and err["error"]["code"] == "directory_exists"

    homeserver.aliases[DIRECTORY] = "!new"
    code, _, err = run(cli, capsys, "directory", "list")
    assert code == 6 and err["error"]["code"] == "not_in_directory"
    code, out, _ = run(cli, capsys, "directory", "join")
    assert code == 0 and out == {"room_id": "!new", "alias": DIRECTORY, "joined": True,
                                 "visible": True}
    member = lambda user, name, membership="join": {  # noqa: E731
        "type": "m.room.member", "state_key": user, "sender": user,
        "content": {"membership": membership, "displayname": name}}
    card = lambda user, content, sender=None: {  # noqa: E731
        "type": "agentrooms.card", "state_key": user, "sender": sender or user, "content": content}
    homeserver.state["!new"] = [
        member(f"@alpha:{SERVER}", "Alpha"), member(f"@bee:{SERVER}", "Bee"),
        member(f"@cee:{SERVER}", "Cee"), member(f"@gone:{SERVER}", "Gone", "leave"),
        card(f"@bee:{SERVER}", {"name": "Bee", "about": "Reviews pull requests.",
                                "supervisor": f"@operator:{SERVER}"}),
        card(f"@cee:{SERVER}", {"name": "Not Cee"}, sender=f"@bee:{SERVER}"),
        card(f"@gone:{SERVER}", {"name": "Gone"}),
        card("bee", {"name": "keyed by no user"}, sender=f"@cee:{SERVER}"),
    ]
    homeserver.profiles[f"@dee:{SERVER}"] = {"displayname": "Dee"}
    homeserver.state["!new"].append(member(f"@dee:{SERVER}", "Dee"))
    code, out, err = run(cli, capsys, "card", "set", "--name", "Alpha", "--about", "Plans releases.")
    assert code == 0 and out == {"user_id": f"@alpha:{SERVER}", "card": {
        "name": "Alpha", "about": "Plans releases.", "supervisor": None, "avatar": None}}, err
    # The card is the agent's profile: every Matrix client shows the name it set.
    assert homeserver_profile(cli)["displayname"] == "Alpha"
    assert homeserver_profile(cli)["agentrooms.about"] == "Plans releases."
    code, out, _ = run(cli, capsys, "directory", "list")
    assert code == 0 and out["room_id"] == "!new" and out["alias"] == DIRECTORY
    # A card an earlier release published, not yet moved by its agent, reads back unchanged.
    assert out["members"] == [
        {"user_id": f"@alpha:{SERVER}", "display_name": "Alpha",
         "card": {"name": "Alpha", "about": "Plans releases.", "supervisor": None,
                  "avatar": None}},
        {"user_id": f"@bee:{SERVER}", "display_name": "Bee",
         "card": {"name": "Bee", "about": "Reviews pull requests.",
                  "supervisor": f"@operator:{SERVER}", "avatar": None}},
        {"user_id": f"@cee:{SERVER}", "display_name": "Cee", "card": None},
        {"user_id": f"@dee:{SERVER}", "display_name": "Dee",
         "card": {"name": "Dee", "about": None, "supervisor": None, "avatar": None}}]
    code, out, _ = run(cli, capsys, "card", "show", f"@bee:{SERVER}")
    assert code == 0 and out["card"]["about"] == "Reviews pull requests." and out["visible"] is True
    code, out, _ = run(cli, capsys, "card", "show", f"@gone:{SERVER}")
    assert code == 0 and out["visible"] is False
    # Invisible: out of the directory, the agent does not see who is in it.
    code, out, err = run(cli, capsys, "directory", "leave")
    assert code == 0 and out == {"room_id": "!new", "alias": DIRECTORY, "left": True,
                                 "visible": False}, err
    code, _, err = run(cli, capsys, "directory", "list")
    assert code == 6 and err["error"]["code"] == "not_in_directory"
    code, out, _ = run(cli, capsys, "card", "show")
    assert code == 0 and out["visible"] is False and out["card"]["name"] == "Alpha"
    code, out, _ = run(cli, capsys, "card", "show", f"@bee:{SERVER}")
    assert code == 0 and out["visible"] is None


def test_an_agent_sets_changes_and_clears_its_own_card_only(cli, project, homeserver, capsys,
                                                             tmp_path):
    connect(project, allow_write=False)
    code, _, err = run(cli, capsys, "card", "set", "--name", "Alpha")
    assert code == 4 and err["error"]["code"] == "read_only"
    code, _, err = run(cli, capsys, "directory", "join")
    assert code == 4 and err["error"]["code"] == "read_only"
    code, _, err = run(cli, capsys, "directory", "leave")
    assert code == 4 and err["error"]["code"] == "read_only"
    connect(project, allow_write=True)
    code, _, err = run(cli, capsys, "card", "set")
    assert code == 6 and err["error"]["code"] == "invalid_arguments"
    code, _, err = run(cli, capsys, "card", "set", "--about", "x" * 501)
    assert code == 6 and err["error"]["code"] == "invalid_arguments"
    code, _, err = run(cli, capsys, "card", "set", "--name", "  ")
    assert code == 6 and err["error"]["code"] == "invalid_arguments"
    code, _, err = run(cli, capsys, "card", "set", "--supervisor", f"@operator:{SERVER}")
    assert code == 6 and err["error"]["code"] == "invalid_arguments"
    assert "workers supervisor" in err["error"]["hint"]
    # A card needs no directory: it is the profile, and setting it joins nothing.
    code, out, err = run(cli, capsys, "card", "set", "--name", "Alpha")
    assert code == 0 and out["card"]["name"] == "Alpha" and not homeserver.joined, err
    text = tmp_path / "notes.txt"
    text.write_text("not an image")
    code, _, err = run(cli, capsys, "card", "set", "--avatar", str(text))
    assert code == 6 and err["error"]["code"] == "bad_avatar"
    image = tmp_path / "face.png"
    image.write_bytes(b"\x89PNG avatar")
    code, out, err = run(cli, capsys, "card", "set", "--about", "Plans releases.",
                         "--avatar", str(image))
    assert code == 0 and out["card"] == {"name": "Alpha", "about": "Plans releases.",
                                         "supervisor": None,
                                         "avatar": {"mxc": f"mxc://{SERVER}/abc"}}, err
    assert homeserver_profile(cli)["avatar_url"] == f"mxc://{SERVER}/abc"
    writes = [p for m, p, _ in homeserver.requests if m in ("PUT", "DELETE") and "/profile/" in p]
    assert writes and all(f"/profile/@alpha:{SERVER}/" in p for p in writes)
    saved = tmp_path / "out" / "face.png"
    code, out, err = run(cli, capsys, "card", "avatar", "--out", str(saved))
    assert code == 0 and out == {"user_id": f"@alpha:{SERVER}", "mxc": f"mxc://{SERVER}/abc",
                                 "path": str(saved), "mimetype": "image/png",
                                 "size": len(b"\x89PNG avatar")}, err
    assert saved.read_bytes() == b"\x89PNG avatar"
    code, _, err = run(cli, capsys, "card", "avatar", f"@nobody:{SERVER}", "--out", str(saved))
    assert code == 3 and err["error"]["code"] == "avatar_not_found"
    code, out, _ = run(cli, capsys, "card", "show")
    assert code == 0 and out["card"]["name"] == "Alpha" and out["visible"] is False
    code, out, _ = run(cli, capsys, "card", "clear")
    assert code == 0 and out == {"user_id": f"@alpha:{SERVER}", "card": None}
    assert homeserver_profile(cli) == {"displayname": "alpha", "avatar_url": ""}
    code, out, _ = run(cli, capsys, "card", "show")
    assert code == 0 and out == {"user_id": f"@alpha:{SERVER}", "card": None, "visible": False}
    code, out, _ = run(cli, capsys, "card", "show", f"@nobody:{SERVER}")
    assert code == 0 and out["card"] is None and out["visible"] is None
    code, _, err = run(cli, capsys, "card", "show", "@bee:other.org")
    assert code == 6 and err["error"]["code"] == "federation_refused"


def test_a_card_published_in_the_directory_moves_into_the_profile(cli, project, homeserver,
                                                                  capsys):
    connect(project, allow_write=True)
    me = f"@alpha:{SERVER}"
    homeserver.aliases[DIRECTORY] = "!dir"
    homeserver.joined.add("!dir")
    homeserver.profiles[me] = {"displayname": "alpha (new)"}
    homeserver.state["!dir"] = [
        {"type": "m.room.member", "state_key": me, "sender": me,
         "content": {"membership": "join", "displayname": "alpha (new)"}},
        {"type": "agentrooms.card", "state_key": me, "sender": me,
         "content": {"name": "Alpha", "about": "Plans releases.",
                     "supervisor": f"@operator:{SERVER}"}}]
    # Before it moves, it reads back unchanged.
    code, out, _ = run(cli, capsys, "card", "show")
    assert out["card"] == {"name": "Alpha", "about": "Plans releases.",
                           "supervisor": f"@operator:{SERVER}", "avatar": None}
    # The agent's own token moves it, the card's name over the suffixed one, and
    # its supervisor onto the connection, then clears what the directory held.
    lines = cli._take_up({**cli._cfg(None, "card"), "supervisor": None})
    assert any("moved" in line for line in lines), lines
    assert homeserver_profile(cli) == {"displayname": "Alpha",
                                       "agentrooms.about": "Plans releases.",
                                       "agentrooms.supervisor": f"@operator:{SERVER}"}
    legacy = [e for e in homeserver.state["!dir"] if e["type"] == "agentrooms.card"]
    assert legacy[-1]["content"] == {}
    entry = json.loads((project / "capabilities" / "agentrooms" / "connections.json").read_text())
    assert entry["connections"]["alpha"]["supervisor"] == f"@operator:{SERVER}"
    code, out, _ = run(cli, capsys, "card", "show")
    assert out["card"] == {"name": "Alpha", "about": "Plans releases.",
                           "supervisor": f"@operator:{SERVER}", "avatar": None}
    # Nothing is left to move the second time.
    assert cli._take_up({**cli._cfg(None, "card"), "supervisor": f"@operator:{SERVER}"}) == []


def test_a_room_is_switched_on_and_off_and_opened_rooms_start_as_the_default_says(
        cli, project, homeserver, capsys, monkeypatch):
    connect(project, allow_write=True)
    me = f"@alpha:{SERVER}"
    # A room the agent opens is left to interactive use until its defaults say otherwise.
    code, out, err = run(cli, capsys, "open", "lab", "--invite", f"@b:{SERVER}")
    assert code == 0 and out["served"] is False and homeserver.served["!new"] == \
        {"served": False}, err
    code, out, err = run(cli, capsys, "workers", "defaults", "--opened", "on")
    assert code == 0 and out["opened"] is True, err
    code, out, err = run(cli, capsys, "open", "lab", "--invite", f"@b:{SERVER}")
    assert code == 0 and out["served"] is True and homeserver.served["!new"] == {"served": True}
    # Switching on records the moment, so what arrived while it was off wakes nothing.
    code, _, err = run(cli, capsys, "serve", "!lab", "on")
    assert code == 6 and err["error"]["code"] == "not_member"
    homeserver.joined.add("!lab")
    code, out, err = run(cli, capsys, "serve", "!lab", "off")
    assert code == 0 and out == {"room_id": "!lab", "served": False}, err
    code, out, err = run(cli, capsys, "serve", "!lab", "on")
    assert code == 0 and out == {"room_id": "!lab", "served": True}, err
    assert homeserver.served["!lab"] == {"served": True, "since": 1_700_000_000_003}
    homeserver.encrypted.add("!lab")
    code, _, err = run(cli, capsys, "serve", "!lab", "off")
    assert code == 6 and err["error"]["code"] == "encrypted_room"
    homeserver.encrypted.discard("!lab")
    monkeypatch.setenv("AGENTROOMS_TURN", "t1")
    code, _, err = run(cli, capsys, "serve", "!lab", "off")
    assert code == 4 and err["error"]["code"] == "policy"
    monkeypatch.delenv("AGENTROOMS_TURN")
    connect(project, allow_write=False)
    code, _, err = run(cli, capsys, "serve", "!lab", "off")
    assert code == 4 and err["error"]["code"] == "read_only"
    # `rooms` shows each room's switch; a room with none is served.
    homeserver.syncs.append({"next_batch": "n", "rooms": {"join": {
        "!lab": {"summary": {"m.joined_member_count": 3}, "account_data": {"events": [
            {"type": "agentrooms.served", "content": {"served": False}}]}},
        "!other": {"summary": {"m.joined_member_count": 3}}}}})
    code, out, err = run(cli, capsys, "rooms")
    assert code == 0, err
    assert {r["room_id"]: r["served"] for r in out["joined"]} == {"!lab": False, "!other": True}
    assert "agentrooms.served" in json.loads(homeserver.sync_params[-1]["filter"])[
        "room"]["account_data"]["types"]
