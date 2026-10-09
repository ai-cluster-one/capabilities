"""A process started under CAPABILITIES_READ_ONLY reads and changes nothing.

The switch is decided once, in the store tier every capability carries, and
consulted everywhere a write can happen: a connection's write gate, the
project's records, the manager's record-writing verbs. These tests prove it
where it is decided and where a real capability and the real manager consult
it, against an HTTP instance that is nobody's and a project that exists only
for the test. They also prove the switch left off - unset, `0`, `false` -
changes nothing that worked before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
COOLIFY = REPO / "capabilities" / "coolify" / "bin" / "coolify"

sys.path.insert(0, str(REPO / "contract"))
import store as S  # noqa: E402

SWITCH = "CAPABILITIES_READ_ONLY"
ON = ("1", "true", "TRUE", " True ")
OFF = (None, "", "0", "false", "FALSE")


@pytest.fixture(autouse=True)
def _switch_starts_off(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


def _with_switch(env: dict, value: str | None) -> dict:
    env = dict(env)
    env.pop(SWITCH, None)
    if value is not None:
        env[SWITCH] = value
    return env


def _error(result: subprocess.CompletedProcess) -> dict:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stdout
    return json.loads(lines[-1])["error"]


# --- where it is decided ------------------------------------------------------

@pytest.mark.parametrize("value", ON)
def test_the_switch_is_on_for_one_and_true_in_any_case(monkeypatch, value):
    monkeypatch.setenv(SWITCH, value)
    assert S.read_only_switch() is True


@pytest.mark.parametrize("value", OFF)
def test_the_switch_is_off_unset_zero_or_false(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(SWITCH, value)
    assert S.read_only_switch() is False


def test_record_writes_refuse_and_leave_the_file_as_it_was(tmp_path, monkeypatch):
    envelope = tmp_path / "capabilities"
    envelope.mkdir()
    (envelope / "project.json").write_text(json.dumps({"slug": "lab", "id": "p"}))
    records = S.open_records(envelope, tmp_path / "config")
    records.set("thing", "identifier", "kept", "before")
    identifiers = envelope / "thing" / "identifiers.json"
    written = identifiers.read_text()
    monkeypatch.setenv(SWITCH, "true")
    for write in (lambda: records.set("thing", "identifier", "new", 1),
                  lambda: records.delete("thing", "identifier", "kept"),
                  lambda: records.set("thing", "grant", "c", {"allow_write": True}),
                  lambda: records.set("capabilities", "policy", "thing", {"enabled": True}),
                  lambda: records.document_put("thing", "reference.new", "x")):
        with pytest.raises(S.StoreError) as refused:
            write()
        assert refused.value.slug == "read_only_switch"
    assert identifiers.read_text() == written
    assert records.get("thing", "identifier", "kept") == "before"


@pytest.mark.parametrize("write_default", [True, False])
def test_every_connection_resolves_read_only_under_the_switch(tmp_path, monkeypatch, write_default):
    envelope = tmp_path / "capabilities"
    (envelope / "thing").mkdir(parents=True)
    (envelope / "project.json").write_text(json.dumps({"slug": "lab", "id": "p"}))
    (envelope / "thing" / "connections.json").write_text(json.dumps({"connections": {
        "granted": {"url": "http://a.example", "allow_write": True},
        "silent": {"url": "http://b.example"},
    }}))
    records = S.open_records(envelope, tmp_path / "config")
    before = records.connections("thing", write_default=write_default)
    assert before["granted"]["allow_write"] is True
    assert before["silent"]["allow_write"] is write_default
    monkeypatch.setenv(SWITCH, "1")
    after = records.connections("thing", write_default=write_default)
    assert {cid: entry["allow_write"] for cid, entry in after.items()} == {
        "granted": False, "silent": False}


# --- a real connection-bearing capability ------------------------------------

class _Instance(BaseHTTPRequestHandler):
    """A self-hosted instance that is nobody's: it answers every read with an
    empty list and every write with an empty result, and remembers each."""

    seen: list

    def _answer(self, body) -> None:
        self.seen.append((self.command, self.path.split("?", 1)[0]))
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._answer([])

    def do_POST(self):  # noqa: N802
        self._answer({"deployments": []})

    def log_message(self, *_args):
        pass


@pytest.fixture()
def instance():
    seen: list = []
    handler = type("Handler", (_Instance,), {"seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()


@pytest.fixture()
def lab(tmp_path, instance):
    url, seen = instance
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "coolify").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"coolify": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": str(uuid.uuid4()), "slug": "lab"}))
    (envelope / "coolify" / "connections.json").write_text(json.dumps({
        "default": "box",
        "connections": {"box": {"base_url": url, "secret_env": "COOLIFY_TOKEN",
                                "allow_write": True}}}))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "COOLIFY_TOKEN": "test-token",
    })
    for leaked in (SWITCH, "COOLIFY_BASE_URL", "VIRTUAL_ENV",
                   "CAPABILITIES_PROJECT_ENVELOPE", "AGENTKIT_DB_URL",
                   "AGENTKIT_DB_HOST"):
        env.pop(leaked, None)
    return {"project": project, "envelope": envelope, "env": env, "seen": seen}


def _coolify(lab, *args: str, switch: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(COOLIFY), *args], cwd=lab["project"],
                          env=_with_switch(lab["env"], switch), text=True,
                          capture_output=True, timeout=180)


@pytest.mark.parametrize("value", ["1", "true"])
def test_a_write_verb_is_refused_and_a_read_verb_works(lab, value):
    read = _coolify(lab, "resources", switch=value)
    assert read.returncode == 0, read.stdout + read.stderr
    assert ("GET", "/api/v1/resources") in lab["seen"]

    write = _coolify(lab, "deploy", "some-app", switch=value)
    assert write.returncode == 4, write.stdout + write.stderr
    error = _error(write)
    assert error["code"] == "read_only_switch"
    assert SWITCH in error["message"]
    assert "ask the user" in error["hint"]
    assert not [call for call in lab["seen"] if call[0] != "GET"]


def test_the_switch_refusal_is_distinct_from_a_connections_own_grant(lab):
    registry = lab["envelope"] / "coolify" / "connections.json"
    body = json.loads(registry.read_text())
    body["connections"]["box"]["allow_write"] = False
    registry.write_text(json.dumps(body))
    own = _coolify(lab, "deploy", "some-app")
    assert own.returncode == 4
    assert _error(own)["code"] == "read_only"
    assert SWITCH not in _error(own)["message"]
    switched = _coolify(lab, "deploy", "some-app", switch="1")
    assert _error(switched)["code"] == "read_only_switch"


@pytest.mark.parametrize("value", OFF)
def test_with_the_switch_off_the_write_reaches_the_instance(lab, value):
    write = _coolify(lab, "deploy", "some-app", switch=value)
    assert write.returncode == 0, write.stdout + write.stderr
    assert ("POST", "/api/v1/deploy") in lab["seen"]


def test_connections_names_the_switch_and_marks_every_connection_read_only(lab):
    report = json.loads(_coolify(lab, "connections", switch="1").stdout)
    assert report["connections"]["box"]["allow_write"] is False
    assert report["read_only_switch"] == {
        "variable": SWITCH, "on": True, "effect": S.READ_ONLY_EFFECT}


@pytest.mark.parametrize("value", OFF)
def test_connections_is_unchanged_with_the_switch_off(lab, value):
    unset = _coolify(lab, "connections")
    report = _coolify(lab, "connections", switch=value)
    assert report.stdout == unset.stdout
    parsed = json.loads(report.stdout)
    assert "read_only_switch" not in parsed
    assert parsed["connections"]["box"]["allow_write"] is True


def test_identifiers_refuse_under_the_switch_and_still_read(lab):
    assert _coolify(lab, "ids", "set", "team", "one").returncode == 0
    for args in (("ids", "set", "team", "two"), ("ids", "rm", "team")):
        refused = _coolify(lab, *args, switch="1")
        assert refused.returncode == 4, refused.stderr
        assert _error(refused)["code"] == "read_only_switch"
    listed = _coolify(lab, "ids", "list", switch="1")
    assert json.loads(listed.stdout)["team"]["value"] == "one"


# --- the manager --------------------------------------------------------------

def _manager(lab, *args: str, switch: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(MANAGER), *args], cwd=lab["project"],
                          env=_with_switch(lab["env"], switch), text=True,
                          capture_output=True, timeout=180)


@pytest.mark.parametrize("args", [
    ("enable", "coolify", "--project"),
    ("disable", "coolify", "--project"),
    ("inherit", "coolify", "--project"),
    ("set", "coolify", "grant", "box", '{"allow_write": false}'),
    ("init",),
    ("relabel", "renamed"),
])
def test_the_manager_refuses_its_record_writing_verbs(lab, args):
    before = {path: path.read_text() for path in lab["envelope"].rglob("*.json")}
    result = _manager(lab, *args, switch="1")
    assert result.returncode == 4, result.stdout + result.stderr
    error = _error(result)
    assert error["code"] == "read_only_switch"
    assert f"capabilities {args[0]}" in error["message"]
    assert {path: path.read_text() for path in lab["envelope"].rglob("*.json")} == before


def test_the_manager_writes_as_before_with_the_switch_off(lab):
    for value in OFF:
        result = _manager(lab, "set", "coolify", "grant", "box", '{"allow_write": true}',
                          switch=value)
        assert result.returncode == 0, result.stdout + result.stderr


def test_doctor_reports_the_switch_only_while_it_is_on(lab):
    on = json.loads(_manager(lab, "doctor", switch="1").stdout)
    assert on["read_only_switch"]["variable"] == SWITCH
    assert on["read_only_switch"]["on"] is True
    assert on["read_only_switch"]["effect"] == S.READ_ONLY_EFFECT
    for value in OFF:
        off = json.loads(_manager(lab, "doctor", switch=value).stdout)
        assert "read_only_switch" not in off


def test_the_manager_help_names_the_variable():
    help_text = subprocess.run([str(MANAGER), "help"], text=True,
                               capture_output=True, timeout=60).stdout
    assert SWITCH in help_text
