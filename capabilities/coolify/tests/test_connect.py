#!/usr/bin/env python3
"""Tests for `coolify connect` and for reading credentials files with the
contract's parser.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_connect.py
"""

import io
import json
import os
import re
import stat
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
_code = _coolify_path.read_text()
coolify_module = types.ModuleType("coolify_connect")
coolify_module.__file__ = str(_coolify_path)
exec(_code, coolify_module.__dict__)
sys.modules["coolify_connect"] = coolify_module

TOKEN = "7|AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefgh"
URL = "https://coolify.example.com"


class Probe:
    """A Coolify that answers the doctor probe, recording what reached it."""

    def __init__(self, version="4.3.23", fail=None):
        self.version, self.fail, self.seen = version, fail, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.url.path, request.headers.get("authorization")))
        if self.fail:
            raise httpx.ConnectError(self.fail)
        if request.url.path.endswith("/version"):
            return httpx.Response(200, text=self.version)
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0, "name": "Root Team"})
        return httpx.Response(200, json=[])

    def client(self, conn):
        return httpx.Client(base_url=conn["base_url"] + coolify_module.API_VERSION,
                            transport=httpx.MockTransport(self.handler),
                            headers={"Authorization": f"Bearer {conn['token']}"})


@pytest.fixture
def place(tmp_path, monkeypatch):
    """A project with a capabilities envelope and its own config home."""
    root = tmp_path / "project"
    (root / "capabilities").mkdir(parents=True)
    config = tmp_path / "config"
    monkeypatch.setattr(coolify_module, "_CONFIG_HOME", config)
    monkeypatch.setattr(coolify_module, "CREDENTIALS_ENV",
                        config / "coolify" / "credentials.env")
    monkeypatch.setattr(coolify_module, "_RECORDS", None)
    monkeypatch.setattr(coolify_module, "_project_root", lambda: root)
    monkeypatch.setattr(coolify_module, "_project_capabilities_dir",
                        lambda r: r / "capabilities")
    monkeypatch.setattr(coolify_module, "_gate", lambda: None)
    monkeypatch.delenv("CAPABILITIES_READ_ONLY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    for key in [key for key in os.environ if key.startswith("AGENTKIT_DB_")]:
        monkeypatch.delenv(key)
    return types.SimpleNamespace(root=root, config=config,
                                 registry=root / "capabilities" / "coolify" / "connections.json",
                                 env_local=root / ".env.local",
                                 global_registry=config / "coolify" / "connections.json",
                                 global_credentials=config / "coolify" / "credentials.env")


def _run(argv, probe=None, stdin=""):
    """Run the CLI in-process; return (exit code, stdout, stderr, child argv)."""
    probe = probe or Probe()
    children = []
    real_popen = subprocess.Popen

    def record_popen(args, *a, **k):
        children.append(list(args) if not isinstance(args, str) else [args])
        return real_popen(args, *a, **k)

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (
        patch.object(sys, "argv", ["coolify", *argv]),
        patch.object(sys, "stdin", io.StringIO(stdin)),
        patch.object(sys, "stdout", out),
        patch.object(sys, "stderr", err),
        patch.object(coolify_module, "_client", probe.client),
        patch.object(subprocess, "Popen", record_popen),
    ):
        coolify_module._RECORDS = None
        try:
            coolify_module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue(), children


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_no_token(*texts):
    for text in texts:
        assert TOKEN not in text
        assert TOKEN.split("|", 1)[1] not in text


def test_connect_from_stdin_writes_entry_token_and_probes(place):
    probe = Probe()
    code, out, err, children = _run(
        ["connect", "exp", "--url", URL + "/", "--token-stdin", "--project"],
        probe, stdin=TOKEN + "\n")

    assert code == 0, err
    registry = json.loads(place.registry.read_text())
    assert registry["connections"]["exp"] == {
        "base_url": URL, "secret_env": "COOLIFY_EXP_TOKEN"}
    assert "default" not in registry
    assert place.env_local.read_text() == f"COOLIFY_EXP_TOKEN='{TOKEN}'\n"
    assert _mode(place.env_local) == 0o600
    answer = json.loads(out)
    assert answer["ok"] is True
    assert answer["scope"] == "project"
    assert answer["credentials"] == str(place.env_local)
    assert answer["registry"] == str(place.registry)
    assert answer["doctor"]["version"] == "4.3.23"
    assert ("/api/v1/version", f"Bearer {TOKEN}") in probe.seen
    _assert_no_token(out, err, json.dumps(children))
    assert not place.global_credentials.exists()


def test_connect_from_a_file(place, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    code, out, err, _ = _run(
        ["connect", "exp", "--url", URL, "--token-file", str(token_file)])

    assert code == 0, err
    assert coolify_module._parse_env_file(place.env_local)["COOLIFY_EXP_TOKEN"] == TOKEN
    _assert_no_token(out, err)


def test_connect_from_an_env_key_in_the_process(place, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", TOKEN)
    code, out, err, _ = _run(
        ["connect", "exp", "--url", URL, "--token-env", "SOME_TOKEN", "--project"])

    assert code == 0, err
    assert coolify_module._parse_env_file(place.env_local)["COOLIFY_EXP_TOKEN"] == TOKEN
    _assert_no_token(out, err)


def test_connect_token_env_resolves_through_the_files_too(place):
    """Re-pairing on a new URL reads the token the first pairing wrote."""
    place.env_local.write_text(f"COOLIFY_EXP_TOKEN='{TOKEN}'\n")
    code, out, err, _ = _run(
        ["connect", "exp", "--url", "https://new.example.com",
         "--token-env", "COOLIFY_EXP_TOKEN"])

    assert code == 0, err
    assert json.loads(place.registry.read_text())["connections"]["exp"]["base_url"] \
        == "https://new.example.com"
    assert place.env_local.read_text() == f"COOLIFY_EXP_TOKEN='{TOKEN}'\n"


def test_connect_global_writes_the_config_home_at_0600(place, monkeypatch):
    monkeypatch.setattr(coolify_module, "_project_root", lambda: None)
    code, out, err, _ = _run(
        ["connect", "main", "--url", URL, "--token-stdin", "--global", "--default"],
        stdin=TOKEN)

    assert code == 0, err
    registry = json.loads(place.global_registry.read_text())
    assert registry == {"connections": {"main": {
        "base_url": URL, "secret_env": "COOLIFY_MAIN_TOKEN"}}, "default": "main"}
    assert _mode(place.global_credentials) == 0o600
    assert coolify_module._parse_env_file(place.global_credentials)["COOLIFY_MAIN_TOKEN"] == TOKEN
    assert not place.env_local.exists() and not place.registry.exists()
    answer = json.loads(out)
    assert answer["scope"] == "global" and answer["default"] is True
    _assert_no_token(out, err)


def test_connect_global_from_inside_a_project_names_the_grant(place):
    code, out, err, _ = _run(
        ["connect", "main", "--url", URL, "--token-stdin", "--global"], stdin=TOKEN)

    assert code == 0, err
    answer = json.loads(out)
    assert answer["usable_here"] is False
    assert "capabilities set coolify grant main" in answer["hint"]
    assert not place.registry.exists()


def test_connect_global_outside_a_project_is_usable_by_the_machine_reads(place, monkeypatch):
    monkeypatch.setattr(coolify_module, "_project_root", lambda: None)
    code, out, err, _ = _run(
        ["connect", "main", "--url", URL, "--token-stdin", "--global", "--default"],
        stdin=TOKEN)

    assert code == 0, err
    answer = json.loads(out)
    assert answer["usable_here"] is True
    assert answer["machine_reads"] == ["connections", "doctor", "servers"]
    assert "hint" not in answer
    _assert_no_token(out, err)


def test_outside_a_project_the_machine_reads_use_the_global_connection(place, monkeypatch):
    monkeypatch.setattr(coolify_module, "_project_root", lambda: None)
    code, _, err, _ = _run(
        ["connect", "main", "--url", URL, "--token-stdin", "--global", "--default"],
        stdin=TOKEN)
    assert code == 0, err

    code, out, err, _ = _run(["connections"])
    assert code == 0, err
    report = json.loads(out)
    assert report["connections"]["main"]["allow_write"] is False
    assert report["machine_reads"]["connections"]["main"]["scope"] == "machine"
    _assert_no_token(out, err)

    probe = Probe()
    code, out, err, _ = _run(["--connection", "main", "doctor"], probe=probe)
    assert code == 0, err
    assert json.loads(out)["connections"]["main"]["version"] == "4.3.23"

    probe = Probe()
    code, out, err, _ = _run(["servers"], probe=probe)
    assert code == 0, err
    assert [path for path, _ in probe.seen] == ["/api/v1/servers"]

    for argv in (["deploy", "abc"], ["applications"], ["env", "list", "abc"]):
        probe = Probe()
        code, _, err, _ = _run(argv, probe=probe)
        assert code == 4, argv
        assert json.loads(err)["error"]["code"] == "connection_not_granted"
        assert probe.seen == []


def test_inside_a_project_the_global_connection_still_needs_the_grant(place):
    code, _, err, _ = _run(
        ["connect", "main", "--url", URL, "--token-stdin", "--global", "--default"],
        stdin=TOKEN)
    assert code == 0, err
    for argv in (["servers"], ["connections"], ["doctor"]):
        probe = Probe()
        code, _, err, _ = _run(argv, probe=probe)
        assert code == 4, argv
        assert json.loads(err)["error"]["code"] == "connection_not_granted"
        assert probe.seen == []


def test_connect_default_points_the_project_default(place):
    code, _, err, _ = _run(
        ["connect", "exp", "--url", URL, "--token-stdin", "--project", "--default"],
        stdin=TOKEN)

    assert code == 0, err
    assert json.loads(place.registry.read_text())["default"] == "exp"


def test_connect_ssh_key_records_root_and_the_path(place, tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("not a real key")
    code, out, err, _ = _run(
        ["connect", "exp", "--url", URL, "--token-stdin", "--ssh-key", str(key)],
        stdin=TOKEN)

    assert code == 0, err
    entry = json.loads(place.registry.read_text())["connections"]["exp"]
    assert entry == {"base_url": URL, "secret_env": "COOLIFY_EXP_TOKEN",
                     "ssh": {"user": "root", "key_path": str(key)}}


def test_connect_keeps_other_entries_grants_and_lines(place):
    place.registry.parent.mkdir(parents=True)
    place.registry.write_text(json.dumps({
        "default": "other",
        "connections": {
            "other": {"base_url": "https://other.example.com",
                      "secret_env": "COOLIFY_OTHER_TOKEN"},
            "exp": {"base_url": "http://old.example.com",
                    "secret_env": "COOLIFY_EXP_TOKEN", "stale": "x",
                    "allow_write": False}}}))
    place.env_local.write_text(
        "# local secrets\nOTHER=1\nexport COOLIFY_EXP_TOKEN=old|value\n"
        "COOLIFY_OTHER_TOKEN='9|zzz'\nCOOLIFY_EXP_TOKEN=older\n")

    code, _, err, _ = _run(["connect", "exp", "--url", URL, "--token-stdin"],
                           stdin=TOKEN)

    assert code == 0, err
    registry = json.loads(place.registry.read_text())
    assert registry["default"] == "other"
    assert registry["connections"]["other"] == {
        "base_url": "https://other.example.com", "secret_env": "COOLIFY_OTHER_TOKEN"}
    assert registry["connections"]["exp"] == {
        "base_url": URL, "secret_env": "COOLIFY_EXP_TOKEN", "allow_write": False}
    assert place.env_local.read_text() == (
        f"# local secrets\nOTHER=1\nCOOLIFY_EXP_TOKEN='{TOKEN}'\n"
        "COOLIFY_OTHER_TOKEN='9|zzz'\n")
    assert _mode(place.env_local) == 0o600


def test_a_failed_probe_is_reported_and_keeps_the_entry(place):
    probe = Probe(fail="connection refused")
    code, out, err, _ = _run(
        ["connect", "exp", "--url", URL, "--token-stdin"], probe, stdin=TOKEN)

    assert code == 5
    answer = json.loads(out)
    assert answer["ok"] is False
    assert "network error" in answer["doctor"]["error"]
    assert "category" not in answer["doctor"]
    assert "exp" in json.loads(place.registry.read_text())["connections"]
    assert coolify_module._parse_env_file(place.env_local)["COOLIFY_EXP_TOKEN"] == TOKEN
    _assert_no_token(out, err)


def test_a_rejected_token_exits_2(place):
    class Rejecting(Probe):
        def handler(self, request):
            return httpx.Response(401, json={"message": "Unauthenticated."})

    code, out, err, _ = _run(
        ["connect", "exp", "--url", URL, "--token-stdin"], Rejecting(), stdin=TOKEN)

    assert code == 2
    assert json.loads(out)["ok"] is False
    _assert_no_token(out, err)


@pytest.mark.parametrize("argv,stdin,code_word", [
    (["--url", URL, "--token-stdin"], "", "input"),
    (["--url", URL, "--token-stdin"], "1|abc\nsecond line", "input"),
    (["--url", URL, "--token-stdin"], "1|a b", "input"),
    (["--url", URL + "/api/v1", "--token-stdin"], TOKEN, "input"),
    (["--url", "https://user:pw@coolify.example.com", "--token-stdin"], TOKEN, "input"),
    (["--url", "coolify.example.com", "--token-stdin"], TOKEN, "input"),
    (["--url", URL, "--token-env", "UNSET_KEY_FOR_TEST"], "", "input"),
])
def test_bad_input_is_refused_before_anything_is_written(place, argv, stdin, code_word):
    code, out, err, _ = _run(["connect", "exp", *argv], stdin=stdin)

    assert code == 6
    assert json.loads(err)["error"]["code"] == code_word
    assert not place.registry.exists() and not place.env_local.exists()
    assert "abc" not in err and "second line" not in err


def test_the_global_token_flag_is_refused_for_connect(place):
    code, _, err, _ = _run(["--token", "x", "connect", "exp", "--url", URL,
                            "--token-stdin"], stdin=TOKEN)

    assert code == 6
    assert not place.registry.exists()


def test_project_scope_outside_a_project_is_refused(place, monkeypatch):
    monkeypatch.setattr(coolify_module, "_project_root", lambda: None)
    code, _, err, _ = _run(["connect", "exp", "--url", URL, "--token-stdin",
                            "--project"], stdin=TOKEN)

    assert code == 6
    assert json.loads(err)["error"]["code"] == "no_project"
    assert not place.global_credentials.exists()


def test_the_read_only_switch_refuses_before_writing(place, monkeypatch):
    monkeypatch.setenv("CAPABILITIES_READ_ONLY", "1")
    code, _, err, _ = _run(["connect", "exp", "--url", URL, "--token-stdin"],
                           stdin=TOKEN)

    assert code == 4
    assert json.loads(err)["error"]["code"] == "read_only_switch"
    assert not place.registry.exists() and not place.env_local.exists()


def test_the_written_connection_is_what_the_verbs_then_resolve(place):
    code, _, err, _ = _run(["connect", "exp", "--url", URL, "--token-stdin"],
                           stdin=TOKEN)
    assert code == 0, err

    coolify_module._RECORDS = None
    conn = coolify_module._resolve_conn(types.SimpleNamespace(
        connection=None, base_url=None, token=None))
    assert conn["base_url"] == URL and conn["token"] == TOKEN
    report = coolify_module._connections_report()
    row = report["connections"]["exp"]["keys"][1]
    assert row["key"] == "COOLIFY_EXP_TOKEN" and row["value"] == "…" + TOKEN[-4:]
    assert row["source"] == str(place.env_local)


# ── credentials files are read only through the contract's parser ──────────

PIPE_TOKEN = "3|Zx9|pipes|inside"


@pytest.mark.parametrize("line", [
    f"COOLIFY_EXP_TOKEN={PIPE_TOKEN}",
    f"COOLIFY_EXP_TOKEN='{PIPE_TOKEN}'",
    f'COOLIFY_EXP_TOKEN="{PIPE_TOKEN}"',
    f"export COOLIFY_EXP_TOKEN={PIPE_TOKEN}",
])
@pytest.mark.parametrize("tier", ["project-local", "project", "user"])
def test_a_token_containing_a_pipe_survives_every_read_path(place, monkeypatch, tier, line):
    files = {"project-local": place.env_local, "project": place.root / ".env",
             "user": place.global_credentials}
    target = files[tier]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# comment\n{line}\n")
    place.registry.parent.mkdir(parents=True)
    place.registry.write_text(json.dumps({"connections": {"exp": {
        "base_url": URL, "secret_env": "COOLIFY_EXP_TOKEN"}}}))

    assert coolify_module._resolve_env_key("COOLIFY_EXP_TOKEN")[0] == PIPE_TOKEN
    conn, problem = coolify_module._build_conn(
        "exp", {"base_url": URL, "secret_env": "COOLIFY_EXP_TOKEN"},
        types.SimpleNamespace(base_url=None, token=None))
    assert problem is None and conn["token"] == PIPE_TOKEN
    assert coolify_module._read_env_secret("COOLIFY_EXP_TOKEN", "t") == PIPE_TOKEN
    probe = Probe()
    code, out, err, _ = _run(["doctor"], probe)
    assert code == 0, err
    assert ("/api/v1/version", f"Bearer {PIPE_TOKEN}") in probe.seen
    assert PIPE_TOKEN not in out and PIPE_TOKEN not in err


def test_the_writer_round_trips_a_pipe_through_the_parser(tmp_path):
    path = tmp_path / "credentials.env"
    coolify_module._write_env_key(path, "COOLIFY_EXP_TOKEN", PIPE_TOKEN)
    assert coolify_module._parse_env_file(path) == {"COOLIFY_EXP_TOKEN": PIPE_TOKEN}
    assert _mode(path) == 0o600


def test_no_code_path_sources_a_credentials_file():
    """Every env file is read by `_parse_env_file`; nothing hands one to a shell."""
    assert not re.search(r"""["'](?:source|\.)\s+[^"']*(?:\.env|credentials)""", _code)
    assert "os.system(" not in _code
    assert "shell=True" not in _code
    readers = re.findall(r"_parse_env_file\(([^)]*)\)", _code)
    assert any("CREDENTIALS_ENV" in r for r in readers)
    assert any('root / fname' in r for r in readers)
