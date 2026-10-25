"""What a person is asked for: declared once, written through the contract.

A capability declares a connection's fields (`CONN_FIELDS`), the machine's
prerequisites (`SETUP_FIELDS`), the ceremonies that link a connection
(`LINK_METHODS`) and where its settings live (`SETTINGS`). The manifest carries
them, the manager validates them, and the contract answers `connection
add|remove`, `setup` and `settings` from them, with a secret taken only from
stdin. A linking verb begun with `_events_begin()` speaks JSON lines.

The capability these tests drive is the manager's own scaffold, stamped with
the canonical contract and given the declarations and one domain verb, `link`,
that asks for a code and answers it.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "declfix"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_declared_fields", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()

DECLARATIONS = '''CONN_FIELDS = [
    {"key": "url", "label": "URL", "help": "The service root.", "type": "url",
     "required": True, "identity": True},
    {"key": "port", "label": "Port", "help": "The port.", "type": "integer",
     "required": False, "default": 443},
    {"key": "secret_env", "label": "Token", "help": "The API token.", "type": "string",
     "secret": True, "required": True, "env": "DECLFIX_{ID}_TOKEN"},
]
SETUP_FIELDS = [
    {"key": "DECLFIX_APP_ID", "label": "App id", "help": "The app id.", "type": "integer",
     "required": True},
    {"key": "DECLFIX_APP_HASH", "label": "App hash", "help": "The app hash.",
     "type": "string", "secret": True, "required": True},
]
SETUP = {"title": "Declfix App",
         "guide": {"url": "https://apps.example/declfix",
                   "steps": ["Sign in.", "Copy App id and App hash here."]},
         "verify": "doctor"}
LINK_METHODS = [
    {"id": "code", "label": "Code", "help": "Enter a code.",
     "argv": ["declfix", "link", "--events"]},
]
SETTINGS = {
    "homes": [
        {"scope": "project", "path": "service/config.toml", "format": "toml",
         "seed": {"version": 1}},
        {"scope": "global", "path": "service/settings.json", "format": "json"},
    ],
    "keys": [
        {"key": "poll_seconds", "label": "Poll", "help": "Seconds between polls.",
         "type": "integer", "default": 60, "minimum": 1, "scope": "project",
         "apply": "reload"},
        {"key": "mode", "label": "Mode", "help": "The mode.", "type": "enum",
         "choices": ["a", "b"], "scope": "global", "apply": "next_call"},
    ],
}


def _settings_check(scope, document):
    if scope == "project":
        unknown = sorted(set(document) - {"version", "poll_seconds", "lanes"})
        if unknown:
            return [f"it names {unknown[0]!r}, which nothing reads"]
    return []
'''

DOMAIN = '''    if cleaned[:1] == ["link"]:
        if "--events" in cleaned:
            _events_begin()
        code = _event_answer("code_needed", retry=False)
        if code == "bad":
            _die(2, "code_invalid", "the code was refused")
        _emit({"ok": True, "code": code})
        return
    if cleaned[:1] == ["prove"]:
        _emit({"recorded": _record_identity(*cleaned[1:])})
        return
    if cleaned == ["doctor"]:'''


def _fixture_text(declared: bool = True) -> str:
    text = M._capability_skeleton(NAME, False)
    text = text.replace(f"  {NAME} inventory [--network]\n",
                        f"  {NAME} inventory [--network]\n  {NAME} link [--events]\n", 1)
    if declared:
        text = text.replace("CONN_FIELDS: list[dict] = []\n", DECLARATIONS, 1)
    text = text.replace('    if cleaned == ["doctor"]:', DOMAIN, 1)
    assert DOMAIN.splitlines()[0] in text
    return text


def _write(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body) + "\n")


@pytest.fixture()
def lab(tmp_path):
    home = tmp_path / "home"
    nowhere = home / "nowhere"
    nowhere.mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "capabilities").mkdir()
    config = tmp_path / "config"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CAPABILITIES_", "CLAUDE_"))}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(config),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CAPABILITIES_BIN": str(tmp_path / "bin")})
    script = tmp_path / "cap" / NAME
    script.parent.mkdir()
    script.write_text(_fixture_text())
    script.chmod(0o755)
    _write(project / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": True}}})
    _write(config / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": True}}})
    return {"tmp": tmp_path, "home": home, "nowhere": nowhere, "project": project,
            "config": config, "env": env, "script": script}


def _run(lab, *args, stdin=None, extra=None, where="project"):
    env = dict(lab["env"])
    env.update(extra or {})
    return subprocess.run([sys.executable, str(lab["script"]), *args],
                          cwd=lab[where], env=env, text=True, input=stdin,
                          capture_output=True, timeout=120)


def _error(result) -> dict:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stdout
    return json.loads(lines[-1])["error"]


def _registry(lab) -> dict:
    return json.loads((lab["project"] / "capabilities" / NAME / "connections.json").read_text())


def _env_file(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out[key] = value.strip("'")
    return out


# --- the declaration -----------------------------------------------------------

def test_manifest_carries_the_declarations_and_validates(lab):
    result = _run(lab, "manifest")
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert [f["key"] for f in manifest["connections"]["fields"]] == ["url", "port", "secret_env"]
    assert [f["key"] for f in manifest["connections"]["setup"]] == ["DECLFIX_APP_ID", "DECLFIX_APP_HASH"]
    assert manifest["connections"]["link"][0]["argv"][-1] == "--events"
    assert manifest["settings"]["homes"][0]["format"] == "toml"
    assert manifest["setup"] == {"title": "Declfix App",
                                 "guide": {"url": "https://apps.example/declfix",
                                           "steps": ["Sign in.", "Copy App id and App hash here."]},
                                 "verify": "doctor"}
    assert M._validate_manifest(NAME, manifest) == []


def test_a_capability_without_declarations_answers_null_and_keeps_its_verbs(lab):
    lab["script"].write_text(_fixture_text(declared=False).replace(
        "CONN_FIELDS: list[dict] = []\n", ""))
    manifest = json.loads(_run(lab, "manifest").stdout)
    assert manifest["connections"] == {"fields": None, "setup": [], "link": []}
    assert manifest["settings"] is None
    assert "setup" not in manifest
    assert M._validate_manifest(NAME, manifest) == []
    # Undeclared, the verbs fall through to the CLI's own parser.
    assert _run(lab, "settings").returncode != 0
    assert "settings_undeclared" not in _run(lab, "settings").stderr


@pytest.mark.parametrize("fields, problem", [
    ([{"key": "a", "label": "A", "help": "h", "identity": "yes"}], "not a boolean"),
    ([{"key": "s_env", "label": "S", "help": "h", "secret": True, "identity": True}],
     "is a secret"),
    ([{"key": "a", "label": "A", "help": "h", "identity": True},
      {"key": "b", "label": "B", "help": "h", "identity": True}], "has one"),
])
def test_a_main_identifier_marks_one_field_that_is_no_secret(lab, fields, problem):
    manifest = json.loads(_run(lab, "manifest").stdout)
    assert [f["key"] for f in manifest["connections"]["fields"] if f.get("identity")] == ["url"]
    manifest["connections"]["fields"] = fields
    problems = M._validate_manifest(NAME, manifest)
    assert any(problem in p for p in problems), problems


@pytest.mark.parametrize("broken, problem", [
    ({"connections": {"fields": [{"key": "x", "label": "X", "help": "h", "type": "colour"}],
                      "setup": [], "link": []}}, "type"),
    ({"connections": {"fields": [{"key": "x", "label": "X", "help": "h", "type": "enum"}],
                      "setup": [], "link": []}}, "choices"),
    ({"connections": {"fields": [{"key": "s", "label": "S", "help": "h", "secret": True,
                                  "env": "NO_ID_HERE"}], "setup": [], "link": []}}, "{ID}"),
    ({"connections": {"fields": [], "setup": [],
                      "link": [{"id": "qr", "label": "QR", "argv": ["x", "pair"]}]}}, "--events"),
    ({"settings": {"homes": [{"scope": "galaxy", "path": "a.json", "format": "json"}],
                   "keys": []}}, "scope"),
    ({"setup": {"title": " "}}, "setup.title"),
    ({"setup": {"guide": {"url": "my.example/apps", "steps": ["Sign in."]}}}, "setup.guide.url"),
    ({"setup": {"guide": {"url": "https://my.example/apps", "steps": []}}}, "setup.guide.steps"),
    ({"setup": {"guide": {"url": "https://my.example/apps"}}}, "setup.guide is not"),
    ({"setup": {"verify": "--check"}}, "setup.verify"),
    ({"setup": {"verify": "doctor", "colour": "red"}}, "unknown key"),
    ({"setup": None}, "setup is not"),
    ({"settings": {"homes": [{"scope": "project", "path": "../a.json", "format": "json"}],
                   "keys": []}}, "path"),
    ({"settings": {"homes": [{"scope": "project", "path": "a.json", "format": "json"}],
                   "keys": [{"key": "k", "label": "K", "help": "h", "scope": "machine",
                             "apply": "reload"}]}}, "no home"),
    ({"settings": {"homes": [{"scope": "project", "path": "a.json", "format": "json"}],
                   "keys": [{"key": "k", "label": "K", "help": "h", "scope": "project",
                             "apply": "whenever"}]}}, "apply"),
])
def test_the_manager_refuses_a_malformed_declaration(lab, broken, problem):
    manifest = json.loads(_run(lab, "manifest").stdout)
    manifest.update(broken)
    problems = M._validate_manifest(NAME, manifest)
    assert any(problem in p for p in problems), problems


# --- connection add | remove ---------------------------------------------------

def test_a_secret_is_never_taken_on_argv(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--field", "secret_env=tok")
    assert result.returncode == 6
    assert _error(result)["code"] == "secret_on_argv"
    assert not (lab["project"] / "capabilities" / NAME / "connections.json").exists()


def test_required_fields_are_named_before_anything_is_written(lab):
    result = _run(lab, "connection", "add", "work", "--field", "port=8443")
    assert result.returncode == 6
    error = _error(result)
    assert error["code"] == "fields_required"
    assert "url" in error["message"] and "secret_env" in error["message"]
    assert not (lab["project"] / ".env.local").exists()


def test_add_writes_the_entry_and_its_secret_and_remove_takes_both(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--field", "port=8443", "--stdin", "--default",
                  stdin=json.dumps({"secret_env": "tok-123456789"}))
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["action"] == "add" and answer["default"] is True
    secret = next(f for f in answer["fields"] if f["key"] == "secret_env")
    assert secret["env"] == "DECLFIX_WORK_TOKEN" and secret["value"] == "…6789"
    assert "tok-123456789" not in result.stdout
    registry = _registry(lab)
    assert registry["default"] == "work"
    assert registry["connections"]["work"] == {
        "url": "https://a.example", "port": 8443, "secret_env": "DECLFIX_WORK_TOKEN"}
    env_local = lab["project"] / ".env.local"
    assert _env_file(env_local) == {"DECLFIX_WORK_TOKEN": "tok-123456789"}
    assert stat.S_IMODE(env_local.stat().st_mode) == 0o600

    # An existing connection is written over only when asked to.
    result = _run(lab, "connection", "add", "work", "--field", "port=9443")
    assert result.returncode == 6 and _error(result)["code"] == "connection_exists"
    assert _registry(lab)["connections"]["work"]["port"] == 8443

    # A replacement keeps every field not given, the secret's pointer included.
    result = _run(lab, "connection", "add", "work", "--field", "port=9443", "--replace")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "update"
    assert _registry(lab)["connections"]["work"]["secret_env"] == "DECLFIX_WORK_TOKEN"
    assert _registry(lab)["connections"]["work"]["port"] == 9443

    result = _run(lab, "connection", "remove", "work")
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["secrets_removed"] == ["DECLFIX_WORK_TOKEN"] and answer["default_cleared"]
    assert "work" not in _registry(lab)["connections"]
    assert "default" not in _registry(lab)
    assert _env_file(env_local) == {}


def test_a_secret_field_can_name_a_key_already_set_and_nothing_is_copied(lab):
    env_local = lab["project"] / ".env.local"
    env_local.write_text("SHARED_TOKEN=tok-shared-0001\n")
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--env", "secret_env=SHARED_TOKEN")
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["credentials"] is None
    secret = next(f for f in answer["fields"] if f["key"] == "secret_env")
    assert secret["env"] == "SHARED_TOKEN" and secret["set"] and secret["value"] == "…0001"
    assert _registry(lab)["connections"]["work"]["secret_env"] == "SHARED_TOKEN"
    assert env_local.read_text() == "SHARED_TOKEN=tok-shared-0001\n"

    # A name the cascade does not resolve is refused, and not echoed.
    result = _run(lab, "connection", "add", "other", "--field", "url=https://a.example",
                  "--env", "secret_env=NOT_SET_ANYWHERE")
    assert result.returncode == 6 and _error(result)["code"] == "env_key_unset"
    assert "NOT_SET_ANYWHERE" not in result.stderr
    assert "other" not in _registry(lab)["connections"]

    # Only a secret field takes a name.
    result = _run(lab, "connection", "add", "other", "--env", "url=SHARED_TOKEN")
    assert result.returncode == 6 and _error(result)["code"] == "input"

    # Removing the connection keeps the key it did not write.
    result = _run(lab, "connection", "remove", "work")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["secrets_kept"] == ["SHARED_TOKEN"]
    assert env_local.read_text() == "SHARED_TOKEN=tok-shared-0001\n"


def test_a_global_connection_keeps_its_secret_in_credentials_env(lab):
    result = _run(lab, "connection", "add", "home", "--global", "--stdin",
                  stdin=json.dumps({"url": "https://b.example", "secret_env": "g-tok-0001"}))
    assert result.returncode == 0, result.stderr
    added = json.loads(result.stdout)   # a global connection still needs a grant here
    assert "hint" in added and added["granted"] is False
    credentials = lab["config"] / NAME / "credentials.env"
    assert _env_file(credentials) == {"DECLFIX_HOME_TOKEN": "g-tok-0001"}
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    held = json.loads((lab["config"] / NAME / "connections.json").read_text())
    assert held["connections"]["home"]["secret_env"] == "DECLFIX_HOME_TOKEN"


def _global_connection(lab, cid: str = "home") -> None:
    result = _run(lab, "connection", "add", cid, "--global", "--stdin",
                  stdin=json.dumps({"url": "https://b.example", "secret_env": "g-tok-0001"}))
    assert result.returncode == 0, result.stderr


def _project_registry(lab, body) -> None:
    _write(lab["project"] / "capabilities" / NAME / "connections.json", body)


def test_a_global_connection_is_written_over_only_with_replace(lab):
    _global_connection(lab)
    for args in (("--global",), ("--machine",)):
        result = _run(lab, "connection", "add", "home", *args, "--field", "port=1")
        assert result.returncode == 6 and _error(result)["code"] == "connection_exists"
    held = json.loads((lab["config"] / NAME / "connections.json").read_text())
    assert "port" not in held["connections"]["home"]
    result = _run(lab, "connection", "add", "home", "--machine", "--replace",
                  "--field", "port=1", where="nowhere")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "update"
    held = json.loads((lab["config"] / NAME / "connections.json").read_text())
    assert held["connections"]["home"]["port"] == 1
    assert held["connections"]["home"]["secret_env"] == "DECLFIX_HOME_TOKEN"


def test_replace_is_only_an_option_of_add(lab):
    _global_connection(lab)
    result = _run(lab, "connection", "remove", "home", "--global", "--replace")
    assert result.returncode == 6 and _error(result)["code"] == "input"


def test_a_machine_connection_this_project_grants_is_not_removed(lab):
    _global_connection(lab)
    _project_registry(lab, {"connections": {"home": {"enabled": True}}})
    for args in (("--global",), ("--machine",)):
        result = _run(lab, "connection", "remove", "home", *args)
        assert result.returncode == 6, result.stderr
        error = _error(result)
        assert error["code"] == "connection_granted"
        assert str(lab["project"]) in error["message"]
        assert "capabilities set declfix grant home" in error["hint"]
    held = json.loads((lab["config"] / NAME / "connections.json").read_text())
    assert "home" in held["connections"]
    assert _env_file(lab["config"] / NAME / "credentials.env") == {
        "DECLFIX_HOME_TOKEN": "g-tok-0001"}

    _project_registry(lab, {"connections": {"home": {"enabled": False}}})
    result = _run(lab, "connection", "remove", "home", "--global")
    assert result.returncode == 0, result.stderr
    held = json.loads((lab["config"] / NAME / "connections.json").read_text())
    assert "home" not in held["connections"]


def test_a_project_declaring_its_own_connection_under_the_id_does_not_hold_the_machines(lab):
    _global_connection(lab)
    _project_registry(lab, {"connections": {"home": {
        "url": "https://own.example", "secret_env": "OWN_TOKEN", "enabled": True}}})
    result = _run(lab, "connection", "remove", "home", "--global")
    assert result.returncode == 0, result.stderr


def test_a_project_connection_is_removed_from_its_own_project(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--stdin", stdin=json.dumps({"secret_env": "tok-1"}))
    assert result.returncode == 0, result.stderr
    _project_registry(lab, {"connections": {"work": {
        **_registry(lab)["connections"]["work"], "enabled": True}}})
    result = _run(lab, "connection", "remove", "work")
    assert result.returncode == 0, result.stderr


# --- machine administration -------------------------------------------------------

def _not_enabled(lab) -> None:
    (lab["config"] / "capabilities" / "settings.json").unlink()


def _quarantine(lab) -> None:
    _write(lab["config"] / "capabilities" / "machine.json",
           {"schema": "capabilities.machine.v1",
            "capabilities": {NAME: {"state": "quarantined", "at": "t", "by": "t"}}})


MACHINE_WRITES = (
    (("setup", "set", "--machine", "--field", "DECLFIX_APP_ID=7"), None),
    (("setup", "unset", "--machine", "DECLFIX_APP_ID"), None),
    (("connection", "add", "home", "--machine", "--stdin"),
     json.dumps({"url": "https://b.example", "secret_env": "g-tok-0001"})),
    (("connection", "remove", "home", "--machine"), None),
)


def test_outside_a_project_setup_is_reported_with_no_policy(lab):
    _not_enabled(lab)
    result = _run(lab, "setup", where="nowhere")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] is False
    result = _run(lab, "setup", "show", where="nowhere")
    assert result.returncode == 0, result.stderr


def test_a_machine_write_with_machine_reads_no_policy_wherever_it_runs(lab):
    _not_enabled(lab)
    for where in ("nowhere", "project"):
        if where == "project":
            _write(lab["project"] / "capabilities" / "settings.json",
                   {"capabilities": {NAME: {"enabled": False}}})
        for args, stdin in MACHINE_WRITES:
            result = _run(lab, *args, stdin=stdin, where=where)
            assert result.returncode == 0, (where, args, result.stderr)
    assert not (lab["config"] / NAME / "credentials.env").read_text().strip()


def test_a_machine_write_without_machine_answers_to_policy(lab):
    _not_enabled(lab)
    (lab["project"] / "capabilities" / "settings.json").unlink()
    for args, stdin in (
            (("setup", "set", "--field", "DECLFIX_APP_ID=7"), None),
            (("setup", "unset", "DECLFIX_APP_ID"), None),
            (("connection", "add", "home", "--global", "--stdin"),
             json.dumps({"url": "https://b.example", "secret_env": "g-tok-0001"})),
            (("connection", "remove", "home", "--global"), None)):
        for where in ("nowhere", "project"):
            result = _run(lab, *args, stdin=stdin, where=where)
            assert result.returncode == 4, (where, args, result.stderr)
            assert _error(result)["code"] == "not_enabled"
    result = _run(lab, "setup", "set", "--field", "DECLFIX_APP_ID=7",
                  extra={"CLAUDE_PROJECT_DIR": str(lab["nowhere"])}, where="nowhere")
    assert result.returncode == 4 and _error(result)["code"] == "not_enabled"


def test_inside_a_project_setup_reporting_answers_to_policy(lab):
    _write(lab["project"] / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": False}}})
    result = _run(lab, "setup")
    assert result.returncode == 4 and _error(result)["code"] == "disabled"


def test_machine_administration_answers_to_the_ceiling(lab):
    _not_enabled(lab)
    _quarantine(lab)
    for args, stdin in ((("setup",), None), *MACHINE_WRITES):
        result = _run(lab, *args, stdin=stdin, where="nowhere")
        assert result.returncode == 4, (args, result.stderr)
        assert _error(result)["code"] == "quarantined"


def test_machine_writes_answer_to_the_read_only_switch(lab):
    _not_enabled(lab)
    switch = {"CAPABILITIES_READ_ONLY": "1"}
    for args, stdin in MACHINE_WRITES:
        result = _run(lab, *args, stdin=stdin, where="nowhere", extra=switch)
        assert result.returncode == 4, (args, result.stderr)
        assert _error(result)["code"] == "read_only_switch"
    result = _run(lab, "setup", where="nowhere", extra=switch)
    assert result.returncode == 0, result.stderr


def test_machine_and_project_are_not_asked_together(lab):
    result = _run(lab, "connection", "add", "home", "--machine", "--project",
                  "--field", "url=https://b.example")
    assert result.returncode == 6 and _error(result)["code"] == "input"


def test_a_value_is_checked_against_its_type(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=ftp://a.example",
                  "--stdin", stdin=json.dumps({"secret_env": "t"}))
    assert result.returncode == 6 and _error(result)["code"] == "bad_field"
    result = _run(lab, "connection", "add", "work", "--field", "colour=red")
    assert result.returncode == 6 and _error(result)["code"] == "unknown_field"


def test_the_read_only_switch_refuses_every_write(lab):
    switch = {"CAPABILITIES_READ_ONLY": "1"}
    for args, stdin in ((("connection", "add", "w", "--field", "url=https://a.example"), None),
                        (("setup", "set", "--field", "DECLFIX_APP_ID=7"), None),
                        (("settings", "set", "poll_seconds", "5"), None)):
        result = _run(lab, *args, stdin=stdin, extra=switch)
        assert result.returncode == 4, (args, result.stderr)
        assert _error(result)["code"] == "read_only_switch"


# --- setup -----------------------------------------------------------------------

def test_setup_reports_and_writes_the_machine_prerequisites(lab):
    shown = json.loads(_run(lab, "setup").stdout)
    assert shown["ready"] is False
    result = _run(lab, "setup", "set", "--field", "DECLFIX_APP_HASH=x")
    assert result.returncode == 6 and _error(result)["code"] == "secret_on_argv"
    result = _run(lab, "setup", "set", "--field", "DECLFIX_APP_ID=7", "--stdin",
                  stdin=json.dumps({"DECLFIX_APP_HASH": "hash-0123456789"}))
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["ready"] is True and answer["changed"] == ["DECLFIX_APP_HASH", "DECLFIX_APP_ID"]
    assert {f["key"]: f["value"] for f in answer["fields"]} == {
        "DECLFIX_APP_ID": "7", "DECLFIX_APP_HASH": "…6789"}
    assert _env_file(lab["config"] / NAME / "credentials.env") == {
        "DECLFIX_APP_ID": "7", "DECLFIX_APP_HASH": "hash-0123456789"}
    answer = json.loads(_run(lab, "setup", "unset", "DECLFIX_APP_HASH").stdout)
    assert answer["ready"] is False


def test_setup_names_itself_the_guide_to_its_fields_and_its_check(lab):
    presentation = {"title": "Declfix App",
                    "guide": {"url": "https://apps.example/declfix",
                              "steps": ["Sign in.", "Copy App id and App hash here."]},
                    "verify": "doctor"}
    for args, stdin in ((("setup",), None),
                        (("setup", "set", "--field", "DECLFIX_APP_ID=7"), None),
                        (("setup", "unset", "DECLFIX_APP_ID"), None)):
        result = _run(lab, *args, stdin=stdin)
        assert result.returncode == 0, (args, result.stderr)
        answer = json.loads(result.stdout)
        assert {k: answer[k] for k in presentation} == presentation, args
    # Each key is carried only where it is declared.
    lab["script"].write_text(_fixture_text().replace(
        '         "verify": "doctor"}\n', '         }\n', 1).replace(
        'SETUP = {"title": "Declfix App",', 'SETUP = {', 1))
    answer = json.loads(_run(lab, "setup").stdout)
    assert "title" not in answer and "verify" not in answer
    assert answer["guide"] == presentation["guide"]
    assert json.loads(_run(lab, "manifest").stdout)["setup"] == {"guide": presentation["guide"]}
    lab["script"].write_text(_fixture_text().replace("SETUP = {", "_UNUSED = {", 1))
    answer = json.loads(_run(lab, "setup").stdout)
    assert not set(presentation) & set(answer)
    assert "setup" not in json.loads(_run(lab, "manifest").stdout)



def test_a_shared_setup_field_is_kept_once_for_the_machine(lab):
    # A capability that declares no shared field answers as before.
    plain = json.loads(_run(lab, "setup").stdout)
    assert "shared" not in plain and "shared_credentials" not in plain
    lab["script"].write_text(_fixture_text().replace(
        '"type": "string", "secret": True, "required": True},\n]\nSETUP = ',
        '"type": "string", "secret": True, "required": True,\n'
        '     "shared": "DECLFIX_SHARED_HASH"},\n]\nSETUP = ', 1))
    shared_env = lab["config"] / "capabilities" / "shared.env"
    own_env = lab["config"] / NAME / "credentials.env"
    # A value kept the older way, in the capability's own file, still resolves.
    own_env.parent.mkdir(parents=True, exist_ok=True)
    own_env.write_text("DECLFIX_APP_HASH='own-hash-0000aaaa'\n")
    shown = json.loads(_run(lab, "setup").stdout)
    assert [f["key"] for f in shown["fields"]] == ["DECLFIX_APP_ID"]
    assert [f["key"] for f in shown["shared"]] == ["DECLFIX_APP_HASH"]
    assert shown["shared"][0]["shared"] == "DECLFIX_SHARED_HASH"
    assert shown["shared_credentials"] == str(shared_env)
    assert (shown["shared"][0]["value"], shown["shared"][0]["source"]) == ("…aaaa", str(own_env))
    # A write lands once, in the machine's shared file, under the shared name.
    result = _run(lab, "setup", "set", "--machine", "--stdin",
                  stdin=json.dumps({"DECLFIX_APP_HASH": "shared-hash-1111bbbb"}))
    assert result.returncode == 0, result.stderr
    assert _env_file(shared_env) == {"DECLFIX_SHARED_HASH": "shared-hash-1111bbbb"}
    assert _env_file(own_env) == {"DECLFIX_APP_HASH": "own-hash-0000aaaa"}
    answer = json.loads(result.stdout)
    assert answer["changed"] == ["DECLFIX_APP_HASH"]
    assert (answer["shared"][0]["tier"], answer["shared"][0]["source"]) == ("user", str(shared_env))
    result = _run(lab, "setup", "show", "--reveal", "DECLFIX_APP_HASH")
    assert result.stdout == "shared-hash-1111bbbb\n"
    # The project's own value still wins over the machine's.
    (lab["project"] / ".env.local").write_text("DECLFIX_APP_HASH='proj-hash-2222cccc'\n")
    assert json.loads(_run(lab, "setup").stdout)["shared"][0]["tier"] == "project"
    (lab["project"] / ".env.local").unlink()
    # Unset takes it out of both files, so the older value does not come back.
    answer = json.loads(_run(lab, "setup", "unset", "DECLFIX_APP_HASH").stdout)
    assert answer["shared"][0]["set"] is False
    assert _env_file(shared_env) == {} and _env_file(own_env) == {}


def test_setup_show_reveals_one_value_in_the_clear_and_nothing_else(lab):
    result = _run(lab, "setup", "set", "--machine", "--field", "DECLFIX_APP_ID=7", "--stdin",
                  stdin=json.dumps({"DECLFIX_APP_HASH": "hash-0123456789"}))
    assert result.returncode == 0, result.stderr
    # Outside any project it reads the machine's value, with no policy read.
    _not_enabled(lab)
    result = _run(lab, "setup", "show", "--reveal", "DECLFIX_APP_HASH", where="nowhere")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hash-0123456789\n"
    # In a project its own value wins, as the masked report resolves it.
    (lab["project"] / ".env.local").write_text("DECLFIX_APP_HASH='proj-hash-000111'\n")
    _write(lab["config"] / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": True}}})
    result = _run(lab, "setup", "--reveal", "DECLFIX_APP_HASH")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "proj-hash-000111\n"
    assert {f["key"]: f["value"] for f in json.loads(_run(lab, "setup").stdout)["fields"]} == {
        "DECLFIX_APP_ID": "7", "DECLFIX_APP_HASH": "…0111"}
    result = _run(lab, "setup", "show", "--reveal", "DECLFIX_OTHER")
    assert (result.returncode, _error(result)["code"]) == (6, "unknown_field")
    assert result.stdout == ""
    _run(lab, "setup", "unset", "DECLFIX_APP_ID")
    result = _run(lab, "setup", "show", "--reveal", "DECLFIX_APP_ID")
    assert (result.returncode, _error(result)["code"]) == (3, "not_set")
    assert result.stdout == ""
    result = _run(lab, "setup", "set", "--reveal", "DECLFIX_APP_ID")
    assert result.returncode == 6 and _error(result)["code"] == "input"


def test_connections_show_reveals_one_value_of_a_usable_connection(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--stdin", stdin=json.dumps({"secret_env": "tok-123456789"}))
    assert result.returncode == 0, result.stderr
    result = _run(lab, "connections", "show", "work", "--reveal", "secret_env")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "tok-123456789\n"
    result = _run(lab, "connections", "show", "work", "--reveal", "url")
    assert result.returncode == 0 and result.stdout == "https://a.example\n"
    # Every other output stays masked.
    assert "tok-123456789" not in _run(lab, "connections").stdout
    for args, exit_code, code in (
            (("work", "--reveal", "colour"), 6, "unknown_field"),
            (("work", "--reveal", "port"), 3, "not_set"),
            (("other", "--reveal", "secret_env"), 6, "unknown_connection"),
            (("work",), 6, "input")):
        result = _run(lab, "connections", "show", *args)
        assert (result.returncode, _error(result)["code"]) == (exit_code, code), args
        assert result.stdout == ""


def _identities(lab) -> dict:
    result = _run(lab, "connections")
    assert result.returncode == 0, result.stderr
    return {cid: entry["identity"]
            for cid, entry in json.loads(result.stdout)["connections"].items()}


def test_an_identity_is_set_by_hand_and_listed_from_the_record(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--identity", "4242", "--display-name", "Example User @example",
                  "--stdin", stdin=json.dumps({"secret_env": "tok-123456789"}))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["identity"] == {"id": "4242", "display_name": "Example User @example"}
    assert _registry(lab)["connections"]["work"]["identity"] == {
        "id": "4242", "display_name": "Example User @example"}
    assert _identities(lab) == {"work": {"id": "4242", "display_name": "Example User @example"}}
    # --display-name alone renames the identity recorded; other fields stay.
    result = _run(lab, "connection", "add", "work", "--replace", "--display-name=Example Renamed")
    assert result.returncode == 0, result.stderr
    assert _identities(lab) == {"work": {"id": "4242", "display_name": "Example Renamed"}}
    assert _registry(lab)["connections"]["work"]["url"] == "https://a.example"
    # Cleared, the main identifier field's value is the id.
    result = _run(lab, "connection", "add", "work", "--replace", "--identity", "")
    assert result.returncode == 0, result.stderr
    assert "identity" not in _registry(lab)["connections"]["work"]
    assert _identities(lab) == {"work": {"id": "https://a.example", "display_name": None}}
    result = _run(lab, "connection", "add", "work", "--replace", "--display-name", "X")
    assert result.returncode == 6 and _error(result)["code"] == "input"
    result = _run(lab, "connection", "remove", "work", "--identity", "1")
    assert result.returncode == 6 and _error(result)["code"] == "input"


def test_a_connection_with_no_identity_lists_null_and_keeps_working(lab):
    _project_registry(lab, {"default": "old", "connections": {
        "old": {"port": 1, "secret_env": "DECLFIX_OLD_TOKEN"}}})
    assert _identities(lab) == {"old": None}
    result = _run(lab, "connections", "show", "old", "--reveal", "port")
    assert result.returncode == 0 and result.stdout == "1\n"


def test_a_proven_identity_is_recorded_where_it_changes_and_never_fails(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--stdin", stdin=json.dumps({"secret_env": "tok-123456789"}))
    assert result.returncode == 0, result.stderr
    result = _run(lab, "prove", "work", "4242", "Example User @example")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"recorded": True}
    assert _identities(lab) == {"work": {"id": "4242", "display_name": "Example User @example"}}
    path = lab["project"] / "capabilities" / NAME / "connections.json"
    before = path.stat().st_mtime_ns
    assert json.loads(_run(lab, "prove", "work", "4242", "Example User @example").stdout) == {"recorded": True}
    assert path.stat().st_mtime_ns == before
    # Nothing is written under the read-only switch, nor for a connection no record holds.
    switch = {"CAPABILITIES_READ_ONLY": "1"}
    result = _run(lab, "prove", "work", "9", "Eve", extra=switch)
    assert result.returncode == 0 and json.loads(result.stdout) == {"recorded": False}
    result = _run(lab, "prove", "ghost", "9", "Eve")
    assert result.returncode == 0 and json.loads(result.stdout) == {"recorded": False}
    # Nor where the record cannot be written, and the verb still answers.
    path.chmod(0o444)
    try:
        result = _run(lab, "prove", "work", "9", "Eve")
    finally:
        path.chmod(0o644)
    assert result.returncode == 0 and json.loads(result.stdout) == {"recorded": False}, result.stderr
    assert _identities(lab) == {"work": {"id": "4242", "display_name": "Example User @example"}}


def test_a_reveal_is_not_safe_discovery(lab):
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--stdin", stdin=json.dumps({"secret_env": "tok-123456789"}))
    assert result.returncode == 0, result.stderr
    # A request whose verb list names other verbs reads the report, not the value.
    auth = {"CAPABILITIES_AUTH_CONTEXT": json.dumps(
        {"allowed_capabilities": {NAME: {"verbs": ["doctor"]}}})}
    assert _run(lab, "connections", extra=auth).returncode == 0
    result = _run(lab, "connections", "show", "work", "--reveal", "secret_env", extra=auth)
    assert result.returncode == 4 and _error(result)["code"] == "verb_not_authorized"
    assert result.stdout == ""
    # A capability this project disabled reports its connections and reveals nothing.
    _write(lab["project"] / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": False}}})
    assert _run(lab, "connections").returncode == 0
    result = _run(lab, "connections", "show", "work", "--reveal", "secret_env")
    assert result.returncode == 4 and _error(result)["code"] == "disabled"
    assert result.stdout == ""


def test_a_reveal_is_gated_where_connection_is_lifted_before_dispatch(lab):
    # A capability that lifts `--connection <id>` out of argv before `_contract`
    # dispatches `connections show` from words the raw argv puts apart.
    text = lab["script"].read_text()
    lifted = ('    _contract([a for i, a in enumerate(argv) if a != "--connection"'
              ' and not (i and argv[i - 1] == "--connection")'
              ' and not a.startswith("--connection=")])\n')
    assert text.count("    _contract(argv)\n") == 1
    lab["script"].write_text(text.replace("    _contract(argv)\n", lifted, 1))
    result = _run(lab, "connection", "add", "work", "--field", "url=https://a.example",
                  "--stdin", stdin=json.dumps({"secret_env": "tok-123456789"}))
    assert result.returncode == 0, result.stderr
    forms = (["connections", "--connection", "work", "show", "work"],
             ["connections", "--connection=work", "show", "work"])
    for form in forms:
        result = _run(lab, *form, "--reveal", "secret_env")
        assert result.returncode == 0 and result.stdout == "tok-123456789\n", result.stderr
    auth = {"CAPABILITIES_AUTH_CONTEXT": json.dumps(
        {"allowed_capabilities": {NAME: {"verbs": ["doctor"]}}})}
    for form in forms:
        result = _run(lab, *form, "--reveal", "secret_env", extra=auth)
        assert result.returncode == 4 and _error(result)["code"] == "verb_not_authorized"
        assert result.stdout == ""
    _write(lab["project"] / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": False}}})
    for form in forms:
        result = _run(lab, *form, "--reveal", "secret_env")
        assert result.returncode == 4 and _error(result)["code"] == "disabled"
        assert result.stdout == ""


# --- settings --------------------------------------------------------------------

def test_settings_write_toml_from_its_seed_and_read_it_back_whole(lab):
    result = _run(lab, "settings", "set", "poll_seconds", "30")
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["apply"] == "reload" and answer["scope"] == "project"
    result = _run(lab, "settings", "set", "lanes.implementation.max_parallel", "2",
                  "--scope", "project")
    assert result.returncode == 0, result.stderr
    path = lab["project"] / "capabilities" / NAME / "service" / "config.toml"
    assert tomllib.loads(path.read_text()) == {
        "version": 1, "poll_seconds": 30, "lanes": {"implementation": {"max_parallel": 2}}}
    shown = json.loads(_run(lab, "settings").stdout)
    assert shown["homes"][0]["document"]["poll_seconds"] == 30
    assert {(k["key"], k["value"], k["from"]) for k in shown["keys"]} == {
        ("poll_seconds", 30, "project"), ("mode", None, "default")}
    assert _run(lab, "settings", "get", "poll_seconds").stdout.strip() == "30"


def test_settings_refuse_what_the_declaration_or_the_capability_refuses(lab):
    result = _run(lab, "settings", "set", "poll_seconds", "0")
    assert result.returncode == 6 and _error(result)["code"] == "settings_invalid"
    result = _run(lab, "settings", "set", "bogus", "1", "--scope", "project")
    assert result.returncode == 6
    error = _error(result)
    assert error["code"] == "settings_invalid" and "bogus" in error["message"]
    assert ".tmp" not in error["message"]
    assert not (lab["project"] / "capabilities" / NAME / "service" / "config.toml").exists()
    result = _run(lab, "settings", "set", "mode", "c")
    assert result.returncode == 6 and _error(result)["code"] == "settings_invalid"


def test_settings_put_replaces_one_scope_and_unset_removes_a_key(lab):
    result = _run(lab, "settings", "put", "--scope", "global",
                  stdin=json.dumps({"mode": "b", "extra": {"x": 1}}))
    assert result.returncode == 0, result.stderr
    path = lab["config"] / NAME / "service" / "settings.json"
    assert json.loads(path.read_text()) == {"mode": "b", "extra": {"x": 1}}
    assert _run(lab, "settings", "unset", "extra.x", "--scope", "global").returncode == 0
    assert json.loads(path.read_text()) == {"mode": "b", "extra": {}}
    result = _run(lab, "settings", "unset", "nope", "--scope", "global")
    assert result.returncode == 3


# --- linking events --------------------------------------------------------------

def test_a_linking_verb_speaks_json_lines_and_reads_its_answers(lab):
    result = _run(lab, "link", "--events", stdin="12345\n")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events == [{"event": "code_needed", "retry": False},
                      {"event": "done", "result": {"ok": True, "code": "12345"}}]


def test_a_refusal_is_the_last_event_and_still_reaches_stderr(lab):
    result = _run(lab, "link", "--events", stdin="bad\n")
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1] == {"event": "error", "exit": 2,
                          "error": {"code": "code_invalid", "message": "the code was refused"}}
    assert _error(result)["code"] == "code_invalid"


def test_stdin_closing_while_an_answer_is_awaited_is_input_closed(lab):
    result = _run(lab, "link", "--events", stdin="")
    assert result.returncode == 6
    assert _error(result)["code"] == "input_closed"


def test_help_ends_with_the_declared_verbs(lab):
    text = _run(lab, "help").stdout
    for needle in ("connection add <id>", "--replace", "--machine", "setup set",
                   "setup [show] [--reveal <KEY>]", "connections show <id> --reveal <KEY>",
                   "settings put", "declfix link --events", "poll_seconds @project",
                   "--identity ID [--display-name TEXT]", "main identifier"):
        assert needle in text, needle


def test_help_names_where_this_project_keeps_its_connections(lab):
    text = _run(lab, "help").stdout
    home = text.split("═══ Where connections live", 1)[1]
    assert "This project keeps its connections in files" in home
    assert str(lab["project"] / "capabilities" / NAME / "connections.json") in home
    assert str(lab["config"] / NAME / "connections.json") in home
    outside = _run(lab, "help", where="nowhere").stdout
    assert "There is no project here" in outside
    assert str(lab["config"] / NAME / "connections.json") in outside


def test_help_of_a_capability_without_connections_names_no_home(lab):
    lab["script"].write_text(M._capability_skeleton(NAME, True))
    result = _run(lab, "help")
    assert result.returncode == 0, result.stderr
    assert "Where connections live" not in result.stdout
